import json
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.models import IStoreSEOApproval
from app.services.istore_approval import publish_approved_fix, scan_istore_seo_opportunities, validate_istore_payload


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


class MockIStoreClient:
    def __init__(self, *, verified_value: str = "Better title", response: dict[str, object] | None = None) -> None:
        self.put_payloads: list[dict[str, object]] = []
        self.verified_value = verified_value
        self.response = response or {"ok": True}

    def list_products(self) -> dict[str, object]:
        return {
            "products": [
                {
                    "product_id": "123",
                    "keyword": "bad slug value",
                    "meta_title": "Bad",
                    "meta_description": "Short",
                    "product_description": {"3": {"name": "Portable Grill", "description": "Tiny desc"}},
                }
            ]
        }

    def update_product(self, product_id: str, payload: dict[str, object]) -> dict[str, object]:
        assert product_id == "123"
        self.put_payloads.append(payload)
        return self.response

    def get_product(self, product_id: str) -> dict[str, object]:
        assert product_id == "123"
        return {"product": {"meta_title": self.verified_value}}


def _approved_fix(db_session: Session, *, proposed_payload: dict[str, object] | None = None) -> IStoreSEOApproval:
    payload = proposed_payload or {"meta_title": "Better title"}
    fix = IStoreSEOApproval(
        target_type="product",
        target_id="123",
        field_path="meta_title",
        current_value="Bad",
        proposed_value="Better title",
        seo_reason="Missing or weak meta title",
        risk_level="low",
        status="APPROVED",
        before_snapshot_json=json.dumps({"product_id": "123", "meta_title": "Bad"}),
        proposed_payload_json=json.dumps(payload),
        rollback_payload_json=json.dumps({"meta_title": "Bad"}),
    )
    db_session.add(fix)
    db_session.commit()
    db_session.refresh(fix)
    return fix


def test_generated_fixes_are_drafts_only_and_rollback_payload_is_stored(db_session: Session) -> None:
    result = scan_istore_seo_opportunities(db_session, client=MockIStoreClient())

    assert result["drafts_created"] >= 4
    fixes = db_session.query(IStoreSEOApproval).all()
    assert {fix.status for fix in fixes} == {"PENDING_APPROVAL"}
    assert all(json.loads(fix.rollback_payload_json) is not None for fix in fixes)


def test_approval_required_before_publish(db_session: Session) -> None:
    fix = _approved_fix(db_session)
    client = MockIStoreClient()

    with pytest.raises(ValueError, match="approval=true"):
        publish_approved_fix(db_session, fix, approval_confirmed=False, client=client)

    assert client.put_payloads == []


