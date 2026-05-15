from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, PageAudit, SEOAutomationRun, SEOFix, SEOTask
from app.integrations.gsc import MissingGoogleCredentialsError
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


class MockCrawler:
    def __init__(self, target_domain: str, max_pages: int) -> None:
        self.target_domain = target_domain
        self.max_pages = max_pages

    def run(self, db: Session) -> tuple[CrawlRun, list[PageAudit]]:
        crawl_run = CrawlRun(target_domain=self.target_domain, status="completed", pages_crawled=3, average_score=45)
        db.add(crawl_run)
        db.flush()
        pages = [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/high-one",
                status_code=200,
                title="High One",
                h1="High One",
                meta_description="Meta one",
                missing_fields="title",
                seo_score=40,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/high-two",
                status_code=200,
                title="High Two",
                h1="High Two",
                meta_description="Meta two",
                missing_fields="h1",
                seo_score=45,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/medium",
                status_code=200,
                title="Medium",
                h1="Medium",
                meta_description="Meta medium",
                missing_fields="meta_description",
                seo_score=65,
            ),
        ]
        db.add_all(pages)
        db.commit()
        for page in pages:
            db.refresh(page)
        db.refresh(crawl_run)
        return crawl_run, pages


class MockOpenAIClient:
    def generate_seo_recommendation(self, page: dict) -> dict:
        return {
            "suggested_title": f"SEO title {page['task_id']}",
            "suggested_h1": f"SEO H1 {page['task_id']}",
            "meta_description": f"SEO meta {page['task_id']}",
            "primary_keyword": "automation keyword",
            "secondary_keywords": [],
            "content_recommendations": ["Improve copy."],
            "technical_recommendations": [],
            "internal_link_ideas": [],
            "priority_reason": "High-priority automation task.",
        }

    def generate_full_article(self, task: dict) -> dict:
        return {
            "article_title": f"Article {task['task_id']}",
            "article_html": "<article><p>Generated article.</p></article>",
            "faq": [],
            "faq_schema_json": {},
            "article_schema_json": {},
            "meta_title": f"Article title {task['task_id']}",
            "meta_description": f"Article meta {task['task_id']}",
            "slug_suggestion": "article",
        }


def patch_automation_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.seo_automation.SEOCrawler", MockCrawler)
    monkeypatch.setattr("app.services.seo_automation.OpenAIClient", MockOpenAIClient)
    monkeypatch.setattr(
        "app.services.seo_automation.generate_strategy_recommendations", lambda db: {"created_count": 2}
    )


class MissingGSCClient:
    @classmethod
    def from_settings(cls, db: Session) -> "MissingGSCClient":
        raise MissingGoogleCredentialsError("GSC credentials missing")


