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
    assert 'data-endpoint="/crawler/run"' in response.text


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
    assert 'data-action="edit-fix"' in html
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
    assert "כותרת טובה יותר יכולה לגרום ליותר אנשים ללחוץ על התוצאה בגוגל." in html
    assert "תיאור ייחודי עוזר לגוגל להבין במה העמוד שונה מעמודים אחרים." in html
    assert "ממתין לבדיקה" in html
    assert "בדיקת פרסום יבשה" in html
    assert "פרטים טכניים" in html
    assert "צריך לחבר למוצר בחנות" in html
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
    assert "בדוק שהשינוי הופיע באתר" in html


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


def test_simple_workspace_publish_visibility_and_block_reason(client: TestClient, db_session: Session) -> None:
    safe_fix, unsafe_fix, _ = _seed_simple_workspace_fixes(db_session)
    safe_fix.status = "APPROVED"
    db_session.add(safe_fix)
    db_session.commit()

    response = client.get("/seo/simple-workspace")
    html = response.text
    assert response.status_code == 200
    assert "בדיקת פרסום יבשה" in html
    assert "אי אפשר לפרסם עדיין כי המוצר לא חובר בוודאות לחנות." in html


def test_simple_workspace_verify_live_endpoint(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_fix, _, _ = _seed_simple_workspace_fixes(db_session)
    safe_fix.status = "PUBLISHED"
    db_session.add(safe_fix)
    db_session.commit()

    class Resp:
        text = (
            '<html><head><title>x</title><meta name="description" '
            'content="תיאור חדש וברור לגריל גז איכותי לחצר"></head></html>'
        )

    monkeypatch.setattr("app.api.routes.requests.get", lambda *a, **k: Resp())
    resp = client.post(f"/seo/simple-workspace/{safe_fix.id}/verify-live")
    assert resp.status_code == 200
    assert resp.json()["message"] == "השינוי מופיע באתר"


def test_simple_workspace_publish_actions_use_explicit_approval_and_confirmation(
    client: TestClient, db_session: Session
) -> None:
    safe_fix, _, _ = _seed_simple_workspace_fixes(db_session)
    safe_fix.status = "APPROVED"
    db_session.add(safe_fix)
    db_session.commit()

    response = client.get("/seo/simple-workspace")

    assert response.status_code == 200
    html = response.text
    assert 'data-action="fetch" data-endpoint="/integrations/istore/seo-approvals/' in html
    assert 'data-body=\'{"approval":true,"dry_run":true}\'' in html
    assert 'data-action="confirm-fetch"' in html
    assert 'data-body=\'{"approval":true,"dry_run":false}\'' in html
    assert 'data-action="confirm-fetch"' in html


def test_simple_workspace_dry_run_publish_does_not_mark_published(client: TestClient, db_session: Session) -> None:
    safe_fix, _, _ = _seed_simple_workspace_fixes(db_session)
    safe_fix.status = "APPROVED"
    db_session.add(safe_fix)
    db_session.commit()

    response = client.post(
        f"/integrations/istore/seo-approvals/{safe_fix.id}/publish",
        json={"approval": True, "dry_run": True},
    )
    assert response.status_code in {200, 400, 403}
    db_session.refresh(safe_fix)
    assert safe_fix.status != "PUBLISHED"


def test_simple_workspace_hides_system_slug_and_recommendation_only_rows(
    client: TestClient, db_session: Session
) -> None:
    safe_fix, _, system_fix = _seed_simple_workspace_fixes(db_session)
    slug_fix = IStoreSEOApproval(
        target_type="product",
        target_id="slug-1",
        target_url="https://example.com/products/slug",
        publish_mapping_verified=True,
        mapping_conflict=False,
        field_path="keyword",
        current_value="bad",
        proposed_value="better",
        seo_reason="slug",
        risk_level="medium",
        issue_type="invalid_slug",
        priority_score=10,
        status="PENDING_APPROVAL",
        approval_metadata_json=json.dumps({"page_type": "product"}),
    )
    keep_fix = IStoreSEOApproval(
        target_type="product",
        target_id="keep-1",
        target_url="https://example.com/products/keep",
        publish_mapping_verified=True,
        mapping_conflict=False,
        field_path="meta_title",
        current_value="טוב",
        proposed_value="אין צורך בשינוי",
        seo_reason="keep",
        risk_level="low",
        issue_type="title_too_short",
        priority_score=5,
        status="PENDING_APPROVAL",
        approval_metadata_json=json.dumps({"page_type": "product", "decision": {"decision": "KEEP_EXISTING"}}),
    )
    db_session.add_all([slug_fix, keep_fix])
    db_session.commit()

    html = client.get("/seo/simple-workspace").text
    assert "https://example.com/cart" not in html
    assert "https://example.com/products/slug" not in html
    assert "https://example.com/products/keep" not in html
    assert (safe_fix.target_url or "") in html


def _seed_fix_center_crawl(db_session: Session) -> CrawlRun:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=5, average_score=51)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/blog/brisket",
                status_code=200,
                title="מאמר בריסקט",
                meta_description="",
                h1="",
                word_count=120,
                internal_links=0,
                missing_fields="meta_description,h1,image_alt",
                page_type="article",
                seo_score=46,
                seo_risk_level="high",
                remediation_suggestions=json.dumps(["internal_link_opportunity"]),
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/products/grill-a",
                status_code=200,
                title="גריל גז",
                meta_description="תיאור מוצר",
                h1="גריל גז",
                word_count=80,
                internal_links=2,
                missing_fields="generic_ai_meta,redirect_chain",
                page_type="product",
                seo_score=52,
                seo_risk_level="critical",
                remediation_suggestions=json.dumps(["redirect_chain"]),
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/products/grill-b",
                status_code=200,
                title="גריל גז",
                meta_description="תיאור מוצר אחר",
                h1="גריל גז",
                word_count=80,
                internal_links=3,
                missing_fields="",
                page_type="product",
                seo_score=65,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/empty-category",
                status_code=200,
                title="קטגוריה ריקה",
                meta_description="",
                h1="קטגוריה ריקה",
                word_count=10,
                internal_links=1,
                missing_fields="meta_description",
                page_type="category",
                seo_score=20,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/old-404",
                status_code=404,
                title="ישן",
                meta_description="",
                h1="ישן",
                word_count=0,
                internal_links=0,
                missing_fields="",
                page_type="unknown",
                seo_score=0,
            ),
        ]
    )
    db_session.commit()
    return crawl_run


