from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import IStoreSEOApproval, PageAudit
from app.integrations.istore import IStoreAPIError, IStoreClient, _redact_token

ALLOWED_ISTORE_FIELDS = {"product_description", "keyword", "meta_title", "meta_description"}
BLOCKED_ISTORE_FIELDS = {
    "price",
    "quantity",
    "stock",
    "status",
    "hidden",
    "category",
    "category_id",
    "categories",
    "images",
    "image",
    "options",
    "shipping",
    "brand",
    "sku",
    "model",
    "orders",
    "order",
    "inventory",
}
PUBLISHABLE_STATUSES = {"APPROVED"}
ROLLBACKABLE_STATUSES = {"PUBLISHED", "FAILED_REVIEW_REQUIRED"}

_HTML_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")


@dataclass(frozen=True)
class ProposedFix:
    field_path: str
    current_value: str
    proposed_value: str
    seo_reason: str
    risk_level: str = "low"


def scan_istore_seo_opportunities(
    db: Session, client: IStoreClient | None = None, limit: int = 50
) -> dict[str, object]:
    """Scan ISTORE products plus latest site pages and store draft SEO approvals only."""
    client = client or IStoreClient.from_settings()
    raw_products = client.list_products()
    products = _extract_products(raw_products)[:limit]
    created: list[IStoreSEOApproval] = []
    skipped_duplicates = 0

    pages = _latest_pages(db)
    for product in products:
        product_id = _product_id(product)
        if not product_id:
            continue

        for proposal in _product_fix_proposals(product, pages):
            if _existing_pending(db, product_id, proposal.field_path, proposal.proposed_value):
                skipped_duplicates += 1
                continue

            approval = _approval_from_proposal(product_id, _product_url(product), product, proposal)
            db.add(approval)
            created.append(approval)

    page_drafts = _site_page_content_opportunities(db, pages)
    for draft in page_drafts:
        db.add(draft)
        created.append(draft)

    db.commit()

    for approval in created:
        db.refresh(approval)

    return {
        "success": True,
        "products_scanned": len(products),
        "site_pages_scanned": len(pages),
        "drafts_created": len(created),
        "duplicates_skipped": skipped_duplicates,
        "fixes": [approval.to_dict() for approval in created],
        "safety": _safety_payload(),
    }


