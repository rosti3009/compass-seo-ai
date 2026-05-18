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


def _seed_crawl(db_session: Session) -> CrawlRun:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=2, average_score=64)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/products/gas-grill",
                status_code=200,
                title="Gas Grill",
                meta_description="Generic AI copy",
                h1="Gas Grill",
                word_count=90,
                missing_fields="generic_ai_meta,duplicate_meta_description",
                page_type="product",
                seo_score=48,
                seo_risk_level="critical",
                remediation_suggestions=json.dumps(["rewrite_meta_description"]),
                context_keywords=json.dumps(["גריל גז", "חצר"]),
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/brand/weber",
                status_code=200,
                title="Weber",
                meta_description="Weber brand",
                h1="Weber",
                word_count=140,
                missing_fields="",
                page_type="brand",
                seo_score=80,
                seo_risk_level="low",
                remediation_suggestions=json.dumps([]),
                context_keywords=json.dumps(["וובר"]),
            ),
        ]
    )
    db_session.commit()
    return crawl_run


def _seed_pending_fix(db_session: Session) -> IStoreSEOApproval:
    fix = IStoreSEOApproval(
        target_type="product",
        target_id="gas-grill",
        target_url="https://example.com/products/gas-grill",
        field_path="meta_description",
        current_value="Old meta value",
        proposed_value="New Hebrew meta value",
        seo_reason="Generic meta description",
        risk_level="high",
        issue_type="generic_ai_meta",
        priority_score=92,
        status="PENDING_APPROVAL",
        approval_metadata_json=json.dumps({"page_type": "product"}),
    )
    db_session.add(fix)
    db_session.commit()
    db_session.refresh(fix)
    return fix


def test_seo_operations_view_loads(client: TestClient, db_session: Session) -> None:
    _seed_crawl(db_session)
    _seed_pending_fix(db_session)

    response = client.get("/seo/operations-view")

    assert response.status_code == 200
    assert "לוח תפעול SEO" in response.text
    assert "הרץ סריקה" in response.text
    assert "Generic AI meta" in response.text
    assert "data-endpoint=\"/crawler/run\"" in response.text


def test_latest_crawler_results_view_loads(client: TestClient, db_session: Session) -> None:
    _seed_crawl(db_session)

    response = client.get("/crawler/results-view/latest")

    assert response.status_code == 200
    assert "תוצאות סריקה אחרונה" in response.text
    assert "remediation_suggestions" in response.text
    assert "rewrite_meta_description" in response.text


def test_pending_fixes_view_loads_and_renders_old_new_diff(client: TestClient, db_session: Session) -> None:
    _seed_crawl(db_session)
    _seed_pending_fix(db_session)

    response = client.get("/seo/fixes/pending-view")

    assert response.status_code == 200
    assert "תיקוני SEO ממתינים" in response.text
    assert "Old meta value" in response.text
    assert "New Hebrew meta value" in response.text
    assert "dry-run publish" in response.text


def test_dashboard_buttons_do_not_use_raw_api_form_actions(client: TestClient) -> None:
    response = client.get("/seo/operations-view")

    assert response.status_code == 200
    dashboard_html = response.text
    assert 'action="/crawler/run"' not in dashboard_html
    assert 'action="/seo/tasks/from-latest-crawl"' not in dashboard_html
    assert 'action="/seo/fixes/generate-from-latest-crawl"' not in dashboard_html
    assert 'data-endpoint="/seo/fixes/generate-from-latest-crawl"' in dashboard_html


def test_no_auto_publish_behavior_changed(client: TestClient, db_session: Session) -> None:
    fix = _seed_pending_fix(db_session)

    response = client.post(f"/integrations/istore/seo-approvals/{fix.id}/publish", json={"approval": False})

    assert response.status_code in {400, 403}
    db_session.refresh(fix)
    assert fix.status == "PENDING_APPROVAL"
    validate_istore_payload({"meta_title": "כותרת מאושרת"})
    with pytest.raises(ValueError, match="not allowed"):
        validate_istore_payload({"h1": "לא לפרסום אוטומטי"})