def test_fix_center_scan_generates_employee_friendly_tasks(client: TestClient, db_session: Session) -> None:
    _seed_fix_center_crawl(db_session)

    response = client.post("/seo/fix-center/scan")

    assert response.status_code == 201
    payload = response.json()
    assert payload["created_count"] >= 8
    tasks_response = client.get("/seo/fix-center/tasks")
    assert tasks_response.status_code == 200
    tasks_payload = tasks_response.json()
    issue_types = {task["issue_type"] for task in tasks_payload["tasks"]}
    assert "missing_meta_description" in issue_types
    assert "missing_h1" in issue_types
    assert "image_missing_alt" in issue_types
    assert "duplicate_meta_title" in issue_types
    assert "duplicate_h1" in issue_types
    assert "redirect_chain" in issue_types
    assert "product_seo_issue" in issue_types
    assert "gsc_404" in issue_types
    first_task = tasks_payload["tasks"][0]
    assert first_task["title"]
    assert first_task["explanation"]
    assert first_task["why_it_matters"]
    assert first_task["recommended_fix"]
    assert first_task["difficulty"] in {"קל", "בינוני", "מתקדם"}
    assert first_task["risk_level"] in {"נמוך", "בינוני", "גבוה"}
    assert first_task["estimated_impact"] in {"גבוה", "בינוני", "נמוך"}
    assert tasks_payload["safety"] == {
        "auto_publish": False,
        "auto_content_edits": False,
        "approval_required": True,
        "high_risk_double_confirmation": True,
    }


def test_fix_center_pages_render_hebrew_filters_tooltips_and_summary(client: TestClient, db_session: Session) -> None:
    _seed_fix_center_crawl(db_session)

    fix_center_response = client.get("/seo/fix-center")
    dashboard_response = client.get("/seo/fixes/dashboard")

    assert fix_center_response.status_code == 200
    html = fix_center_response.text
    assert '<html lang="he" dir="rtl">' in html
    assert "מרכז תיקוני SEO ידידותי לעובדים" in html
    assert "מה כדאי לעשות עכשיו?" in html
    assert "סיכום יומי" in html
    assert "חומרה" in html
    assert "סוג בעיה" in html
    assert "סטטוס" in html
    assert "עמוד" in html
    assert "קלות ביצוע" in html
    assert "בדוק" in html
    assert "הצג פרטים" in html
    assert "אשר תיקון" in html
    assert "דחה" in html
    assert "סמן כבוצע" in html
    assert "אין פרסום אוטומטי" in html
    assert "missing_meta_description" in html
    assert "Meta description הוא תקציר" in html
    assert dashboard_response.status_code == 200
    assert "What should I do now?" in dashboard_response.text
    assert "New issues" in dashboard_response.text
    assert "Fixed today" in dashboard_response.text
    assert "Waiting for approval" in dashboard_response.text
    assert "High priority open" in dashboard_response.text