def approve_fix(
    db: Session,
    approval: IStoreSEOApproval,
    approved_by: str | None = None,
    metadata: dict[str, object] | None = None,
) -> IStoreSEOApproval:
    approval.status = "APPROVED"
    approval.approved_by = approved_by
    approval.approval_action = "approved"
    approval.approval_metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    _append_publish_log(approval, "Approved for gated publishing", actor=approved_by, metadata=metadata)

    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def reject_fix(
    db: Session,
    approval: IStoreSEOApproval,
    approved_by: str | None = None,
    metadata: dict[str, object] | None = None,
) -> IStoreSEOApproval:
    approval.status = "REJECTED"
    approval.approved_by = approved_by
    approval.approval_action = "rejected"
    approval.approval_metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    _append_publish_log(approval, "Rejected by reviewer", actor=approved_by, metadata=metadata)

    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def publish_approved_fix(
    db: Session,
    approval: IStoreSEOApproval,
    *,
    approval_confirmed: bool,
    dry_run: bool = False,
    client: IStoreClient | None = None,
) -> dict[str, object]:
    """Publish exactly one approved SEO fix after all safety gates pass and verification succeeds."""
    if not approval_confirmed:
        raise ValueError("Publishing requires explicit approval=true")

    if approval.status not in PUBLISHABLE_STATUSES:
        raise ValueError("Only approved fixes can be published")

    if approval.target_type != "product":
        raise ValueError("Only product SEO fields can be published to ISTORE automatically")

    payload = _json_dict(approval.proposed_payload_json)
    validate_istore_payload(payload)

    if dry_run:
        _append_publish_log(
            approval,
            "Dry-run publish validation passed; no ISTORE PUT sent",
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)

        return {
            "success": True,
            "dry_run": True,
            "put_sent": False,
            "fix": approval.to_dict(),
            "preview": preview_generated_content(approval, db),
            "safety": _safety_payload(),
        }

    if not settings.istore_publish_enabled:
        raise PermissionError("ISTORE_PUBLISH_ENABLED must be true before publishing")

    if settings.istore_safe_mode:
        raise PermissionError("ISTORE_SAFE_MODE must be false before publishing")

    client = client or IStoreClient.from_settings()

    try:
        _append_publish_log(
            approval,
            f"Sending SEO-only ISTORE update for {approval.field_path}",
        )
        response = client.update_product(approval.target_id, payload)
        approval.publish_response_json = json.dumps(_redacted_json(response), ensure_ascii=False)
        approval.publish_timestamp = datetime.now(UTC)
        fetched = client.get_product(approval.target_id)
    except IStoreAPIError as exc:
        approval.status = "FAILED_REVIEW_REQUIRED"
        approval.publish_response_json = json.dumps(
            {"error": _redact_token(str(exc))},
            ensure_ascii=False,
        )
        _append_publish_log(
            approval,
            "ISTORE publish failed and requires human review",
            metadata={"error": str(exc)},
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)

        return {
            "success": False,
            "verified": False,
            "fix": approval.to_dict(),
        }

    if _field_value(fetched, approval.field_path) == approval.proposed_value:
        approval.status = "PUBLISHED"
        verified = True
        _append_publish_log(approval, "Published and verified in ISTORE")
    else:
        approval.status = "FAILED_REVIEW_REQUIRED"
        verified = False
        _append_publish_log(
            approval,
            "ISTORE update response received, but verification did not match proposed value",
        )

    db.add(approval)
    db.commit()
    db.refresh(approval)

    return {
        "success": verified,
        "verified": verified,
        "put_sent": True,
        "fix": approval.to_dict(),
    }


def rollback_published_fix(
    db: Session,
    approval: IStoreSEOApproval,
    *,
    approval_confirmed: bool,
    dry_run: bool = False,
    client: IStoreClient | None = None,
) -> dict[str, object]:
    """Rollback a previously published SEO-only change through the same safety gates."""
    if not approval_confirmed:
        raise ValueError("Rollback requires explicit approval=true")

    if approval.status not in ROLLBACKABLE_STATUSES:
        raise ValueError("Only published or failed-review fixes can be rolled back")

    if approval.target_type != "product":
        raise ValueError("Only product SEO fields can be rolled back in ISTORE automatically")

    payload = _json_dict(approval.rollback_payload_json)
    validate_istore_payload(payload)

    if dry_run:
        _append_publish_log(approval, "Dry-run rollback validation passed; no ISTORE PUT sent")
        db.add(approval)
        db.commit()
        db.refresh(approval)

        return {
            "success": True,
            "dry_run": True,
            "put_sent": False,
            "rollback_preview": rollback_preview(approval),
        }

    if not settings.istore_publish_enabled:
        raise PermissionError("ISTORE_PUBLISH_ENABLED must be true before rollback")

    if settings.istore_safe_mode:
        raise PermissionError("ISTORE_SAFE_MODE must be false before rollback")

    client = client or IStoreClient.from_settings()

    try:
        _append_publish_log(
            approval,
            f"Sending SEO-only ISTORE rollback for {approval.field_path}",
        )
        response = client.update_product(approval.target_id, payload)
        approval.publish_response_json = json.dumps(_redacted_json(response), ensure_ascii=False)
        fetched = client.get_product(approval.target_id)
    except IStoreAPIError as exc:
        approval.status = "ROLLBACK_FAILED_REVIEW_REQUIRED"
        approval.publish_response_json = json.dumps(
            {"error": _redact_token(str(exc))},
            ensure_ascii=False,
        )
        _append_publish_log(
            approval,
            "ISTORE rollback failed and requires human review",
            metadata={"error": str(exc)},
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)

        return {
            "success": False,
            "verified": False,
            "fix": approval.to_dict(),
        }

    if _field_value(fetched, approval.field_path) == (approval.current_value or ""):
        approval.status = "ROLLED_BACK"
        verified = True
        _append_publish_log(approval, "Rollback published and verified in ISTORE")
    else:
        approval.status = "ROLLBACK_FAILED_REVIEW_REQUIRED"
        verified = False
        _append_publish_log(
            approval,
            "Rollback response received, but verification did not match original value",
        )

    db.add(approval)
    db.commit()
    db.refresh(approval)

    return {
        "success": verified,
        "verified": verified,
        "put_sent": True,
        "fix": approval.to_dict(),
    }


