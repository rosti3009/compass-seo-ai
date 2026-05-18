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


def test_pending_fixes_view_has_rtl_review_workflow_filters_and_badges(client: TestClient, db_session: Session) -> None:
    _seed_crawl(db_session)
    _seed_pending_fix(db_session)

    response = client.get("/seo/fixes/pending-view")

    assert response.status_code == 200
    html = response.text
    assert '<html lang="he" dir="rtl">' in html
    assert 'data-filter="page_type"' in html
    assert 'data-filter="issue_type"' in html
    assert 'data-filter="risk_level"' in html
    assert 'data-filter="publishable"' in html
    assert 'data-filter="mapping_verified"' in html
    assert "ממתין לאישור" in html
    assert "מיפוי חסר" in html
    assert "לא ניתן לפרסום" in html
    assert "data-action=\"edit-fix\"" in html
    assert "verify-mapping" in html


def test_pending_fixes_view_renders_structured_diff_viewer(client: TestClient, db_session: Session) -> None:
    _seed_crawl(db_session)
    _seed_pending_fix(db_session)

    response = client.get("/seo/fixes/pending-view")

    assert response.status_code == 200
    html = response.text
    assert "diff-viewer" in html
    assert "OLD" in html
    assert "NEW" in html
    assert "תווים" in html
    assert "removed-text" in html
    assert "added-text" in html


def test_generated_seo_copy_does_not_leak_context_keywords(client: TestClient, db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=1, average_score=42)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add(
        PageAudit(
            crawl_run_id=crawl_run.id,
            url="https://example.com/products/gas-grill",
            status_code=200,
            title="Gas Grill",
            meta_description="Generic AI copy",
            h1="Gas Grill",
            word_count=80,
            missing_fields="generic_ai_meta,duplicate_title_similarity",
            page_type="product",
            seo_score=40,
            seo_risk_level="high",
            remediation_suggestions=json.dumps(["rewrite_meta_description"]),
            context_keywords=json.dumps(["grills, smokers, butcher tools"]),
        )
    )
    db_session.commit()

    response = client.post("/seo/fixes/generate-from-latest-crawl", json={"dry_run": True, "limit": 10})

    assert response.status_code == 201
    payload = response.json()
    generated_text = " ".join(fix["proposed_value"] for fix in payload["fixes"])
    assert "grills, smokers, butcher tools" not in generated_text
    assert "גריל גז" in generated_text


def test_dashboard_metrics_render_numeric_values(client: TestClient, db_session: Session) -> None:
    _seed_crawl(db_session)
    _seed_pending_fix(db_session)

    response = client.get("/seo/operations-view")

    assert response.status_code == 200
    html = response.text
    assert "ציון SEO ממוצע" in html
    assert "64" in html
    assert "ממתין לאישור" in html
    assert "--:average_score" not in html
    assert "--:pending fixes count" not in html


def _seed_simple_workspace_fixes(db_session: Session) -> tuple[IStoreSEOApproval, IStoreSEOApproval, IStoreSEOApproval]:
    safe_fix = IStoreSEOApproval(
        target_type="product",
        target_id="sku-safe",
        target_url="https://example.com/products/gas-grill-safe",
        istore_product_id="sku-safe",
        publish_mapping_verified=True,
        mapping_conflict=False,
        mapping_confidence=100,
        field_path="meta_description",
        current_value="Old generic description",
        proposed_value="תיאור חדש וברור לגריל גז איכותי לחצר",
        seo_reason="Generic meta description",
        risk_level="high",
        issue_type="generic_ai_meta",
        priority_score=95,
        status="PENDING_APPROVAL",
        approval_metadata_json=json.dumps({"page_type": "product"}),
    )
    unsafe_fix = IStoreSEOApproval(
        target_type="product",
        target_id="sku-needs-map",
        target_url="https://example.com/products/needs-map",
        field_path="meta_title",
        current_value="Very long old title that needs replacement",
        proposed_value="כותרת קצרה וברורה",
        seo_reason="Title too long",
        risk_level="medium",
        issue_type="title_too_long",
        priority_score=80,
        status="PENDING_APPROVAL",
        approval_metadata_json=json.dumps({"page_type": "product"}),
    )
    system_fix = IStoreSEOApproval(
        target_type="recommendation",
        target_id="system-page",
        target_url="https://example.com/cart",
        publish_mapping_verified=True,
        mapping_conflict=False,
        mapping_confidence=100,
        field_path="noindex_recommendation",
        current_value="index",
        proposed_value="לא לקדם את עמוד המערכת הזה בגוגל",
        seo_reason="System page indexable",
        risk_level="high",
        issue_type="system_page_indexable",
        priority_score=70,
        status="PENDING_APPROVAL",
        approval_metadata_json=json.dumps({"page_type": "system"}),
    )
    db_session.add_all([safe_fix, unsafe_fix, system_fix])
    db_session.commit()
    db_session.refresh(safe_fix)
    db_session.refresh(unsafe_fix)
    db_session.refresh(system_fix)
    return safe_fix, unsafe_fix, system_fix