def _field_values(task: dict[str, object]) -> dict[str, str]:
    solution = task["copyable_solution"]
    assert isinstance(solution, dict)
    return {str(field["key"]): str(field["value"]) for field in solution["fields"]}  # type: ignore[index]


def test_fix_center_scan_route_and_ready_to_copy_solutions(client: TestClient, db_session: Session) -> None:
    _seed_fix_center_crawl(db_session)

    post_response = client.post("/seo/fix-center/scan")
    get_response = client.get("/seo/fix-center/scan")

    assert post_response.status_code == 201
    assert get_response.status_code == 200
    tasks = client.get("/seo/fix-center/tasks").json()["tasks"]

    product_task = next(task for task in tasks if task["issue_type"] == "product_seo_issue")
    product_fields = _field_values(product_task)
    assert product_fields["suggested_meta_title"]
    assert product_fields["suggested_meta_description"]
    assert product_task["manual_notice"] == "יש להעתיק ידנית לאתר ISTORE לאחר בדיקה."

    image_task = next(task for task in tasks if task["issue_type"] == "image_missing_alt")
    image_fields = _field_values(image_task)
    assert image_fields["suggested_alt"]

    broken_task = next(task for task in tasks if task["issue_type"] == "broken_link")
    broken_fields = _field_values(broken_task)
    assert broken_fields["suggested_replacement_url"]


def test_fix_center_ui_and_command_center_render_copyable_manual_workflows(
    client: TestClient, db_session: Session
) -> None:
    _seed_fix_center_crawl(db_session)

    fix_center_response = client.get("/seo/fix-center")
    command_center_response = client.get("/seo/command-center")

    assert fix_center_response.status_code == 200
    assert "פתרון מוכן להעתקה" in fix_center_response.text
    assert "יש להעתיק ידנית לאתר ISTORE לאחר בדיקה." in fix_center_response.text
    assert "פתח עמוד באתר" in fix_center_response.text
    assert "data-copy-target" in fix_center_response.text
    assert command_center_response.status_code == 200
    assert "SEO Command Center" in command_center_response.text
    assert "GSC SEO Tasks" in command_center_response.text
    assert "Article Drafts" in command_center_response.text
    assert "SEO Fix Center" in command_center_response.text
    assert "Manual iStore Publishing Queue" in command_center_response.text
    assert "Daily top priorities" in command_center_response.text

def test_fix_center_workflow_requires_double_confirmation_for_high_risk(
    client: TestClient, db_session: Session
) -> None:
    _seed_fix_center_crawl(db_session)
    client.post("/seo/fix-center/scan")
    tasks = client.get("/seo/fix-center/tasks").json()["tasks"]
    high_risk_task = next(task for task in tasks if task["requires_double_confirmation"])

    check_response = client.post(f"/seo/fix-center/{high_risk_task['id']}/check")
    assert check_response.status_code == 200
    assert check_response.json()["task"]["status"] == "בבדיקה"

    approve_response = client.post(f"/seo/fix-center/{high_risk_task['id']}/approve", json={"double_confirm": False})
    assert approve_response.status_code == 409

    confirmed_response = client.post(f"/seo/fix-center/{high_risk_task['id']}/approve", json={"double_confirm": True})
    assert confirmed_response.status_code == 200
    assert confirmed_response.json()["task"]["status"] == "אושר"
    assert confirmed_response.json()["auto_published"] is False


def test_fix_center_safe_one_click_is_low_risk_only_and_never_publishes(
    client: TestClient, db_session: Session
) -> None:
    _seed_fix_center_crawl(db_session)
    client.post("/seo/fix-center/scan")
    tasks = client.get("/seo/fix-center/tasks").json()["tasks"]
    safe_task = next(task for task in tasks if task["issue_type"] == "image_missing_alt")
    unsafe_task = next(task for task in tasks if task["risk_level"] != "נמוך")

    safe_response = client.post(f"/seo/fix-center/{safe_task['id']}/safe-fix")
    assert safe_response.status_code == 200
    assert safe_response.json()["task"]["status"] == "ממתין לאישור"
    assert safe_response.json()["auto_published"] is False
    assert "לא פורסם" in safe_response.json()["message"]

    unsafe_response = client.post(f"/seo/fix-center/{unsafe_task['id']}/safe-fix")
    assert unsafe_response.status_code == 403
