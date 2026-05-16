from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
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
    "images",
    "image",
    "options",
    "shipping",
    "brand",
    "sku",
    "model",
}
PUBLISHABLE_STATUSES = {"APPROVED"}

_HTML_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


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

    for product in products:
        product_id = _product_id(product)
        if not product_id:
            continue
        for proposal in _product_fix_proposals(product):
            if _existing_pending(db, product_id, proposal.field_path, proposal.proposed_value):
                skipped_duplicates += 1
                continue
            approval = _approval_from_proposal(product_id, _product_url(product), product, proposal)
            db.add(approval)
            created.append(approval)

    page_drafts = _site_page_content_opportunities(db)
    for draft in page_drafts:
        db.add(draft)
        created.append(draft)

    db.commit()
    for approval in created:
        db.refresh(approval)
    return {
        "success": True,
        "products_scanned": len(products),
        "site_pages_scanned": len(_latest_pages(db)),
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

    payload = _json_dict(approval.proposed_payload_json)
    validate_istore_payload(payload)

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "put_sent": False,
            "fix": approval.to_dict(),
            "safety": _safety_payload(),
        }
    if not settings.istore_publish_enabled:
        raise PermissionError("ISTORE_PUBLISH_ENABLED must be true before publishing")
    if settings.istore_safe_mode:
        raise PermissionError("ISTORE_SAFE_MODE must be false before publishing")

    client = client or IStoreClient.from_settings()
    try:
        response = client.update_product(approval.target_id, payload)
        approval.publish_response_json = json.dumps(_redacted_json(response), ensure_ascii=False)
        approval.publish_timestamp = datetime.now(UTC)
        fetched = client.get_product(approval.target_id)
    except IStoreAPIError as exc:
        approval.status = "FAILED_REVIEW_REQUIRED"
        approval.publish_response_json = json.dumps({"error": _redact_token(str(exc))}, ensure_ascii=False)
        db.add(approval)
        db.commit()
        db.refresh(approval)
        return {"success": False, "verified": False, "fix": approval.to_dict()}

    if _field_value(fetched, approval.field_path) == approval.proposed_value:
        approval.status = "PUBLISHED"
        verified = True
    else:
        approval.status = "FAILED_REVIEW_REQUIRED"
        verified = False
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return {"success": verified, "verified": verified, "put_sent": True, "fix": approval.to_dict()}


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
        "instructions": ["Preview only. Rollback is never executed automatically."],
    }


def _approval_from_proposal(
    product_id: str, url: str | None, product: dict[str, Any], proposal: ProposedFix
) -> IStoreSEOApproval:
    proposed_payload = _payload_for_field(proposal.field_path, proposal.proposed_value)
    rollback_payload = _payload_for_field(proposal.field_path, proposal.current_value)
    return IStoreSEOApproval(
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


def _product_fix_proposals(product: dict[str, Any]) -> list[ProposedFix]:
    description, language_id = _description(product)
    name = _clean(description.get("name") or product.get("name") or "Product")
    meta_title = _clean(product.get("meta_title") or description.get("meta_title") or "")
    meta_description = _clean(product.get("meta_description") or description.get("meta_description") or "")
    body = _clean(description.get("description") or product.get("description") or "")
    keyword = _clean(product.get("keyword") or "")
    category = _clean(product.get("category") or product.get("category_name") or "")
    proposals: list[ProposedFix] = []

    if len(meta_title) < 15 or len(meta_title) > 65 or name.lower() not in meta_title.lower():
        proposals.append(
            ProposedFix("meta_title", meta_title, _clip(f"{name} | Compass", 60), "Missing or weak meta title")
        )
    if len(meta_description) < 70 or len(meta_description) > 160:
        proposals.append(
            ProposedFix(
                "meta_description",
                meta_description,
                _clip(
                    f"Discover {name}{f' in {category}' if category else ''} with details, benefits, "
                    "and professional Compass support.",
                    155,
                ),
                "Meta description is missing, too short, or too long",
            )
        )
    if not name or len(name) < 3:
        proposals.append(
            ProposedFix(
                f"product_description[{language_id}].name",
                name,
                meta_title or "SEO Product Name",
                "Missing or weak H1/product name",
                "medium",
            )
        )
    if len(body) < 250:
        proposals.append(
            ProposedFix(
                f"product_description[{language_id}].description",
                body,
                _expanded_description(name, body, category),
                "Short product description / content expansion opportunity",
                "medium",
            )
        )
    if not keyword or len(keyword) < 4 or " " in keyword.strip("/"):
        proposals.append(ProposedFix("keyword", keyword, _slugify(name), "Weak keyword/slug suggestion"))
    return proposals


def _site_page_content_opportunities(db: Session) -> list[IStoreSEOApproval]:
    drafts: list[IStoreSEOApproval] = []
    for page in _latest_pages(db):
        reasons = []
        if page.internal_links < 2:
            reasons.append("missing internal links")
        if page.word_count < 500:
            reasons.append("content expansion opportunities")
        if not reasons:
            continue
        payload = {
            "blog_article_draft": (
                f"Draft an article expanding {page.h1 or page.title or page.url} with FAQs and internal links."
            ),
            "faq_schema": {"@type": "FAQPage", "mainEntity": []},
        }
        drafts.append(
            IStoreSEOApproval(
                target_type="page",
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
        )
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
    for key in ("url", "href", "link"):
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


def _json_dict(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "seo-product"


def _expanded_description(name: str, current: str, category: str) -> str:
    base = current or f"{name} is available from Compass."
    return (
        f"{base}\n\n"
        f"Explore {name}{f' for {category}' if category else ''} with clear product details, practical use cases, "
        "key benefits, sizing or compatibility notes, delivery information, and expert support."
    )


def _safety_payload() -> dict[str, object]:
    return {
        "auto_publish": False,
        "requires_approval_true": True,
        "istore_publish_enabled": settings.istore_publish_enabled,
        "istore_safe_mode": settings.istore_safe_mode,
        "allowed_fields": sorted(ALLOWED_ISTORE_FIELDS),
    }