def test_simple_workspace_loads_with_plain_hebrew_cards_and_preview(client: TestClient, db_session: Session) -> None:
    _seed_simple_workspace_fixes(db_session)

    response = client.get("/seo/simple-workspace")

    assert response.status_code == 200
    html = response.text
    assert "לוח עבודה פשוט לקידום בגוגל" in html
    assert "מה צריך טיפול היום" in html
    assert "כמה תיקונים מוכנים לבדיקה" in html
    assert "כמה תיקונים בטוחים לאישור" in html
    assert "כמה תיקונים צריכים בדיקת מוצר" in html
    assert "כמה תיקונים כבר אושרו" in html
    assert "פעולה מומלצת הבאה" in html
    assert "התיאור נשמע גנרי מדי ולא מספיק משכנע ללקוחות." in html
    assert "הכותרת ארוכה מדי וגוגל עלול לחתוך אותה." in html
    assert "זה עמוד מערכת שלא צריך לקדם בגוגל." in html
    assert "כותרת טובה יותר יכולה לגרום ליותר אנשים ללחוץ על התוצאה בגוגל." in html
    assert "תיאור ייחודי עוזר לגוגל להבין במה העמוד שונה מעמודים אחרים." in html
    assert "ממתין לבדיקה" in html
    assert "צריך לחבר למוצר בחנות" in html
    assert "עדיין לא ניתן לפרסם" in html
    assert "מצב מתקדם" in html
    assert 'href="/seo/operations-view"' in html
    assert "בדקת שהטקסט החדש נשמע נכון ומתאים למוצר?" in html
    assert "אשר את כל התיקונים הבטוחים בדף זה" in html
    assert "איך זה עשוי להיראות בגוגל:" in html
    assert "שלב 1: הבעיה" in html
    assert "שלב 2: התיקון המוצע" in html
    assert "שלב 3: בדיקה שלך" in html
    assert "שלב 4: אישור" in html


def test_simple_workspace_hides_technical_fields_and_raw_payloads(client: TestClient, db_session: Session) -> None:
    _seed_simple_workspace_fixes(db_session)

    response = client.get("/seo/simple-workspace")

    assert response.status_code == 200
    html = response.text
    assert "issue_type" not in html
    assert "mapping_confidence" not in html
    assert "canonical_url" not in html
    assert "source_audit_id" not in html
    assert "proposed_payload" not in html
    assert "rollback_payload" not in html
    assert "JSON" not in html
    assert "dry-run publish" not in html


def test_simple_bulk_approve_only_includes_safe_publishable_fixes(client: TestClient, db_session: Session) -> None:
    safe_fix, unsafe_fix, system_fix = _seed_simple_workspace_fixes(db_session)

    response = client.get("/seo/simple-workspace")

    assert response.status_code == 200
    html = response.text
    assert f'data-bulk-ids="{safe_fix.id}"' in html
    assert f'data-bulk-ids="{unsafe_fix.id}"' not in html
    assert f'data-bulk-ids="{system_fix.id}"' not in html

    bulk_response = client.post(
        "/seo/simple-workspace/bulk-approve",
        json={"fix_ids": [safe_fix.id, unsafe_fix.id, system_fix.id], "confirmed": True},
    )

    assert bulk_response.status_code == 200
    assert bulk_response.json()["approved_count"] == 1
    assert bulk_response.json()["skipped_count"] == 2
    db_session.refresh(safe_fix)
    db_session.refresh(unsafe_fix)
    db_session.refresh(system_fix)
    assert safe_fix.status == "APPROVED"
    assert unsafe_fix.status == "PENDING_APPROVAL"
    assert system_fix.status == "PENDING_APPROVAL"
    assert safe_fix.publish_timestamp is None
