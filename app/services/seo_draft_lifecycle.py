from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import IStoreSEOApproval
from app.services.seo_engine_version import CURRENT_SEO_ENGINE_VERSION

FORBIDDEN_PHRASES = ("קנייה חכמה", "יתרונות מרכזיים", "סקירת מותג", "...", "…")
OUTDATED_RECOMMENDATION_TYPES = {"h1_recommendation", "noindex_recommendation"}


def is_stale_draft(draft: IStoreSEOApproval) -> bool:
    if draft.status == "INVALIDATED":
        return False
    if draft.generated_engine_version and draft.generated_engine_version != CURRENT_SEO_ENGINE_VERSION:
        return True
    content = f"{draft.proposed_value or ''} {draft.seo_reason or ''}"
    if any(phrase in content for phrase in FORBIDDEN_PHRASES):
        return True
    if draft.generated_engine_version and not draft.to_dict().get("publishable") and (
        draft.field_path in OUTDATED_RECOMMENDATION_TYPES
    ):
        return True
    return False


def invalidate_stale_drafts(db: Session, reason: str = "stale_draft") -> dict[str, int]:
    drafts = db.query(IStoreSEOApproval).all()
    invalidated = 0
    for draft in drafts:
        if is_stale_draft(draft):
            draft.status = "INVALIDATED"
            draft.invalidated_at = datetime.now(UTC)
            draft.invalidation_reason = reason
            invalidated += 1
            db.add(draft)
    db.commit()
    return {"invalidated_count": invalidated}


def regenerate_stale_drafts(db: Session) -> dict[str, int]:
    drafts = db.query(IStoreSEOApproval).all()
    created = 0
    for draft in drafts:
        if not is_stale_draft(draft) and draft.status != "INVALIDATED":
            continue
        proposed = draft.proposed_value
        reason = draft.seo_reason
        if draft.target_type == "home":
            proposed = "דורש בדיקה ידנית"
            reason = "דורש בדיקה ידנית"
        if draft.current_value and len(draft.current_value) >= len(draft.proposed_value or ""):
            proposed = "נראה שהטקסט הקיים כבר איכותי"
            reason = "נראה שהטקסט הקיים כבר איכותי"
        if any(p in (proposed or "") for p in FORBIDDEN_PHRASES):
            proposed = "נראה שהטקסט הקיים כבר איכותי"
            reason = "נראה שהטקסט הקיים כבר איכותי"
        new_draft = IStoreSEOApproval(
            target_type=draft.target_type,
            target_id=draft.target_id,
            target_url=draft.target_url,
            source_page_audit_id=draft.source_page_audit_id,
            source_url=draft.source_url,
            istore_product_id=draft.istore_product_id,
            publish_mapping_verified=draft.publish_mapping_verified,
            mapping_conflict=draft.mapping_conflict,
            mapping_confidence=draft.mapping_confidence,
            mapping_source=draft.mapping_source,
            field_path=draft.field_path,
            current_value=draft.current_value,
            proposed_value=proposed,
            seo_reason=reason,
            risk_level=draft.risk_level,
            source_audit_id=draft.source_audit_id,
            issue_type=draft.issue_type,
            priority_score=draft.priority_score,
            status="PENDING_APPROVAL",
            before_snapshot_json=draft.before_snapshot_json,
            proposed_payload_json=draft.proposed_payload_json,
            rollback_payload_json=draft.rollback_payload_json,
            approval_metadata_json=draft.approval_metadata_json,
            generated_engine_version=CURRENT_SEO_ENGINE_VERSION,
            generated_at=datetime.now(UTC),
            regenerated_from_id=draft.id,
        )
        db.add(new_draft)
        created += 1
    db.commit()
    return {"regenerated_count": created}


def stale_drafts(db: Session) -> list[IStoreSEOApproval]:
    return [d for d in db.query(IStoreSEOApproval).order_by(IStoreSEOApproval.id.desc()).all() if is_stale_draft(d)]


def fresh_drafts(db: Session) -> list[IStoreSEOApproval]:
    return [
        d
        for d in db.query(IStoreSEOApproval).order_by(IStoreSEOApproval.id.desc()).all()
        if not is_stale_draft(d) and d.status != "INVALIDATED"
    ]
