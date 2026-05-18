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
from app.services.istore_mapping import publishable_mapping
from app.services.seo_copy_quality import sanitize_generated_seo_copy, truncate_without_ellipsis

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
_ENGLISH_LETTER_RE = re.compile(r"[A-Za-z]")
ENGLISH_FALLBACK_PHRASES = (
    "is available from Compass",
    "Explore",
    "Discover",
    "available from",
)
CONTENT_DRAFT_FIELD = "content_draft"


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
    _append_publish_log(approval, "אושר לפרסום מבוקר לאחר בדיקה אנושית", actor=approved_by, metadata=metadata)

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
    _append_publish_log(approval, "נדחה על ידי בודק אנושי", actor=approved_by, metadata=metadata)

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
        if not settings.istore_publish_enabled or settings.istore_safe_mode:
            _append_publish_log(
                approval,
                "פרסום תוכן אוטומטי נחסם במצב בטוח; הטיוטה נשארה לבדיקה/ייצוא ידני",
            )
            db.add(approval)
            db.commit()
            db.refresh(approval)
            raise PermissionError("ISTORE_SAFE_MODE blocks automatic content publishing; use manual export")

        return export_content_draft_for_manual_publish(db, approval)

    payload = _json_dict(approval.proposed_payload_json)
    validate_istore_payload(payload)

    if not publishable_mapping(approval):
        raise ValueError("ISTORE product mapping not verified")

    if dry_run:
        _append_publish_log(
            approval,
            "בדיקת פרסום יבשה עברה בהצלחה; לא נשלחה בקשה ל-ISTORE",
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
            f"נשלח עדכון SEO בלבד ל-ISTORE עבור {approval.field_path}",
        )
        product_id = str(approval.istore_product_id)
        response = client.update_product(product_id, payload)
        approval.publish_response_json = json.dumps(_redacted_json(response), ensure_ascii=False)
        approval.publish_timestamp = datetime.now(UTC)
        fetched = client.get_product(product_id)
    except IStoreAPIError as exc:
        approval.status = "FAILED_REVIEW_REQUIRED"
        approval.publish_response_json = json.dumps(
            {"error": _redact_token(str(exc))},
            ensure_ascii=False,
        )
        _append_publish_log(
            approval,
            "פרסום ב-ISTORE נכשל ונדרשת בדיקה אנושית",
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
        _append_publish_log(approval, "פורסם ואומת ב-ISTORE")
    else:
        approval.status = "FAILED_REVIEW_REQUIRED"
        verified = False
        _append_publish_log(
            approval,
            "התקבלה תגובת עדכון מ-ISTORE, אך האימות לא תאם לערך המוצע",
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

    if not publishable_mapping(approval):
        raise ValueError("ISTORE product mapping not verified")

    if dry_run:
        _append_publish_log(approval, "בדיקת שחזור יבשה עברה בהצלחה; לא נשלחה בקשה ל-ISTORE")
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
            f"נשלח שחזור SEO בלבד ל-ISTORE עבור {approval.field_path}",
        )
        product_id = str(approval.istore_product_id)
        response = client.update_product(product_id, payload)
        approval.publish_response_json = json.dumps(_redacted_json(response), ensure_ascii=False)
        fetched = client.get_product(product_id)
    except IStoreAPIError as exc:
        approval.status = "ROLLBACK_FAILED_REVIEW_REQUIRED"
        approval.publish_response_json = json.dumps(
            {"error": _redact_token(str(exc))},
            ensure_ascii=False,
        )
        _append_publish_log(
            approval,
            "שחזור ב-ISTORE נכשל ונדרשת בדיקה אנושית",
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
        _append_publish_log(approval, "השחזור פורסם ואומת ב-ISTORE")
    else:
        approval.status = "ROLLBACK_FAILED_REVIEW_REQUIRED"
        verified = False
        _append_publish_log(
            approval,
            "התקבלה תגובת שחזור, אך האימות לא תאם לערך המקורי",
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

    stored_preview = (
        _json_dict(approval.proposed_value)
        if approval.field_path == CONTENT_DRAFT_FIELD
        else {}
    )

    product_name = _hebrew_topic(_product_display_name(before) or approval.target_id, "המוצר")
    category = _hebrew_topic(_category(before), "הקטגוריה")
    content_title = stored_preview.get("suggested_title") or _content_title(product_name, category)
    meta_title = stored_preview.get("suggested_meta_title") or _hebrew_meta_title(product_name, category)
    meta_description = stored_preview.get("suggested_meta_description") or _hebrew_meta_description(
        product_name,
        category,
    )
    article = stored_preview.get("hebrew_article") or _hebrew_article(
        product_name,
        category,
        _hebrew_context(approval.proposed_value),
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
    preview_html = _content_preview_html(content_title, meta_title, meta_description, article, faq, internal_links)
    content_export = {
        "status": "READY_FOR_MANUAL_PUBLISH" if approval.field_path == CONTENT_DRAFT_FIELD else approval.status,
        "target_url": approval.target_url,
        "suggested_title": content_title,
        "suggested_meta_title": meta_title,
        "suggested_meta_description": meta_description,
        "html": preview_html,
        "faq": faq,
        "internal_links": internal_links,
        "schema_suggestions": schema,
    }

    return {
        "fix_id": approval.id,
        "status": approval.status,
        "target_type": approval.target_type,
        "field_path": approval.field_path,
        "current_value": approval.current_value,
        "proposed_value": approval.proposed_value,
        "proposed_payload": proposed_payload,
        "suggested_title": content_title,
        "suggested_meta_title": meta_title,
        "suggested_meta_description": meta_description,
        "hebrew_article": article,
        "preview_html": preview_html,
        "content_export": content_export,
        "faq": faq,
        "internal_links": internal_links,
        "schema_suggestions": schema,
        "publish_gates": _safety_payload(),
        "rollback_preview": rollback_preview(approval),
    }


def mark_english_fallback_drafts_stale(db: Session) -> dict[str, object]:
    """Safely mark pending English fallback drafts stale without publishing or deleting records."""
    stale_statuses = {"PENDING_APPROVAL", "APPROVED"}
    candidates = (
        db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status.in_(sorted(stale_statuses)))
        .order_by(IStoreSEOApproval.created_at.desc(), IStoreSEOApproval.id.desc())
        .all()
    )
    marked: list[IStoreSEOApproval] = []

    for approval in candidates:
        searchable = "\n".join(
            [
                approval.proposed_value or "",
                approval.proposed_payload_json or "",
                approval.seo_reason or "",
            ]
        )
        if not contains_english_fallback_text(searchable):
            continue

        approval.status = "STALE_ENGLISH_FALLBACK"
        approval.approval_action = "marked_stale_english_cleanup"
        _append_publish_log(
            approval,
            "טיוטה סומנה כלא עדכנית בגלל תבנית אנגלית ישנה; לא נמחקה ולא פורסמה",
            metadata={"cleanup": "english_fallback"},
        )
        db.add(approval)
        marked.append(approval)

    db.commit()
    for approval in marked:
        db.refresh(approval)

    return {
        "success": True,
        "stale_marked": len(marked),
        "published_records_deleted": 0,
        "published_records_modified": 0,
        "publishing_attempted": False,
        "fixes": [approval.to_dict() for approval in marked],
    }


def export_content_draft_for_manual_publish(db: Session, approval: IStoreSEOApproval) -> dict[str, object]:
    """Prepare a copy/export payload for one approved content draft when no content API exists."""
    if approval.field_path != CONTENT_DRAFT_FIELD or approval.target_type not in {"page", "category", "content"}:
        raise ValueError("Only article/content drafts can be exported for manual publishing")

    if approval.status not in {"APPROVED", "READY_FOR_MANUAL_PUBLISH"}:
        raise ValueError("Content export requires an approved draft")

    preview = preview_generated_content(approval, db)
    approval.status = "READY_FOR_MANUAL_PUBLISH"
    approval.publish_response_json = json.dumps(
        {"mode": "manual_export", "api_publish_sent": False, "status": approval.status}, ensure_ascii=False
    )
    _append_publish_log(
        approval,
        "טיוטת תוכן הוכנה להעתקה ידנית; לא בוצע פרסום אוטומטי",
        metadata={"api_publish_sent": False},
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    return {
        "success": True,
        "status": approval.status,
        "api_publish_sent": False,
        "fix": approval.to_dict(),
        "preview_html": preview["preview_html"],
        "copy_payload": preview["content_export"],
        "instructions": [
            "להעתיק ידנית את הכותרת, המטא והתוכן למערכת הניהול לאחר בדיקה מקדימה.",
            "אין לשנות מחיר, מלאי, תמונות, קטגוריות, משלוחים, מותג, SKU או הזמנות.",
            "לא נשלח פרסום אוטומטי כי לא מוגדר Endpoint תוכן/בלוג בטוח ב-ISTORE.",
        ],
    }


def contains_english_fallback_text(value: str) -> bool:
    normalized = _clean(value).lower()
    return any(phrase.lower() in normalized for phrase in ENGLISH_FALLBACK_PHRASES)

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
        source_url=url,
        istore_product_id=product_id,
        publish_mapping_verified=True,
        mapping_conflict=False,
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
    _append_publish_log(approval, "טיוטה נוצרה לאישור אנושי; לא בוצע פרסום ל-ISTORE")
    return approval


def _product_fix_proposals(product: dict[str, Any], pages: list[PageAudit] | None = None) -> list[ProposedFix]:
    description, language_id = _description(product)
    name = _hebrew_topic(description.get("name") or product.get("name"), "המוצר")
    meta_title = _clean(product.get("meta_title") or description.get("meta_title") or "")
    meta_description = _clean(product.get("meta_description") or description.get("meta_description") or "")
    body = _clean(description.get("description") or product.get("description") or "")
    keyword = _clean(product.get("keyword") or "")
    category = _hebrew_topic(_category(product), "הקטגוריה")
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
                "תיאור מוצר קצר: נוצר תוכן מסחרי בעברית עם שאלות נפוצות, קישורים פנימיים וסכמת SEO מוצעת",
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
            reasons.append("חסרים קישורים פנימיים")

        if page.word_count < 500:
            reasons.append("נדרשת הרחבת תוכן")

        if not reasons:
            continue

        topic = _hebrew_topic(page.h1 or page.title or page.url, "עמוד תוכן")
        page_category = _hebrew_topic(_category_from_url(page.url), "הקטגוריה")
        payload = {
               "suggested_title": _content_title(topic, page_category),
               "suggested_meta_title": _hebrew_meta_title(topic, page_category),
               "suggested_meta_description": _hebrew_meta_description(topic, page_category),
               "hebrew_article": _hebrew_article(topic, page_category, _hebrew_context(page.meta_description or "")),
               "faq": _hebrew_faq(topic, page_category),
               "internal_links": _internal_link_suggestions(page.url, topic, pages),
        }
        payload["schema_suggestions"] = _schema_suggestions("category", topic, page.url, payload["faq"])

        approval = IStoreSEOApproval(
            target_type="category" if _looks_like_category(page.url) else "page",
            target_id=page.url,
            target_url=page.url,
            source_page_audit_id=page.id,
            source_url=page.url,
            publish_mapping_verified=False,
            mapping_conflict=False,
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
            "טיוטת תוכן בעברית נוצרה לתצוגה מקדימה בלבד; לא נוצר מטען פרסום ל-ISTORE",
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
    return truncate_without_ellipsis(_clean(value), limit)


def _slugify(value: str) -> str:
    transliterated = value.lower().replace(" ", "-")
    transliterated = re.sub(r"[^a-z0-9\u0590-\u05ff-]+", "-", transliterated).strip("-")
    return transliterated or "מוצר"


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


def _has_english_letters(value: str) -> bool:
    return bool(_ENGLISH_LETTER_RE.search(value or ""))


def _hebrew_topic(value: Any, fallback: str) -> str:
    cleaned = _clean(value)
    if cleaned and _has_hebrew(cleaned) and not contains_english_fallback_text(cleaned):
        # Keep Hebrew, digits and common punctuation; drop Latin fragments from old fallbacks.
        cleaned = re.sub(r"[A-Za-z]+", "", cleaned)
        cleaned = _SPACE_RE.sub(" ", cleaned).strip(" -|,.;:")
        return cleaned or fallback
    return fallback


def _hebrew_context(value: Any) -> str:
    cleaned = _clean(value)
    if not cleaned or not _has_hebrew(cleaned) or contains_english_fallback_text(cleaned):
        return ""
    return re.sub(r"[A-Za-z]+", "", cleaned).strip()


def _content_title(topic: str, category: str) -> str:
    category_part = f" ב{category}" if category and category not in {"הקטגוריה", topic} else ""
    return _clip(f"{topic}{category_part}: מדריך תוכן שימושי", 70)


def _content_preview_html(
    title: str,
    meta_title: str,
    meta_description: str,
    article: str,
    faq: list[dict[str, str]],
    internal_links: list[dict[str, str]],
) -> str:
    faq_html = "".join(
        f"<h3>{escape(item['question'])}</h3><p>{escape(item['answer'])}</p>" for item in faq
    )
    links_html = "".join(
        f"<li><a href=\"{escape(link['url'])}\">{escape(link['anchor_text'])}</a> - {escape(link['reason'])}</li>"
        for link in internal_links
    )
    links_section = f"<h2>קישורים פנימיים מומלצים</h2><ul>{links_html}</ul>" if links_html else ""
    return (
        "<main dir=\"rtl\" lang=\"he\">"
        f"<h1>{escape(title)}</h1>"
        f"<p><strong>כותרת מטא:</strong> {escape(meta_title)}</p>"
        f"<p><strong>תיאור מטא:</strong> {escape(meta_description)}</p>"
        f"{article}"
        f"<section><h2>שאלות נפוצות</h2>{faq_html}</section>"
        f"{links_section}"
        "</main>"
    )


def _hebrew_meta_title(name: str, category: str) -> str:
    category_part = f" {category}" if category and category not in name else ""
    return f"{name}{category_part} | קנייה אונליין בקומפס"


def _hebrew_meta_description(name: str, category: str) -> str:
    category_part = f" בקטגוריית {category}" if category else ""
    return sanitize_generated_seo_copy(
        f"מחפשים {name}? בקומפס תמצאו מידע ברור{category_part}, "
        "מפרט שימושי, תמונות ושירות מקצועי לרכישה אונליין."
    )


def _expanded_description(
    name: str,
    current: str,
    category: str,
    target_url: str | None,
    pages: list[PageAudit],
) -> str:
    intro = _hebrew_context(current) if current and _hebrew_context(current) else _hebrew_article(name, category, "")
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
        "<h2>שאלות נפוצות לפני רכישה</h2>"
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
        "<p>צוות קומפס ממליץ לבחור לפי שימוש, אמינות וחוויית קנייה מלאה - "
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
        if not label or not _has_hebrew(label):
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
    return _hebrew_topic(parts[-1].replace("-", " ") if parts else "", "הקטגוריה")


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