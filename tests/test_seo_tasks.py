import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, PageAudit, SEOTask
from app.integrations.openai_client import OpenAIClient
from app.main import app


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_create_seo_tasks_from_latest_crawl_uses_low_score_and_missing_basics(
    client: TestClient, db_session: Session
) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=3, average_score=72)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/missing-title",
                status_code=200,
                missing_fields="title,meta_description",
                seo_score=82,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/low-score",
                status_code=200,
                title="Low score page",
                h1="Low score page",
                meta_description="Existing description",
                missing_fields="",
                seo_score=62,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/healthy",
                status_code=200,
                title="Healthy page",
                h1="Healthy page",
                meta_description="Healthy description",
                missing_fields="",
                seo_score=91,
            ),
        ]
    )
    db_session.commit()

    response = client.post("/seo/tasks/from-latest-crawl")

    assert response.status_code == 201
    assert response.json() == {"created_count": 2, "total_candidates": 2}
    assert db_session.query(SEOTask).count() == 2


def test_create_seo_tasks_from_latest_crawl_avoids_duplicate_page_urls(client: TestClient, db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=1, average_score=40)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add(
        PageAudit(
            crawl_run_id=crawl_run.id,
            url="https://example.com/duplicate",
            status_code=200,
            missing_fields="h1",
            seo_score=40,
        )
    )
    db_session.add(SEOTask(page_url="https://example.com/duplicate", priority="high", status="open"))
    db_session.commit()

    response = client.post("/seo/tasks/from-latest-crawl")

    assert response.status_code == 201
    assert response.json() == {"created_count": 0, "total_candidates": 1}
    assert db_session.query(SEOTask).count() == 1


def test_list_seo_tasks_returns_saved_tasks(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/task",
            keyword="example keyword",
            priority="medium",
            status="open",
            suggested_title="Suggested title",
            suggested_h1="Suggested H1",
            meta_description="Suggested meta description",
            recommendation_json='{"recommendations": []}',
        )
    )
    db_session.commit()

    response = client.get("/seo/tasks")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["page_url"] == "https://example.com/task"


def test_openai_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.integrations.openai_client.settings.openai_api_key", None)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not configured"):
        OpenAIClient()


def test_generate_seo_recommendation_missing_task_returns_404(client: TestClient) -> None:
    response = client.post("/seo/tasks/999/generate-recommendation")

    assert response.status_code == 404
    assert response.json()["detail"] == "SEO task not found"


def test_generate_seo_recommendation_missing_openai_api_key_returns_clear_error(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(SEOTask(page_url="https://example.com/no-key", priority="high", status="open"))
    db_session.commit()
    task = db_session.query(SEOTask).one()
    monkeypatch.setattr("app.integrations.openai_client.settings.openai_api_key", None)

    response = client.post(f"/seo/tasks/{task.id}/generate-recommendation")

    assert response.status_code == 503
    assert "OPENAI_API_KEY is not configured" in response.json()["detail"]


def test_generate_seo_recommendation_updates_task_with_mocked_openai_response(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/mock-success",
            keyword="existing keyword",
            priority="high",
            status="open",
            suggested_title="Old title",
            suggested_h1="Old H1",
            meta_description="Old meta description",
            recommendation_json='{"source": "test"}',
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()
    generated_recommendation = {
        "suggested_title": "New SEO title",
        "suggested_h1": "New SEO H1",
        "meta_description": "New SEO meta description",
        "primary_keyword": "primary keyword",
        "secondary_keywords": ["secondary one", "secondary two"],
        "content_recommendations": ["Expand service details."],
        "technical_recommendations": ["Add canonical tag."],
        "internal_link_ideas": ["Link from the homepage."],
        "priority_reason": "High-value page with missing metadata.",
    }
    captured_payload = {}

    class MockOpenAIClient:
        def generate_seo_recommendation(self, page: dict) -> dict:
            captured_payload.update(page)
            return generated_recommendation

    monkeypatch.setattr("app.api.routes.OpenAIClient", MockOpenAIClient)

    response = client.post(f"/seo/tasks/{task.id}/generate-recommendation")

    assert response.status_code == 200
    assert response.json() == {"success": True, "task_id": task.id, "recommendation": generated_recommendation}
    db_session.refresh(task)
    assert captured_payload["task_id"] == task.id
    assert captured_payload["page_url"] == "https://example.com/mock-success"
    assert task.status == "recommended"
    assert task.suggested_title == "New SEO title"
    assert task.suggested_h1 == "New SEO H1"
    assert task.meta_description == "New SEO meta description"
    assert task.recommendation_json == json.dumps(generated_recommendation, ensure_ascii=False)


def test_list_seo_tasks_shows_updated_recommendation_and_status(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(SEOTask(page_url="https://example.com/list-updated", priority="medium", status="open"))
    db_session.commit()
    task = db_session.query(SEOTask).one()
    generated_recommendation = {
        "suggested_title": "Listed SEO title",
        "suggested_h1": "Listed SEO H1",
        "meta_description": "Listed SEO meta description",
        "primary_keyword": "listed keyword",
        "secondary_keywords": [],
        "content_recommendations": [],
        "technical_recommendations": [],
        "internal_link_ideas": [],
        "priority_reason": "Ready to publish.",
    }

    class MockOpenAIClient:
        def generate_seo_recommendation(self, page: dict) -> dict:
            return generated_recommendation

    monkeypatch.setattr("app.api.routes.OpenAIClient", MockOpenAIClient)

    generate_response = client.post(f"/seo/tasks/{task.id}/generate-recommendation")
    response = client.get("/seo/tasks")

    assert generate_response.status_code == 200
    assert response.status_code == 200
    [listed_task] = response.json()["tasks"]
    assert listed_task["id"] == task.id
    assert listed_task["status"] == "recommended"
    assert listed_task["suggested_title"] == "Listed SEO title"
    assert listed_task["suggested_h1"] == "Listed SEO H1"
    assert listed_task["meta_description"] == "Listed SEO meta description"
    assert listed_task["recommendation_json"] == json.dumps(generated_recommendation, ensure_ascii=False)
