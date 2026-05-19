from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.models import IStoreSEOApproval
from app.services.seo_draft_lifecycle import (
    fresh_drafts,
    invalidate_stale_drafts,
    is_stale_draft,
    regenerate_stale_drafts,
    stale_drafts,
)
from app.services.seo_engine_version import CURRENT_SEO_ENGINE_VERSION


def _mk(db: Session, **kw) -> IStoreSEOApproval:
    payload = {
        "target_type": "product",
        "target_id": "1",
        "field_path": "meta_title",
        "proposed_value": "חדש",
        "status": "PENDING_APPROVAL",
        "generated_engine_version": CURRENT_SEO_ENGINE_VERSION,
    }
    payload.update(kw)
    draft = IStoreSEOApproval(**payload)
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def test_stale_invalidation_and_regeneration() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine)
    db = session_local()
    try:
        old = _mk(db, generated_engine_version="old_v1", proposed_value="קנייה חכמה")
        fresh = _mk(db, proposed_value="טקסט איכותי")
        assert is_stale_draft(old)
        assert not is_stale_draft(fresh)
        assert len(stale_drafts(db)) == 1
        invalidate_stale_drafts(db)
        db.refresh(old)
        assert old.status == "INVALIDATED"
        assert old.invalidated_at is not None
        regenerate_stale_drafts(db)
        regenerated = db.query(IStoreSEOApproval).filter(IStoreSEOApproval.regenerated_from_id == old.id).first()
        assert regenerated is not None
        assert regenerated.generated_engine_version == CURRENT_SEO_ENGINE_VERSION
        assert len(fresh_drafts(db)) >= 1
    finally:
        db.close()
