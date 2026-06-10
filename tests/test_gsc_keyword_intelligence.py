from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, GSCKeywordMetric, PageAudit, SEOTask
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
) -> GSCKeywordMetric:
    metric = GSCKeywordMetric(
        page_url=page_url,
        query=query,
        clicks=clicks,
        impressions=impressions,
        ctr=ctr,
        average_position=average_position,
        date=date(2026, 5, 1),
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
    assert response.json() == {"success": False, "rows_synced": 0, "top_queries": [], "error": "credentials missing"}


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