def test_publish_disabled_blocks_put(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fix = _approved_fix(db_session)
    client = MockIStoreClient()
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", False)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)

    with pytest.raises(PermissionError, match="ISTORE_PUBLISH_ENABLED"):
        publish_approved_fix(db_session, fix, approval_confirmed=True, client=client)

    assert client.put_payloads == []


def test_safe_mode_blocks_put(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fix = _approved_fix(db_session)
    client = MockIStoreClient()
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", True)

    with pytest.raises(PermissionError, match="ISTORE_SAFE_MODE"):
        publish_approved_fix(db_session, fix, approval_confirmed=True, client=client)

    assert client.put_payloads == []


def test_dry_run_sends_no_put(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fix = _approved_fix(db_session)
    client = MockIStoreClient()
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)

    result = publish_approved_fix(db_session, fix, approval_confirmed=True, dry_run=True, client=client)

    assert result["dry_run"] is True
    assert result["put_sent"] is False
    assert client.put_payloads == []


def test_only_one_fix_publishes_per_request(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _approved_fix(db_session)
    _approved_fix(db_session)
    client = MockIStoreClient()
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)

    publish_approved_fix(db_session, first, approval_confirmed=True, client=client)

    assert len(client.put_payloads) == 1
    assert db_session.query(IStoreSEOApproval).filter(IStoreSEOApproval.status == "PUBLISHED").count() == 1


def test_blocked_commerce_fields_rejected() -> None:
    with pytest.raises(ValueError, match="price"):
        validate_istore_payload({"meta_title": "Good", "price": "9.99"})


def test_successful_publish_verifies_before_marking_published(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fix = _approved_fix(db_session)
    client = MockIStoreClient(verified_value="Better title")
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)

    result = publish_approved_fix(db_session, fix, approval_confirmed=True, client=client)

    assert result["verified"] is True
    assert result["fix"]["status"] == "PUBLISHED"


def test_failed_verification_does_not_mark_published(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fix = _approved_fix(db_session)
    client = MockIStoreClient(verified_value="Still bad")
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)

    result = publish_approved_fix(db_session, fix, approval_confirmed=True, client=client)

    assert result["verified"] is False
    assert result["fix"]["status"] == "FAILED_REVIEW_REQUIRED"


def test_token_is_never_exposed_in_publish_response(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fix = _approved_fix(db_session)
    token_value = "redactable" + "-token-value"
    client = MockIStoreClient(response={"message": f"accepted {token_value}"})
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)
    monkeypatch.setattr("app.core.config.settings.istore_x_token", token_value)
    monkeypatch.setattr("app.integrations.istore.settings.istore_x_token", token_value)

    result = publish_approved_fix(db_session, fix, approval_confirmed=True, client=client)

    assert token_value not in json.dumps(result)
    assert "[redacted]" in json.dumps(result)

def test_generated_product_description_contains_hebrew_faq_and_schema_preview(db_session: Session) -> None:
    result = scan_istore_seo_opportunities(db_session, client=MockIStoreClient())
    description_fix = next(
        fix for fix in db_session.query(IStoreSEOApproval).all() if fix.field_path.endswith(".description")
    )

    assert result["drafts_created"] >= 4
    assert "שאלות נפוצות" in description_fix.proposed_value
    assert "מחיר" in description_fix.proposed_value
    assert "מלאי" in description_fix.proposed_value
    assert "price" not in json.dumps(json.loads(description_fix.proposed_payload_json))
    assert json.loads(description_fix.publish_log_json)[0]["message"].startswith("Draft created")


def test_page_content_draft_includes_hebrew_article_faq_links_and_schema(db_session: Session) -> None:
    from app.db.models import CrawlRun, PageAudit

    crawl = CrawlRun(target_domain="https://example.test", status="completed")
    db_session.add(crawl)
    db_session.commit()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl.id,
                url="https://example.test/category/grills",
                status_code=200,
                title="גרילים לגינה",
                h1="גרילים לגינה",
                meta_description="קטגוריית גרילים",
                word_count=120,
                internal_links=0,
                seo_score=55,
            ),
            PageAudit(
                crawl_run_id=crawl.id,
                url="https://example.test/guides/bbq",
                status_code=200,
                title="מדריך ברביקיו",
                h1="מדריך ברביקיו",
                word_count=800,
                internal_links=6,
                seo_score=88,
            ),
        ]
    )
    db_session.commit()

    scan_istore_seo_opportunities(db_session, client=MockIStoreClient())
    draft = db_session.query(IStoreSEOApproval).filter(IStoreSEOApproval.field_path == "content_draft").first()

    assert draft is not None
    payload = json.loads(draft.proposed_value)
    assert "מדריך קנייה" in payload["hebrew_article"]
    assert payload["faq"][0]["question"].startswith("למי מתאים")
    assert payload["internal_links"][0]["url"] == "https://example.test/guides/bbq"
    assert {schema["@type"] for schema in payload["schema_suggestions"]} >= {"FAQPage", "Article"}
    assert json.loads(draft.proposed_payload_json) == {}


def test_rollback_published_fix_is_gated_and_seo_only(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.istore_approval import rollback_published_fix

    fix = _approved_fix(db_session)
    fix.status = "PUBLISHED"
    db_session.add(fix)
    db_session.commit()
    db_session.refresh(fix)
    client = MockIStoreClient(verified_value="Bad")
    monkeypatch.setattr("app.services.istore_approval.settings.istore_publish_enabled", True)
    monkeypatch.setattr("app.services.istore_approval.settings.istore_safe_mode", False)

    with pytest.raises(ValueError, match="approval=true"):
        rollback_published_fix(db_session, fix, approval_confirmed=False, client=client)

    result = rollback_published_fix(db_session, fix, approval_confirmed=True, client=client)

    assert result["verified"] is True
    assert result["fix"]["status"] == "ROLLED_BACK"
    assert client.put_payloads == [{"meta_title": "Bad"}]