def validate_istore_payload(payload: dict[str, Any]) -> None:
    blocked = _blocked_keys(payload)
    if blocked:
        raise ValueError(f"Blocked commerce fields cannot be published: {', '.join(sorted(blocked))}")

    if not payload:
        raise ValueError("Publish payload is empty")

    for key in payload:
        if key not in ALLOWED_ISTORE_FIELDS:
            raise ValueError(f"Field is not allowed for ISTORE SEO publishing: {key}")

    descriptions = payload.get("product_description")
    if descriptions is not None:
        if not isinstance(descriptions, dict):
            raise ValueError("product_description must be keyed by language_id")

        for description in descriptions.values():
            if not isinstance(description, dict):
                raise ValueError("product_description[language_id] must be an object")

            for key in description:
                if key not in {"name", "description"}:
                    raise ValueError(f"Unsupported product_description field: {key}")


def rollback_preview(approval: IStoreSEOApproval) -> dict[str, object]:
    return {
        "fix_id": approval.id,
        "status": approval.status,
        "rollback_payload": _json_dict(approval.rollback_payload_json),
        "instructions": [
            "Preview only unless the explicit rollback endpoint is called with approval=true.",
            "Rollback payload is restricted to SEO fields and never touches pricing, inventory, "
            "orders, or commerce data.",
        ],
        "safety": _safety_payload(),
    }


def preview_generated_content(approval: IStoreSEOApproval, db: Session | None = None) -> dict[str, object]:
    """Return a reviewer-friendly preview with Hebrew copy, links, FAQ and schema suggestions."""
    proposed_payload = _json_dict(approval.proposed_payload_json)
    before = _json_dict(approval.before_snapshot_json)
    pages = _latest_pages(db) if db is not None else []
    stored_preview = _json_dict(approval.proposed_value) if approval.field_path == "content_draft" else {}

    product_name = _product_display_name(before) or approval.target_id
    category = _category(before)
    article = stored_preview.get("hebrew_article") or _hebrew_article(
        product_name,
        category,
        _clean(approval.proposed_value),
    )
    faq = stored_preview.get("faq") or _hebrew_faq(product_name, category)
    internal_links = stored_preview.get("internal_links") or _internal_link_suggestions(
        approval.target_url,
        product_name,
        pages,
    )
    schema = stored_preview.get("schema_suggestions") or _schema_suggestions(
        approval.target_type,
        product_name,
        approval.target_url,
        faq,
    )

    return {
        "fix_id": approval.id,
        "status": approval.status,
        "target_type": approval.target_type,
        "field_path": approval.field_path,
        "current_value": approval.current_value,
        "proposed_value": approval.proposed_value,
        "proposed_payload": proposed_payload,
        "hebrew_article": article,
        "faq": faq,
        "internal_links": internal_links,
        "schema_suggestions": schema,
        "publish_gates": _safety_payload(),
        "rollback_preview": rollback_preview(approval),
    }


def _approval_from_proposal(
    product_id: str,
    url: str | None,
    product: dict[str, Any],
    proposal: ProposedFix,
) -> IStoreSEOApproval:
    proposed_payload = _payload_for_field(proposal.field_path, proposal.proposed_value)
    rollback_payload = _payload_for_field(proposal.field_path, proposal.current_value)

    approval = IStoreSEOApproval(
        target_type="product",
        target_id=product_id,
        target_url=url,
        field_path=proposal.field_path,
        current_value=proposal.current_value,
        proposed_value=proposal.proposed_value,
        seo_reason=proposal.seo_reason,
        risk_level=proposal.risk_level,
        status="PENDING_APPROVAL",
        before_snapshot_json=json.dumps(product, ensure_ascii=False),
        proposed_payload_json=json.dumps(proposed_payload, ensure_ascii=False),
        rollback_payload_json=json.dumps(rollback_payload, ensure_ascii=False),
    )
    _append_publish_log(approval, "Draft created for human approval; no ISTORE publish attempted")
    return approval