def test_automation_run_creates_record(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_automation_dependencies(monkeypatch)
    monkeypatch.setattr("app.services.seo_automation.GSCClient", MissingGSCClient)

    response = client.post("/seo/automation/run?sync_gsc=false")

    assert response.status_code == 201
    payload = response.json()["run"]
    assert payload["status"] == "completed"
    assert payload["seo_tasks_created"] == 3
    assert payload["recommendations_generated"] == 2
    assert db_session.query(SEOAutomationRun).count() == 1


def test_automation_handles_missing_gsc_gracefully(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_automation_dependencies(monkeypatch)
    monkeypatch.setattr("app.services.seo_automation.GSCClient", MissingGSCClient)

    response = client.post("/seo/automation/run")

    assert response.status_code == 201
    payload = response.json()["run"]
    assert payload["status"] == "completed_with_warnings"
    assert payload["gsc_synced_rows"] == 0
    assert any(error["step"] == "gsc_sync" for error in payload["errors"])
    assert db_session.query(SEOAutomationRun).count() == 1


def test_automation_handles_missing_openai_gracefully(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.seo_automation.SEOCrawler", MockCrawler)
    monkeypatch.setattr("app.services.seo_automation.GSCClient", MissingGSCClient)
    monkeypatch.setattr(
        "app.services.seo_automation.generate_strategy_recommendations", lambda db: {"created_count": 1}
    )

    class MissingOpenAIClient:
        def __init__(self) -> None:
            raise RuntimeError("OPENAI_API_KEY is not configured")

    monkeypatch.setattr("app.services.seo_automation.OpenAIClient", MissingOpenAIClient)

    response = client.post("/seo/automation/run?sync_gsc=false")

    assert response.status_code == 201
    payload = response.json()["run"]
    assert payload["status"] == "completed_with_warnings"
    assert payload["recommendations_generated"] == 0
    assert any(error["step"] == "openai" for error in payload["errors"])
    assert db_session.query(SEOAutomationRun).count() == 1


def test_automation_respects_max_tasks(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_automation_dependencies(monkeypatch)
    monkeypatch.setattr("app.services.seo_automation.GSCClient", MissingGSCClient)

    response = client.post("/seo/automation/run?max_tasks=1&sync_gsc=false")

    assert response.status_code == 201
    payload = response.json()["run"]
    assert payload["seo_tasks_created"] == 1
    assert payload["recommendations_generated"] == 1
    assert db_session.query(SEOTask).count() == 1


def test_automation_does_not_auto_approve_fixes(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_automation_dependencies(monkeypatch)
    monkeypatch.setattr("app.services.seo_automation.GSCClient", MissingGSCClient)

    response = client.post("/seo/automation/run?sync_gsc=false")

    assert response.status_code == 201
    fixes = db_session.query(SEOFix).all()
    assert fixes
    assert {fix.status for fix in fixes} == {"draft"}
    assert response.json()["run"]["publishing_packages_created"] == 0


def test_automation_dashboard_view_loads(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOAutomationRun(status="completed", seo_tasks_created=1, fixes_created=2))
    db_session.commit()

    response = client.get("/seo/automation-view")

    assert response.status_code == 200
    assert "SEO Automation" in response.text
    assert "Last automation runs" in response.text


def test_automation_run_details_endpoint_works(client: TestClient, db_session: Session) -> None:
    run = SEOAutomationRun(status="completed", seo_tasks_created=1, summary_json='{"example": true}')
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    response = client.get(f"/seo/automation/runs/{run.id}")

    assert response.status_code == 200
    assert response.json()["run"]["id"] == run.id
    assert response.json()["run"]["summary"] == {"example": True}


class SystemPageCrawler:
    def __init__(self, target_domain: str, max_pages: int) -> None:
        self.target_domain = target_domain
        self.max_pages = max_pages

    def run(self, db: Session) -> tuple[CrawlRun, list[PageAudit]]:
        crawl_run = CrawlRun(target_domain=self.target_domain, status="completed", pages_crawled=1, average_score=20)
        db.add(crawl_run)
        db.flush()
        page = PageAudit(
            crawl_run_id=crawl_run.id,
            url="https://example.com/account",
            status_code=200,
            title="Account",
            h1="Account",
            meta_description="",
            missing_fields="meta_description",
            seo_score=20,
        )
        db.add(page)
        db.commit()
        db.refresh(page)
        db.refresh(crawl_run)
        return crawl_run, [page]


def test_automation_skips_system_urls(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.seo_automation.SEOCrawler", SystemPageCrawler)
    monkeypatch.setattr("app.services.seo_automation.GSCClient", MissingGSCClient)
    monkeypatch.setattr("app.services.seo_automation.OpenAIClient", MockOpenAIClient)
    monkeypatch.setattr(
        "app.services.seo_automation.generate_strategy_recommendations", lambda db: {"created_count": 0}
    )

    response = client.post("/seo/automation/run?sync_gsc=false")

    assert response.status_code == 201
    payload = response.json()["run"]
    assert payload["seo_tasks_created"] == 0
    assert payload["recommendations_generated"] == 0
    assert db_session.query(SEOTask).count() == 0
