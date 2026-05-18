import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, IStoreSEOApproval, PageAudit
from app.main import app
from app.services.istore_approval import validate_istore_payload


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


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _crawl(db_session: Session) -> CrawlRun:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=1, average_score=62)
    db_session.add(crawl_run)
    db_session.flush()
    return crawl_run


def _page(db_session: Session, crawl_run: CrawlRun, **overrides: object) -> PageAudit:
    values = {
        "crawl_run_id": crawl_run.id,
        "url": "https://example.com/products/gas-grill",
        "status_code": 200,
        "title": "גריל גז מקצועי לגינה עם מבערים חזקים ואביזרים משלימים במחיר משתלם במיוחד",
        "meta_description": "פתרון איכותי עם ביצועים מעולים, מקסימום נוחות ומתאים לשימוש מקצועי וביתי.",
        "h1": "גריל גז מקצועי",
        "word_count": 80,
        "missing_fields": "generic_ai_meta,title_too_long",
        "page_type": "product",
        "seo_score": 52.0,
        "seo_risk_level": "high",
        "remediation_suggestions": json.dumps(["rewrite_meta_description", "shorten_title"]),
        "context_keywords": json.dumps(["גריל גז", "חצר"]),
        "primary_intent": "גריל גז",
        "commercial_intent_score": 0.9,
    }
    values.update(overrides)
    if "remediation_suggestions" not in overrides:
        missing = str(values["missing_fields"])
        suggestions = []
        meta_issue_names = ["generic_ai_meta", "duplicate_meta_similarity", "duplicate_meta_description"]
        if any(issue in missing for issue in meta_issue_names):
            suggestions.append("rewrite_meta_description")
        if "title_too_long" in missing or "duplicate_title_similarity" in missing:
            suggestions.append("shorten_title")
        if "thin_content" in missing:
            suggestions.append("expand_content")
        values["remediation_suggestions"] = json.dumps(suggestions)
    page = PageAudit(**values)
    db_session.add(page)
    db_session.commit()
    db_session.refresh(page)
    return page


def test_generic_ai_meta_creates_rewrite_meta_description_fix(client: TestClient, db_session: Session) -> None:
    _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")

    response = client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    assert response.status_code == 201
    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.status == "PENDING_APPROVAL"
    assert fix.issue_type == "generic_ai_meta"
    assert fix.field_path == "meta_description"
    assert fix.source_audit_id is not None
    assert fix.priority_score > 0
    assert "פתרון איכותי" not in fix.proposed_value


def test_title_too_long_creates_shortened_title_fix(client: TestClient, db_session: Session) -> None:
    _page(db_session, _crawl(db_session), missing_fields="title_too_long")

    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.issue_type == "title_too_long"
    assert fix.field_path == "meta_title"
    assert len(fix.proposed_value) <= 65
    assert "גריל גז מקצועי" in fix.proposed_value


def test_brand_page_creates_unique_brand_context_meta_fix(client: TestClient, db_session: Session) -> None:
    _page(
        db_session,
        _crawl(db_session),
        url="https://example.com/brand/boretti",
        title="Boretti",
        h1="Boretti",
        page_type="brand",
        missing_fields="duplicate_meta_similarity",
        context_keywords=json.dumps(["טאבונים", "גרילים"]),
        primary_intent="מותג",
    )

    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.target_type == "page"
    assert fix.issue_type == "duplicate_meta_similarity"
    assert "Boretti" in fix.proposed_value
    assert "מותג" in fix.proposed_value


def test_system_page_indexable_creates_recommendation_only_record(client: TestClient, db_session: Session) -> None:
    _page(
        db_session,
        _crawl(db_session),
        url="https://example.com/account/login",
        page_type="system",
        missing_fields="system_page_indexable",
        title="Login",
        h1="Login",
    )

    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.target_type == "recommendation"
    assert fix.field_path == "noindex_recommendation"
    assert fix.issue_type == "system_page_indexable"
    assert "noindex" in fix.proposed_value
    assert json.loads(fix.proposed_payload_json)["api_publish_allowed"] is False


def test_dry_run_does_not_save_records(client: TestClient, db_session: Session) -> None:
    _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")

    response = client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": True})

    assert response.json()["fixes_generated"] == 1
    assert db_session.query(IStoreSEOApproval).count() == 0


