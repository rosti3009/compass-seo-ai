from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import ContentArticleDraft, CrawlRun, GSCKeywordMetric, PageAudit, SEOTask
from app.main import app


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


def add_metric(
    db_session: Session,
    *,
    page_url: str = "https://example.com/page",
    query: str = "example keyword",
    impressions: int = 1000,
    clicks: int = 10,
    ctr: float = 0.01,
    average_position: float = 7.5,
    metric_date: date = date(2026, 5, 1),
) -> GSCKeywordMetric:
    metric = GSCKeywordMetric(
        page_url=page_url,
        query=query,
        clicks=clicks,
        impressions=impressions,
        ctr=ctr,
        average_position=average_position,
        date=metric_date,
        source="gsc",
    )
    db_session.add(metric)
    db_session.commit()
    return metric


def test_gsc_sync_upserts_mocked_rows(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    class MockGSCClient:
        site_url = "sc-domain:example.com"

        @classmethod
        def from_settings(cls, db: Session | None = None) -> "MockGSCClient":
            assert db is not None
            return cls()

        def fetch_top_queries(self, site_url: str, limit: int = 250) -> list[dict[str, object]]:
            assert site_url == "sc-domain:example.com"
            assert limit == 250
            return [
                {
                    "page_url": "https://example.com/page",
                    "query": "first keyword",
                    "clicks": 3,
                    "impressions": 300,
                    "ctr": 0.01,
                    "average_position": 8.0,
                    "date": "2026-05-01",
                    "source": "gsc",
                }
            ]

    monkeypatch.setattr("app.api.routes.GSCClient", MockGSCClient)

    response = client.post("/gsc/sync")
    second_response = client.post("/gsc/sync")

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["rows_synced"] == 1
    assert second_response.json()["rows_synced"] == 1
    assert db_session.query(GSCKeywordMetric).count() == 1
    metric = db_session.query(GSCKeywordMetric).one()
    assert metric.query == "first keyword"
    assert metric.impressions == 300


@pytest.mark.parametrize("configured_site_url", ["sc-domain:compassgrill.co.il", "https://compassgrill.co.il/"])
def test_manual_live_gsc_sync_imports_last_30_days_and_returns_top_queries_and_pages(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch, configured_site_url: str
) -> None:
    class MockGSCClient:
        @classmethod
        def from_settings(cls, db: Session | None = None) -> "MockGSCClient":
            assert db is not None
            return cls()

        def fetch_query_page_date_rows(
            self, site_url: str, *, start_date: date, end_date: date, limit: int
        ) -> list[dict[str, object]]:
            assert site_url == configured_site_url
            assert (end_date - start_date).days == 29
            assert limit == 25000
            return [
                {
                    "page_url": "https://compassgrill.co.il/grill",
                    "query": "גריל גז",
                    "clicks": 4,
                    "impressions": 400,
                    "ctr": 0.01,
                    "average_position": 6.0,
                    "date": start_date.isoformat(),
                    "source": "gsc",
                },
                {
                    "page_url": "https://compassgrill.co.il/smoker",
                    "query": "מעשנה",
                    "clicks": 3,
                    "impressions": 300,
                    "ctr": 0.01,
                    "average_position": 8.0,
                    "date": end_date.isoformat(),
                    "source": "gsc",
                },
                {
                    "page_url": "https://compassgrill.co.il/grill",
                    "query": "גריל גז",
                    "clicks": 2,
                    "impressions": 200,
                    "ctr": 0.01,
                    "average_position": 7.0,
                    "date": end_date.isoformat(),
                    "source": "gsc",
                },
            ]

    monkeypatch.setattr("app.api.routes.GSCClient", MockGSCClient)
    monkeypatch.setattr("app.api.routes.settings.gsc_site_url", configured_site_url)
    monkeypatch.setattr("app.api.routes.settings.manual_action_token", "manual-token")

    diagnostics_response = client.get("/integrations/google/diagnostics")
    assert diagnostics_response.status_code == 200
    assert diagnostics_response.json()["gsc_site_url_configured"] is True
    assert diagnostics_response.json()["gsc_site_url_value_redacted"] is not None

    response = client.post(
        "/gsc/manual-sync",
        json={"confirmation": f"SYNC {configured_site_url}"},
        headers={"X-Manual-Action-Token": "manual-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["site_url"] == configured_site_url
    assert payload["date_range"]["days"] == 30
    assert payload["rows_imported"] == 3
    assert payload["top_queries"][0]["query"] == "גריל גז"
    assert payload["top_queries"][0]["impressions"] == 600
    assert payload["top_pages"][0]["page_url"] == "https://compassgrill.co.il/grill"
    assert payload["top_pages"][0]["impressions"] == 600
    assert db_session.query(GSCKeywordMetric).count() == 3


@pytest.mark.parametrize("configured_site_url", ["sc-domain:compassgrill.co.il", "https://compassgrill.co.il/"])
def test_manual_live_gsc_sync_requires_runtime_site_url_confirmation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, configured_site_url: str
) -> None:
    monkeypatch.setattr("app.api.routes.settings.gsc_site_url", configured_site_url)

    response = client.post("/gsc/manual-sync", json={"confirmation": "wrong"})

    assert response.status_code == 400
    assert f"SYNC {configured_site_url}" in response.json()["detail"]


def test_manual_live_gsc_sync_requires_token_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.routes.settings.gsc_site_url", "https://compassgrill.co.il/")
    monkeypatch.setattr("app.api.routes.settings.manual_action_token", "manual-token")

    response = client.post("/gsc/manual-sync", json={"confirmation": "SYNC https://compassgrill.co.il/"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid manual action token."


def test_gsc_runtime_diagnostics_returns_non_secret_booleans(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MockGSCClient:
        @classmethod
        def from_settings(cls, db: Session | None = None) -> "MockGSCClient":
            assert db is not None
            return cls()

        def _service(self) -> object:
            return object()

    monkeypatch.setattr("app.api.routes.settings.gsc_site_url", "sc-domain:example.com")
    monkeypatch.setattr("app.api.routes.settings.google_application_credentials_json", "{}")
    monkeypatch.setattr("app.api.routes.settings.google_oauth_client_id", None)
    monkeypatch.setattr("app.api.routes.resolve_google_credentials", lambda db, scopes: object())
    monkeypatch.setattr("app.api.routes.GSCClient", MockGSCClient)

    response = client.get("/diagnostics/gsc-runtime")

    assert response.status_code == 200
    assert response.json() == {
        "gsc_site_url_configured": True,
        "google_application_credentials_json_configured": True,
        "google_oauth_client_id_configured": False,
        "credential_resolution_success": True,
        "search_console_client_created": True,
    }


def test_gsc_sync_handles_missing_credentials_gracefully(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class MockGSCClient:
        @classmethod
        def from_settings(cls, db: Session | None = None) -> "MockGSCClient":
            assert db is not None
            raise RuntimeError("credentials missing")

    monkeypatch.setattr("app.api.routes.GSCClient", MockGSCClient)

    response = client.post("/gsc/sync")

    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "rows_synced": 0,
        "top_queries": [],
        "top_pages": [],
        "error": "credentials missing",
    }


def test_gsc_keywords_filters(client: TestClient, db_session: Session) -> None:
    add_metric(db_session, query="commercial grill", impressions=1000, average_position=6.0, ctr=0.01)
    add_metric(
        db_session,
        page_url="https://example.com/other",
        query="other query",
        impressions=20,
        average_position=30.0,
        ctr=0.20,
    )

    response = client.get("/gsc/keywords?query=grill&min_impressions=100&max_position=10&low_ctr_only=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["keywords"]) == 1
    assert payload["keywords"][0]["query"] == "commercial grill"


def test_gsc_opportunities_endpoint(client: TestClient, db_session: Session) -> None:
    add_metric(db_session, query="low ctr keyword", impressions=500, ctr=0.005, average_position=5.0)
    add_metric(db_session, query="healthy keyword", impressions=500, ctr=0.08, average_position=3.0)

    response = client.get("/gsc/opportunities")

    assert response.status_code == 200
    opportunities = response.json()["opportunities"]
    assert len(opportunities) == 1
    assert opportunities[0]["query"] == "low ctr keyword"
    assert (
        "internal links" in opportunities[0]["recommended_action"].lower()
        or "ctr" in opportunities[0]["recommended_action"].lower()
    )


def test_gsc_dashboard_views_render(client: TestClient, db_session: Session) -> None:
    add_metric(db_session, query="dashboard keyword", impressions=900, ctr=0.01, average_position=9.0)

    keywords_response = client.get("/gsc/keywords-view")
    opportunities_response = client.get("/gsc/opportunities-view")

    assert keywords_response.status_code == 200
    assert "dashboard keyword" in keywords_response.text
    assert opportunities_response.status_code == 200
    assert "dashboard keyword" in opportunities_response.text


def test_gsc_opportunities_dashboard_uses_imported_metrics_and_hebrew_json(
    client: TestClient, db_session: Session
) -> None:
    add_metric(
        db_session,
        page_url="https://example.com/grill",
        query="גריל גז",
        impressions=1200,
        clicks=12,
        ctr=0.01,
        average_position=6.0,
        metric_date=date(2026, 5, 1),
    )
    add_metric(
        db_session,
        page_url="https://example.com/grill",
        query="גריל מומלץ",
        impressions=300,
        clicks=9,
        ctr=0.03,
        average_position=4.0,
        metric_date=date(2026, 5, 1),
    )
    add_metric(
        db_session,
        page_url="https://example.com/smoker",
        query="מעשנה",
        impressions=900,
        clicks=45,
        ctr=0.05,
        average_position=3.0,
        metric_date=date(2026, 5, 1),
    )
    add_metric(
        db_session,
        page_url="https://example.com/declining",
        query="declining grill",
        impressions=1000,
        clicks=40,
        ctr=0.04,
        average_position=2.0,
        metric_date=date(2026, 4, 1),
    )
    add_metric(
        db_session,
        page_url="https://example.com/declining",
        query="declining grill",
        impressions=100,
        clicks=3,
        ctr=0.03,
        average_position=8.0,
        metric_date=date(2026, 5, 1),
    )

    response = client.get("/gsc/opportunities-dashboard")

    assert response.status_code == 200
    assert "charset=utf-8" in response.headers["content-type"]
    assert "גריל גז" in response.text
    assert "\\u05d2" not in response.text
    payload = response.json()
    assert payload["top_queries_by_impressions"][0]["query"] == "גריל גז"
    assert payload["top_queries_by_clicks"][0]["query"] == "מעשנה"
    assert payload["low_ctr_mid_position_queries"][0]["query"] == "גריל גז"
    assert payload["top_pages_by_impressions"][0]["page_url"] == "https://example.com/grill"
    assert payload["declining_pages"][0]["page_url"] == "https://example.com/declining"
    assert payload["article_recommendations"][0]["primary_query"] == "גריל גז"


def test_gsc_opportunities_dashboard_view_renders_all_sections(client: TestClient, db_session: Session) -> None:
    add_metric(db_session, query="dashboard keyword", impressions=900, ctr=0.01, average_position=9.0)

    response = client.get("/gsc/opportunities-view")

    assert response.status_code == 200
    assert "Top queries by impressions" in response.text
    assert "Top queries by clicks" in response.text
    assert "Top pages by impressions" in response.text
    assert "Declining pages compared to previous period" in response.text
    assert "Article recommendations based on GSC queries" in response.text
    assert "Hebrew API and PowerShell output" in response.text
    assert "dashboard keyword" in response.text


def test_seo_tasks_are_enriched_with_gsc_keyword_opportunity(client: TestClient, db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=1, average_score=82)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add(
        PageAudit(
            crawl_run_id=crawl_run.id,
            url="https://example.com/page",
            status_code=200,
            title="Existing title",
            h1="Existing H1",
            meta_description="Existing description",
            missing_fields="",
            seo_score=82,
        )
    )
    add_metric(db_session, query="gsc primary keyword", impressions=2000, ctr=0.001, average_position=6.0)

    response = client.post("/seo/tasks/from-latest-crawl")

    assert response.status_code == 201
    assert response.json() == {"created_count": 1, "total_candidates": 1}
    task = db_session.query(SEOTask).one()
    assert task.keyword == "gsc primary keyword"
    assert task.priority == "high"
    assert "keyword_opportunity_score" in task.recommendation_json


def test_gsc_seo_tasks_group_duplicate_metrics_and_classify(client: TestClient, db_session: Session) -> None:
    add_metric(
        db_session,
        page_url="https://example.com/blog/brisket-oven",
        query="בריסקט בתנור",
        clicks=4,
        impressions=400,
        ctr=0.01,
        average_position=6.0,
        metric_date=date(2026, 5, 1),
    )
    add_metric(
        db_session,
        page_url="https://example.com/blog/brisket-oven",
        query="בריסקט בתנור",
        clicks=2,
        impressions=200,
        ctr=0.01,
        average_position=8.0,
        metric_date=date(2026, 5, 2),
    )
    add_metric(
        db_session,
        page_url="https://example.com/product/pizza-stone",
        query="pizza stone",
        clicks=10,
        impressions=600,
        ctr=0.016,
        average_position=5.0,
    )
    add_metric(db_session, query="too few impressions", impressions=20, clicks=0, ctr=0.0, average_position=9.0)

    response = client.get("/gsc/seo-tasks")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["task_count"] == 2
    brisket = next(task for task in payload["tasks"] if task["query"] == "בריסקט בתנור")
    assert brisket["impressions"] == 600
    assert brisket["clicks"] == 6
    assert brisket["average_position"] == pytest.approx(6.666, rel=0.01)
    assert brisket["priority"] == "high"
    assert brisket["task_type"] in {"meta_title_update", "refresh_existing_article"}
    assert brisket["source"] == "gsc"
    assert db_session.query(SEOTask).count() == 2

    second_response = client.get("/gsc/seo-tasks")
    assert second_response.status_code == 200
    assert second_response.json()["created_count"] == 0
    assert db_session.query(SEOTask).count() == 2


def test_gsc_seo_task_generation_creates_copy_paste_article(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_metric(db_session, query="בריסקט", impressions=800, clicks=8, ctr=0.01, average_position=7.0)
    task_id = client.get("/gsc/seo-tasks").json()["tasks"][0]["task_id"]

    def fake_generate_topic_article_draft(
        db: Session,
        *,
        topic_title: str,
        focus_keyword: str,
        target_intent: str,
        preferred_slug: str | None = None,
    ):
        draft = ContentArticleDraft(
            status="READY_FOR_REVIEW",
            topic_title=topic_title,
            title=topic_title,
            slug=preferred_slug or "brisket-guide",
            meta_title=f"{topic_title} | Compass Grill",
            meta_description="מדריך בריסקט מלא עם טיפים לגריל ולתנור.",
            focus_keyword=focus_keyword,
            target_intent=target_intent,
            article_body="<h2>בריסקט</h2><p>מדריך מעשי להכנת בריסקט.</p>",
            faq_schema_json=(
                '{"mainEntity":[{"name":"איך מכינים בריסקט?","acceptedAnswer":{"text":"לאט ובחום נמוך."}}]}'
            ),
            featured_image_prompt="Hero brisket image",
            section_image_prompts_json='["Trim brisket", "Season brisket", "Slice brisket"]',
            image_alt_text="בריסקט מוכן להגשה",
            image_title="תמונת בריסקט",
            image_caption="בריסקט עסיסי",
            image_filename_slug="compass-grill-brisket-guide",
            image_style_rules="realistic",
            target_site_section="blog",
            target_publish_type="article",
            target_blog_base_url="https://compassgrill.co.il/blog/",
            target_path="/blog/brisket-guide",
            target_url="https://compassgrill.co.il/blog/brisket-guide",
            publish_destination_status="ready",
            featured_image_status="planned",
        )
        db.add(draft)
        db.commit()
        db.refresh(draft)
        return draft

    monkeypatch.setattr("app.api.routes.generate_topic_article_draft", fake_generate_topic_article_draft)
    monkeypatch.setattr("app.api.routes._content_quality_gate_passed", lambda draft: True)

    response = client.post(f"/gsc/seo-tasks/{task_id}/generate-article")

    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_publish"] is False
    assert payload["task"]["status"] == "draft_generated"
    assert payload["article"]["hebrew_article_title"].startswith("מדריך")
    assert payload["article"]["primary_keyword"] == "בריסקט"
    assert len(payload["article"]["image_plan"]) == 4
    assert payload["article"]["copy_paste_mode"]["mode"] == "copy_paste"

    approve_response = client.post(f"/gsc/seo-tasks/{task_id}/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["task"]["status"] == "approved"

    publish_response = client.post(f"/gsc/seo-tasks/{task_id}/publish")
    assert publish_response.status_code == 403
    assert publish_response.json()["detail"] == "ISTORE_PUBLISH_ENABLED=true is required"


def test_gsc_seo_tasks_dashboard_renders_actions(client: TestClient, db_session: Session) -> None:
    add_metric(db_session, query="פילה בקר", impressions=500, clicks=5, ctr=0.01, average_position=9.0)

    response = client.get("/gsc/seo-tasks/dashboard")

    assert response.status_code == 200
    assert "פילה בקר" in response.text
    assert "Generate article" in response.text
    assert "Approve" in response.text
    assert "Publish" in response.text
    assert "Reject" in response.text