def _product_fix_proposals(product: dict[str, Any], pages: list[PageAudit] | None = None) -> list[ProposedFix]:
    description, language_id = _description(product)
    name = _clean(description.get("name") or product.get("name") or "מוצר")
    meta_title = _clean(product.get("meta_title") or description.get("meta_title") or "")
    meta_description = _clean(product.get("meta_description") or description.get("meta_description") or "")
    body = _clean(description.get("description") or product.get("description") or "")
    keyword = _clean(product.get("keyword") or "")
    category = _category(product)
    proposals: list[ProposedFix] = []

    suggested_title = _clip(_hebrew_meta_title(name, category), 60)
    has_weak_title = (
        len(meta_title) < 30
        or len(meta_title) > 65
        or not _contains_product_name(meta_title, name)
        or not _has_hebrew(meta_title)
    )
    if has_weak_title:
        proposals.append(
            ProposedFix(
                "meta_title",
                meta_title,
                suggested_title,
                "כותרת SEO חסרה, קצרה או לא ממוקדת בעברית",
            )
        )

    suggested_description = _clip(_hebrew_meta_description(name, category), 155)
    if len(meta_description) < 70 or len(meta_description) > 160 or not _has_hebrew(meta_description):
        proposals.append(
            ProposedFix(
                "meta_description",
                meta_description,
                suggested_description,
                "תיאור מטא חסר, קצר או לא מספיק מסחרי בעברית",
            )
        )

    if not name or len(name) < 3:
        proposals.append(
            ProposedFix(
                f"product_description[{language_id}].name",
                name,
                meta_title or "שם מוצר ממוקד SEO",
                "שם מוצר/H1 חסר או חלש",
                "medium",
            )
        )

    if len(body) < 450 or not _has_hebrew(body):
        proposals.append(
            ProposedFix(
                f"product_description[{language_id}].description",
                body,
                _expanded_description(name, body, category, _product_url(product), pages or []),
                "תיאור מוצר קצר: נוצר תוכן Ecommerce בעברית עם FAQ, קישורים פנימיים וסכמת SEO מוצעת",
                "medium",
            )
        )

    if not keyword or len(keyword) < 4 or " " in keyword.strip("/"):
        proposals.append(
            ProposedFix(
                "keyword",
                keyword,
                _slugify(name),
                "הצעת URL keyword/slug בטוחה לשדה SEO בלבד",
            )
        )

    return proposals


def _site_page_content_opportunities(
    db: Session,
    pages: list[PageAudit] | None = None,
) -> list[IStoreSEOApproval]:
    drafts: list[IStoreSEOApproval] = []
    pages = pages if pages is not None else _latest_pages(db)

    for page in pages:
        reasons = []

        if page.internal_links < 2:
            reasons.append("missing internal links")

        if page.word_count < 500:
            reasons.append("content expansion opportunities")

        if not reasons:
            continue

        topic = page.h1 or page.title or page.url
        payload = {
            "hebrew_article": _hebrew_article(topic, _category_from_url(page.url), page.meta_description or ""),
            "faq": _hebrew_faq(topic, _category_from_url(page.url)),
            "internal_links": _internal_link_suggestions(page.url, topic, pages),
        }
        payload["schema_suggestions"] = _schema_suggestions("category", topic, page.url, payload["faq"])

        approval = IStoreSEOApproval(
            target_type="category" if _looks_like_category(page.url) else "page",
            target_id=page.url,
            target_url=page.url,
            field_path="content_draft",
            current_value=page.meta_description or page.title or "",
            proposed_value=json.dumps(payload, ensure_ascii=False),
            seo_reason=", ".join(reasons),
            risk_level="low",
            status="PENDING_APPROVAL",
            before_snapshot_json=json.dumps(page.to_dict(), ensure_ascii=False),
            proposed_payload_json=json.dumps({}, ensure_ascii=False),
            rollback_payload_json=json.dumps({}, ensure_ascii=False),
        )

        _append_publish_log(
            approval,
            "Hebrew article/category draft created for preview only; no ISTORE payload generated",
        )

        drafts.append(approval)

    return drafts