def test_duplicate_protection_works(client: TestClient, db_session: Session) -> None:
    _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")

    first = client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False}).json()
    second = client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False}).json()

    assert first["fixes_generated"] == 1
    assert second["fixes_generated"] == 0
    assert second["duplicates_skipped"] == 1
    assert db_session.query(IStoreSEOApproval).count() == 1


def test_pending_fixes_endpoint_returns_grouped_sorted_review_data(client: TestClient, db_session: Session) -> None:
    crawl_run = _crawl(db_session)
    _page(
        db_session, crawl_run, url="https://example.com/products/high", missing_fields="generic_ai_meta", seo_score=40
    )
    _page(
        db_session, crawl_run, url="https://example.com/products/medium", missing_fields="title_too_long", seo_score=80
    )
    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    response = client.get("/seo/fixes/pending")

    assert response.status_code == 200
    payload = response.json()
    scores = [fix["priority_score"] for fix in payload["fixes"]]
    assert scores == sorted(scores, reverse=True)
    assert "generic_ai_meta" in payload["grouped_by_issue_type"]
    assert "product" in payload["grouped_by_page_type"]
    assert payload["fixes"][0]["preview"]["safe_publish_status"]


def test_forbidden_hebrew_phrases_are_removed_from_proposals(client: TestClient, db_session: Session) -> None:
    _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")

    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    fix = db_session.query(IStoreSEOApproval).one()
    forbidden = ["פתרון איכותי", "ביצועים מעולים", "מקסימום נוחות", "מתאים לשימוש מקצועי וביתי"]
    assert all(phrase not in fix.proposed_value for phrase in forbidden)


def test_publishing_safety_gates_remain_unchanged() -> None:
    validate_istore_payload({"meta_title": "כותרת מאושרת"})
    with pytest.raises(ValueError, match="price"):
        validate_istore_payload({"meta_title": "כותרת", "price": "99"})
    with pytest.raises(ValueError, match="not allowed"):
        validate_istore_payload({"h1": "לא לפרסום אוטומטי"})


class MappingClient:
    def __init__(self, products: list[dict[str, object]]) -> None:
        self.products = products

    def list_products(self) -> dict[str, object]:
        return {"products": self.products}


def test_crawler_product_fix_uses_no_page_audit_id_as_publish_target(client: TestClient, db_session: Session) -> None:
    page = _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")

    response = client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})

    assert response.status_code == 201
    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.source_page_audit_id == page.id
    assert fix.source_url == page.url
    assert fix.target_id != str(page.id)
    assert fix.target_id == ""
    assert fix.publish_mapping_verified is False
    assert fix.to_dict()["publishable"] is False


def test_verify_mapping_endpoint_maps_slug_and_pending_view_exposes_status(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")
    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})
    monkeypatch.setattr(
        "app.services.istore_mapping.IStoreClient.from_settings",
        lambda: MappingClient([{"product_id": "real-123", "url": "https://example.com/products/gas-grill"}]),
    )

    response = client.post("/seo/fixes/verify-istore-mappings")

    assert response.status_code == 200
    assert response.json()["mapped"][0]["istore_product_id"] == "real-123"
    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.target_id == "real-123"
    assert fix.istore_product_id == "real-123"
    assert fix.publish_mapping_verified is True
    pending = client.get("/seo/fixes/pending").json()["fixes"][0]
    assert pending["publish_mapping_verified"] is True
    assert pending["istore_product_id"] == "real-123"
    assert pending["publishable"] is False


def test_verify_mapping_endpoint_reports_conflicts_and_blocks_publishable(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page(db_session, _crawl(db_session), missing_fields="generic_ai_meta")
    client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": False})
    monkeypatch.setattr(
        "app.services.istore_mapping.IStoreClient.from_settings",
        lambda: MappingClient(
            [
                {"product_id": "real-1", "url": "https://example.com/products/gas-grill"},
                {"product_id": "real-2", "keyword": "gas-grill"},
            ]
        ),
    )

    response = client.post("/seo/fixes/verify-istore-mappings")

    assert response.status_code == 200
    assert response.json()["conflicts"][0]["candidate_product_ids"] == ["real-1", "real-2"]
    fix = db_session.query(IStoreSEOApproval).one()
    assert fix.mapping_conflict is True
    assert fix.publish_mapping_verified is False
    assert fix.to_dict()["publishable"] is False