def _latest_pages(db: Session) -> list[PageAudit]:
    return db.query(PageAudit).order_by(PageAudit.crawled_at.desc(), PageAudit.id.desc()).limit(50).all()


def _existing_pending(db: Session, product_id: str, field_path: str, proposed_value: str) -> bool:
    return (
        db.query(IStoreSEOApproval)
        .filter(
            IStoreSEOApproval.target_id == product_id,
            IStoreSEOApproval.field_path == field_path,
            IStoreSEOApproval.proposed_value == proposed_value,
            IStoreSEOApproval.status.in_(["PENDING_APPROVAL", "APPROVED"]),
        )
        .first()
        is not None
    )


def _extract_products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("products", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        product = payload.get("product")
        if isinstance(product, dict):
            return [product]

    return []


def _product_id(product: dict[str, Any]) -> str:
    for key in ("product_id", "id", "productId"):
        value = product.get(key)
        if value is not None and str(value):
            return str(value)

    return ""


def _product_url(product: dict[str, Any]) -> str | None:
    for key in ("url", "href", "link", "canonical_url", "product_url"):
        value = product.get(key)
        if isinstance(value, str) and value:
            return value

    return None


def _description(product: dict[str, Any]) -> tuple[dict[str, Any], str]:
    descriptions = product.get("product_description")
    if isinstance(descriptions, dict):
        for language_id in ("3", "1", *descriptions.keys()):
            value = descriptions.get(language_id)
            if isinstance(value, dict):
                return value, str(language_id)

    return {}, "3"


def _payload_for_field(field_path: str, value: str) -> dict[str, Any]:
    if field_path.startswith("product_description["):
        language_id = field_path.split("[", 1)[1].split("]", 1)[0]
        leaf = field_path.rsplit(".", 1)[-1]
        return {"product_description": {language_id: {leaf: value}}}

    return {field_path: value}


def _field_value(product_payload: Any, field_path: str) -> str:
    product = product_payload.get("product", product_payload) if isinstance(product_payload, dict) else {}
    if not isinstance(product, dict):
        return ""

    if field_path.startswith("product_description["):
        language_id = field_path.split("[", 1)[1].split("]", 1)[0]
        leaf = field_path.rsplit(".", 1)[-1]
        descriptions = product.get("product_description")

        if isinstance(descriptions, dict) and isinstance(descriptions.get(language_id), dict):
            return _clean(descriptions[language_id].get(leaf) or "")

    return _clean(product.get(field_path) or "")


def _blocked_keys(value: Any) -> set[str]:
    found: set[str] = set()

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in BLOCKED_ISTORE_FIELDS:
                found.add(normalized)
            found.update(_blocked_keys(nested))

    elif isinstance(value, list):
        for item in value:
            found.update(_blocked_keys(item))

    return found


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def _json_list(value: str | None) -> list[dict[str, object]]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []

    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _redacted_json(value: Any) -> Any:
    return json.loads(_redact_token(json.dumps(value, ensure_ascii=False, default=str)))


def _clean(value: Any) -> str:
    if value is None:
        return ""

    return _SPACE_RE.sub(" ", _HTML_RE.sub(" ", unescape(str(value)))).strip()


def _clip(value: str, limit: int) -> str:
    value = _clean(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _slugify(value: str) -> str:
    transliterated = value.lower().replace(" ", "-")
    transliterated = re.sub(r"[^a-z0-9\u0590-\u05ff-]+", "-", transliterated).strip("-")
    return transliterated or "seo-product"


def _has_hebrew(value: str) -> bool:
    return bool(_HEBREW_RE.search(value or ""))


def _contains_product_name(value: str, name: str) -> bool:
    cleaned_name = _clean(name).lower()
    return bool(cleaned_name and cleaned_name in _clean(value).lower())


def _category(product: dict[str, Any]) -> str:
    for key in ("category", "category_name", "main_category", "department"):
        value = product.get(key)

        if isinstance(value, str) and value:
            return _clean(value)

        if isinstance(value, dict):
            nested = value.get("name") or value.get("title")
            if nested:
                return _clean(nested)

    categories = product.get("categories")
    if isinstance(categories, list):
        for item in categories:
            if isinstance(item, str) and item:
                return _clean(item)

            if isinstance(item, dict) and (item.get("name") or item.get("title")):
                return _clean(item.get("name") or item.get("title"))

    return ""


def _product_display_name(product: dict[str, Any]) -> str:
    description, _language_id = _description(product)
    return _clean(description.get("name") or product.get("name") or product.get("title") or "")


def _hebrew_meta_title(name: str, category: str) -> str:
    category_part = f" {category}" if category and category not in name else ""
    return f"{name}{category_part} | קנייה אונליין ב-Compass"


def _hebrew_meta_description(name: str, category: str) -> str:
    category_part = f" בקטגוריית {category}" if category else ""
    return (
        f"מחפשים {name}? ב-Compass תמצאו מידע ברור{category_part}, "
        "יתרונות מרכזיים, התאמה לצורך ושירות מקצועי לפני קנייה אונליין."
    )


def _expanded_description(
    name: str,
    current: str,
    category: str,
    target_url: str | None,
    pages: list[PageAudit],
) -> str:
    intro = current if current and _has_hebrew(current) else _hebrew_article(name, category, current)
    faq = _hebrew_faq(name, category)
    links = _internal_link_suggestions(target_url, name, pages)

    faq_items = "".join(
        f"<h3>{escape(item['question'])}</h3><p>{escape(item['answer'])}</p>"
        for item in faq
    )
    link_items = "".join(
        "<li>"
        f"<a href=\"{escape(str(link['url']))}\">{escape(str(link['anchor_text']))}</a>"
        f" - {escape(str(link['reason']))}</li>"
        for link in links
    )
    links_html = f"<h2>קישורים מומלצים להמשך קריאה</h2><ul>{link_items}</ul>" if link_items else ""

    return (
        f"<section dir=\"rtl\" lang=\"he\">{intro}"
        "<h2>שאלות נפוצות לפני קנייה</h2>"
        f"{faq_items}{links_html}</section>"
    )


def _hebrew_article(topic: str, category: str, context: str = "") -> str:
    safe_topic = escape(_clean(topic) or "המוצר")
    safe_category = escape(_clean(category) or "הקטגוריה")
    context_sentence = f" {escape(_clean(context))}" if context else ""

    return (
        f"<article dir=\"rtl\" lang=\"he\">"
        f"<h2>{safe_topic}: מדריך קנייה קצר ומעשי</h2>"
        f"<p>{safe_topic} מתאים ללקוחות שרוצים לקבל החלטה בטוחה בלי לנחש. "
        "המטרה היא להבין במה המוצר עוזר, למי הוא מתאים ואילו פרטים חשוב לבדוק "
        f"לפני שמוסיפים לעגלה.{context_sentence}</p>"
        "<h2>למי זה מתאים?</h2>"
        f"<p>אם אתם בוחנים פתרונות בתחום {safe_category}, כדאי להשוות בין שימוש יומיומי, "
        "נוחות, התאמה למידות או לסביבה, ואיכות השירות לאחר הקנייה.</p>"
        "<h2>איך לבחור נכון?</h2>"
        "<ul>"
        "<li>בדקו שהמפרט מתאים לצורך האמיתי שלכם.</li>"
        "<li>קראו את תיאור המוצר וההנחיות לפני שימוש.</li>"
        "<li>העדיפו מוצר עם מידע ברור, תמונות איכותיות ואפשרות לקבל תמיכה.</li>"
        "</ul>"
        "<p>צוות Compass ממליץ לבחור לפי שימוש, אמינות וחוויית קנייה מלאה - "
        "לא רק לפי כותרת קצרה.</p>"
        "</article>"
    )


def _hebrew_faq(name: str, category: str) -> list[dict[str, str]]:
    topic = _clean(name) or "המוצר"
    category_text = _clean(category) or "הקטגוריה"

    return [
        {
            "question": f"למי מתאים {topic}?",
            "answer": (
                f"{topic} מתאים ללקוחות שמחפשים פתרון ברור בתחום {category_text}, "
                "עם מידע מסודר לפני רכישה ושירות מקצועי במקרה של שאלה."
            ),
        },
        {
            "question": f"מה חשוב לבדוק לפני שקונים {topic}?",
            "answer": (
                "מומלץ לבדוק התאמה לשימוש הרצוי, מפרט, מידות או תאימות, "
                "תנאי משלוח וזמינות תמיכה לאחר הקנייה."
            ),
        },
        {
            "question": "האם התוכן משנה מחיר או מלאי?",
            "answer": (
                "לא. הצעת ה-SEO עוסקת רק בכותרות, תיאורים, FAQ, קישורים וסכמה, "
                "ואינה משנה מחיר, מלאי, הזמנות או שדות מסחריים."
            ),
        },
    ]


def _schema_suggestions(target_type: str, name: str, url: str | None, faq: object) -> list[dict[str, object]]:
    faq_items = faq if isinstance(faq, list) else _hebrew_faq(name, "")
    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item.get("question"),
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": item.get("answer"),
                },
            }
            for item in faq_items
            if isinstance(item, dict)
        ],
    }

    article_schema: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": name,
        "inLanguage": "he-IL",
    }
    if url:
        article_schema["mainEntityOfPage"] = url

    schemas = [faq_schema, article_schema]

    if target_type == "product":
        product_schema: dict[str, object] = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": name,
        }
        if url:
            product_schema["url"] = url
        schemas.insert(0, product_schema)

    return schemas


def _internal_link_suggestions(
    target_url: str | None,
    topic: str,
    pages: list[PageAudit],
    limit: int = 5,
) -> list[dict[str, str]]:
    clean_topic = _clean(topic)
    suggestions: list[dict[str, str]] = []

    for page in pages:
        if page.status_code >= 400 or not page.url or page.url == target_url:
            continue

        label = _clean(page.h1 or page.title or page.url)
        if not label:
            continue

        score = (page.seo_score or 0) + min(page.internal_links or 0, 20)
        reason = "עמוד סמכותי באתר לחיזוק ניווט פנימי"

        if clean_topic and clean_topic.lower() in label.lower():
            score += 25
            reason = "התאמה ישירה לנושא התוכן"

        suggestions.append(
            {
                "url": page.url,
                "anchor_text": label[:80],
                "reason": reason,
                "score": str(round(score, 1)),
            }
        )

    suggestions.sort(key=lambda item: float(item["score"]), reverse=True)
    return suggestions[:limit]


def _category_from_url(url: str) -> str:
    parts = [part for part in re.split(r"[/_-]+", url) if part and not part.startswith("http")]
    return _clean(parts[-1].replace("-", " ") if parts else "")


def _looks_like_category(url: str) -> bool:
    lowered = url.lower()
    return any(segment in lowered for segment in ("category", "categories", "collections", "catalog", "shop"))


def _append_publish_log(
    approval: IStoreSEOApproval,
    message: str,
    *,
    actor: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    entries = _json_list(getattr(approval, "publish_log_json", "[]"))
    entries.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "status": approval.status,
            "message": message,
            "actor": actor or "system",
            "metadata": metadata or {},
        }
    )
    approval.publish_log_json = json.dumps(entries, ensure_ascii=False)


def _safety_payload() -> dict[str, object]:
    return {
        "auto_publish": False,
        "requires_approval_true": True,
        "istore_publish_enabled": settings.istore_publish_enabled,
        "istore_safe_mode": settings.istore_safe_mode,
        "allowed_fields": sorted(ALLOWED_ISTORE_FIELDS),
        "blocked_commerce_fields": sorted(BLOCKED_ISTORE_FIELDS),
    }