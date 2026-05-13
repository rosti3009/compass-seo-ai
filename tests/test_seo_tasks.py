import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, PageAudit, PublishingPackage, SEOFix, SEOTask
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


def test_generate_seo_article_missing_task_returns_404(client: TestClient) -> None:
    response = client.post("/seo/tasks/999/generate-article")

    assert response.status_code == 404
    assert response.json()["detail"] == "SEO task not found"


def test_generate_seo_article_without_recommendation_returns_400(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/no-recommendation", priority="medium", status="open"))
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.post(f"/seo/tasks/{task.id}/generate-article")

    assert response.status_code == 400
    assert response.json()["detail"] == "SEO task recommendation is required"


def test_generate_seo_article_saves_mocked_article(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/article-source",
            keyword="grill compass",
            priority="high",
            status="recommended",
            suggested_title="Existing SEO title",
            recommendation_json=json.dumps({"content_recommendations": ["Write a full buyer guide."]}),
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()
    generated_article = {
        "article_title": "Complete Grill Compass Guide",
        "article_html": "<article><h1>Complete Grill Compass Guide</h1><p>Helpful article.</p></article>",
        "faq": [{"question": "What is Grill Compass?", "answer": "A grill planning resource."}],
        "faq_schema_json": {"@type": "FAQPage"},
        "article_schema_json": {"@type": "Article", "headline": "Complete Grill Compass Guide"},
        "meta_title": "Complete Grill Compass Guide",
        "meta_description": "Plan better grilling with this complete guide.",
        "slug_suggestion": "complete-grill-compass-guide",
    }
    captured_payload = {}

    class MockOpenAIClient:
        def generate_full_article(self, task: dict) -> dict:
            captured_payload.update(task)
            return generated_article

    monkeypatch.setattr("app.api.routes.OpenAIClient", MockOpenAIClient)

    response = client.post(f"/seo/tasks/{task.id}/generate-article")

    assert response.status_code == 200
    assert response.json() == {"success": True, "task_id": task.id, "article": generated_article}
    db_session.refresh(task)
    assert captured_payload["task_id"] == task.id
    assert captured_payload["recommendation"] == {"content_recommendations": ["Write a full buyer guide."]}
    assert task.article_html == generated_article["article_html"]
    assert task.article_schema_json == json.dumps(generated_article["article_schema_json"], ensure_ascii=False)
    assert task.faq_schema_json == json.dumps(generated_article["faq_schema_json"], ensure_ascii=False)
    assert task.article_status == "generated"


def test_export_seo_article_missing_task_returns_404(client: TestClient) -> None:
    response = client.get("/seo/tasks/999/export")

    assert response.status_code == 404
    assert response.json()["detail"] == "SEO task not found"


def test_export_seo_article_without_generated_article_returns_400(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/no-export", priority="medium", status="recommended"))
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.get(f"/seo/tasks/{task.id}/export")

    assert response.status_code == 400
    assert response.json()["detail"] == "Generated article is required"


def test_export_seo_article_view_without_generated_article_returns_400(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/no-export-view", priority="medium", status="recommended"))
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.get(f"/seo/tasks/{task.id}/export-view")

    assert response.status_code == 400
    assert response.json()["detail"] == "Generated article is required"


def test_export_seo_article_returns_cms_payload(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/grills/portable-grill-guide",
            priority="high",
            status="recommended",
            suggested_title="Portable Grill Buying Guide",
            suggested_h1="Best Portable Grills for Camping",
            meta_description="Choose the best portable grill for camping and tailgating.",
            article_html="<article><h1>Best Portable Grills for Camping</h1><p>Portable grill body.</p></article>",
            faq_schema_json=json.dumps({"@type": "FAQPage", "mainEntity": []}),
            article_schema_json=json.dumps({"@type": "Article", "headline": "Best Portable Grills for Camping"}),
            article_status="generated",
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.get(f"/seo/tasks/{task.id}/export")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "task_id": task.id,
        "page_url": "https://example.com/grills/portable-grill-guide",
        "meta_title": "Portable Grill Buying Guide",
        "meta_description": "Choose the best portable grill for camping and tailgating.",
        "h1": "Best Portable Grills for Camping",
        "article_html": "<article><h1>Best Portable Grills for Camping</h1><p>Portable grill body.</p></article>",
        "faq_schema_json": {"@type": "FAQPage", "mainEntity": []},
        "article_schema_json": {"@type": "Article", "headline": "Best Portable Grills for Camping"},
        "slug_suggestion": "best-portable-grills-for-camping",
        "publishing_notes": [
            "Copy article_html into the CMS body field.",
            "Add faq_schema_json and article_schema_json to the page schema/script area.",
        ],
    }


def test_export_seo_article_view_returns_copy_friendly_html(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/grills/kamado-guide",
            priority="high",
            status="recommended",
            suggested_title="Kamado Grill Guide",
            suggested_h1="Kamado Grill Setup Tips",
            meta_description="Set up a kamado grill with confidence.",
            article_html="<article><h1>Kamado Grill Setup Tips</h1><p>Setup body.</p></article>",
            faq_schema_json=json.dumps({"@type": "FAQPage"}),
            article_schema_json=json.dumps({"@type": "Article", "headline": "Kamado Grill Setup Tips"}),
            article_status="generated",
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.get(f"/seo/tasks/{task.id}/export-view")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<!doctype html>" in response.text
    assert "<h2>Meta title</h2>" in response.text
    assert "Kamado Grill Guide" in response.text
    assert "<h2>Meta description</h2>" in response.text
    assert "Set up a kamado grill with confidence." in response.text
    assert "<h2>H1</h2>" in response.text
    assert "Kamado Grill Setup Tips" in response.text
    assert "<h2>Article HTML</h2>" in response.text
    escaped_article = (
        "&lt;article&gt;&lt;h1&gt;Kamado Grill Setup Tips&lt;/h1&gt;" "&lt;p&gt;Setup body.&lt;/p&gt;&lt;/article&gt;"
    )
    assert escaped_article in response.text
    assert "<h2>FAQ schema</h2>" in response.text
    assert "<h2>Article schema</h2>" in response.text
    assert "<h2>Publishing notes</h2>" in response.text


def test_seo_article_preview_returns_html(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/preview",
            priority="medium",
            status="recommended",
            suggested_title="Preview Article",
            recommendation_json=json.dumps({"content_recommendations": ["Preview content."]}),
            article_html="<article><h1>Preview Article</h1><p>Preview body.</p></article>",
            article_status="generated",
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.get(f"/seo/tasks/{task.id}/preview")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<!doctype html>" in response.text
    assert "<title>Preview Article</title>" in response.text
    assert "<h1>Preview Article</h1>" in response.text


def test_internal_link_scoring_prefers_authority_sources_and_weak_targets() -> None:
    from app.services.internal_links import authority_score, opportunity_score

    strong_page = {
        "url": "https://example.com/guides/grill-buying-guide",
        "seo_score": 94,
        "internal_links_count": 42,
        "word_count": 1800,
        "crawl_depth": 1,
        "title": "Grill Buying Guide",
        "meta_description": "Choose the right grill.",
        "h1": "Grill Buying Guide",
        "missing_fields": [],
    }
    weak_page = {
        "url": "https://example.com/portable-grills",
        "seo_score": 38,
        "internal_links_count": 1,
        "word_count": 250,
        "title": "Portable Grills",
        "meta_description": "",
        "h1": "",
        "missing_fields": ["meta_description", "h1"],
    }
    generated_task = {"article_status": "generated"}

    assert authority_score(strong_page) >= 80
    assert opportunity_score(weak_page) >= 65
    assert opportunity_score(weak_page, generated_task) < opportunity_score(weak_page)


def test_internal_link_opportunities_endpoint_returns_openai_enriched_opportunities(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=2, average_score=72)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/grill-guides",
                status_code=200,
                title="Complete Grill Guides",
                meta_description="Helpful grill guides.",
                h1="Complete Grill Guides",
                word_count=1800,
                internal_links=45,
                missing_fields="",
                seo_score=94,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/portable-grills",
                status_code=200,
                title="Portable Grills",
                h1="Portable Grills",
                word_count=400,
                internal_links=1,
                missing_fields="meta_description",
                seo_score=42,
            ),
        ]
    )
    db_session.add(
        SEOTask(
            page_url="https://example.com/portable-grills",
            keyword="portable grills",
            priority="high",
            status="recommended",
            suggested_h1="Best Portable Grills",
            article_status="not_generated",
        )
    )
    db_session.commit()
    monkeypatch.setattr("app.api.routes.settings.openai_api_key", "test-key")

    class MockOpenAIClient:
        def generate_internal_link_suggestions(self, pages: list[dict]) -> dict:
            assert pages[0]["source_url"] == "https://example.com/grill-guides"
            assert pages[0]["target_page"]["task"]["keyword"] == "portable grills"
            return {
                "opportunities": [
                    {
                        "source_url": "https://example.com/grill-guides",
                        "target_url": "https://example.com/portable-grills",
                        "anchor_text": "best portable grills",
                        "reason": "The guide context supports a relevant link to the portable grills page.",
                    }
                ]
            }

    monkeypatch.setattr("app.api.routes.OpenAIClient", MockOpenAIClient)

    response = client.get("/seo/internal-link-opportunities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["summary"] == {"strong_pages": 1, "weak_pages": 1, "link_opportunities": 1}
    assert payload["opportunities"] == [
        {
            "source_url": "https://example.com/grill-guides",
            "target_url": "https://example.com/portable-grills",
            "anchor_text": "best portable grills",
            "reason": "The guide context supports a relevant link to the portable grills page.",
            "authority_score": 91,
            "opportunity_score": 68,
        }
    ]


def test_internal_link_opportunities_empty_crawl_returns_empty_payload(client: TestClient) -> None:
    response = client.get("/seo/internal-link-opportunities")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "summary": {"strong_pages": 0, "weak_pages": 0, "link_opportunities": 0},
        "opportunities": [],
    }


def test_topical_cluster_grouping_uses_keyword_and_url_topics() -> None:
    from app.services.topical_clusters import group_pages_by_topic

    pages = [
        {
            "url": "https://example.com/guides/portable-grills",
            "title": "Portable Grill Guide",
            "keyword": "portable grills",
            "seo_score": 82,
        },
        {
            "url": "https://example.com/guides/portable-grill-cleaning",
            "title": "Portable Grill Cleaning",
            "keyword": "portable grills cleaning",
            "seo_score": 74,
        },
        {
            "url": "https://example.com/smokers/electric-smokers",
            "title": "Electric Smokers",
            "seo_score": 91,
        },
    ]

    grouped = group_pages_by_topic(pages)

    assert list(grouped) == ["Electric Smokers", "Portable Grills"]
    assert [page["url"] for page in grouped["Portable Grills"]] == [
        "https://example.com/guides/portable-grills",
        "https://example.com/guides/portable-grill-cleaning",
    ]


def test_topical_cluster_pillar_selection_prefers_strong_guide_page() -> None:
    from app.services.topical_clusters import select_pillar_page

    pages = [
        {
            "url": "https://example.com/blog/portable-grill-tips",
            "page_type": "page",
            "seo_score": 85,
            "word_count": 600,
        },
        {
            "url": "https://example.com/guides/portable-grills",
            "page_type": "guide",
            "seo_score": 72,
            "word_count": 1800,
            "article_status": "generated",
        },
    ]

    assert select_pillar_page(pages)["url"] == "https://example.com/guides/portable-grills"


def test_topical_clusters_endpoint_handles_empty_crawl(client: TestClient) -> None:
    response = client.get("/seo/topical-clusters")

    assert response.status_code == 200
    assert response.json() == {"success": True, "total_pages_analyzed": 0, "clusters": []}


def test_topical_clusters_endpoint_returns_clusters_with_mocked_data(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=3, average_score=76)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/guides/portable-grills",
                status_code=200,
                title="Portable Grill Guide",
                meta_description="Choose portable grills.",
                h1="Portable Grill Guide",
                word_count=1800,
                internal_links=20,
                missing_fields="",
                seo_score=88,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/guides/portable-grill-cleaning",
                status_code=200,
                title="Portable Grill Cleaning",
                meta_description="Clean portable grills.",
                h1="Portable Grill Cleaning",
                word_count=500,
                internal_links=3,
                missing_fields="",
                seo_score=62,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/smokers/electric-smokers",
                status_code=200,
                title="Electric Smokers",
                meta_description="Electric smoker guide.",
                h1="Electric Smokers",
                word_count=900,
                internal_links=8,
                missing_fields="",
                seo_score=74,
            ),
        ]
    )
    db_session.add(
        SEOTask(
            page_url="https://example.com/guides/portable-grill-cleaning",
            keyword="portable grills",
            priority="high",
            status="recommended",
            article_status="not_generated",
        )
    )
    db_session.commit()
    monkeypatch.setattr("app.api.routes.settings.openai_api_key", "test-key")

    class MockOpenAIClient:
        def generate_topical_clusters(self, pages: list[dict]) -> dict:
            portable_page = next(page for page in pages if page["url"].endswith("portable-grill-cleaning"))
            assert portable_page["task_status"] == "recommended"
            assert portable_page["article_status"] == "not_generated"
            assert portable_page["page_type"] == "guide"
            return {
                "clusters": [
                    {
                        "cluster_name": "Portable Grills",
                        "pillar_page": "https://example.com/guides/portable-grills",
                        "supporting_pages": ["https://example.com/guides/portable-grill-cleaning"],
                        "missing_articles": ["Portable grill fuel comparison"],
                        "internal_link_strategy": ["Link cleaning support content to the portable grill pillar page."],
                    }
                ]
            }

    monkeypatch.setattr("app.api.routes.OpenAIClient", MockOpenAIClient)

    response = client.get("/seo/topical-clusters")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "success": True,
        "total_pages_analyzed": 3,
        "clusters": [
            {
                "cluster_name": "Portable Grills",
                "pillar_page": "https://example.com/guides/portable-grills",
                "supporting_pages": ["https://example.com/guides/portable-grill-cleaning"],
                "missing_articles": ["Portable grill fuel comparison"],
                "internal_link_strategy": ["Link cleaning support content to the portable grill pillar page."],
            }
        ],
    }


def test_dashboard_shows_seo_ai_workflow_metrics_and_actions(client: TestClient, db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=2, average_score=68)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/grill-guides",
                status_code=200,
                title="Complete Grill Guides",
                meta_description="Helpful grill guides.",
                h1="Complete Grill Guides",
                word_count=1800,
                internal_links=45,
                missing_fields="",
                seo_score=94,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/portable-grills",
                status_code=200,
                title="Portable Grills",
                h1="Portable Grills",
                word_count=400,
                internal_links=1,
                missing_fields="meta_description",
                seo_score=42,
            ),
        ]
    )
    db_session.add_all(
        [
            SEOTask(
                page_url="https://example.com/portable-grills",
                keyword="portable grills",
                priority="high",
                status="recommended",
                suggested_h1="Best Portable Grills",
                article_html="<article>Generated article</article>",
                article_status="generated",
            ),
            SEOTask(page_url="https://example.com/extra-task", priority="medium", status="open"),
        ]
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert "Latest crawl status" in response.text
    assert "completed" in response.text
    assert "Total SEO tasks" in response.text
    assert "Recommended tasks" in response.text
    assert "Generated articles" in response.text
    assert "Internal link opportunities" in response.text
    assert "Topical clusters" in response.text
    assert response.text.count("<strong>2</strong>") >= 2
    assert "<strong>1</strong>" in response.text
    assert 'href="/seo/tasks-view"' in response.text
    assert 'action="/seo/tasks/from-latest-crawl"' in response.text
    assert 'href="/seo/internal-link-opportunities-view"' in response.text
    assert 'href="/seo/topical-clusters-view"' in response.text


def test_seo_tasks_view_renders_saved_tasks(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/task-view",
            keyword="task view keyword",
            priority="high",
            status="recommended",
            suggested_title="Task View Title",
            article_status="generated",
        )
    )
    db_session.commit()

    response = client.get("/seo/tasks-view")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "SEO tasks" in response.text
    assert "https://example.com/task-view" in response.text
    assert "Task View Title" in response.text
    assert "generated" in response.text


def test_internal_link_opportunities_view_renders_latest_crawl_opportunities(
    client: TestClient, db_session: Session
) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=2, average_score=70)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/grill-guides",
                status_code=200,
                title="Complete Grill Guides",
                meta_description="Helpful grill guides.",
                h1="Complete Grill Guides",
                word_count=1800,
                internal_links=45,
                missing_fields="",
                seo_score=94,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/portable-grills",
                status_code=200,
                title="Portable Grills",
                h1="Portable Grills",
                word_count=400,
                internal_links=1,
                missing_fields="meta_description",
                seo_score=42,
            ),
        ]
    )
    db_session.add(
        SEOTask(
            page_url="https://example.com/portable-grills",
            keyword="portable grills",
            priority="high",
            status="recommended",
        )
    )
    db_session.commit()

    response = client.get("/seo/internal-link-opportunities-view")

    assert response.status_code == 200
    assert "Internal link opportunities" in response.text
    assert "https://example.com/grill-guides" in response.text
    assert "https://example.com/portable-grills" in response.text
    assert "portable grills" in response.text


def test_topical_clusters_view_renders_latest_crawl_clusters(client: TestClient, db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=2, average_score=78)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/guides/portable-grills",
                status_code=200,
                title="Portable Grill Guide",
                meta_description="Choose portable grills.",
                h1="Portable Grill Guide",
                word_count=1800,
                internal_links=20,
                missing_fields="",
                seo_score=88,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/guides/portable-grill-cleaning",
                status_code=200,
                title="Portable Grill Cleaning",
                meta_description="Clean portable grills.",
                h1="Portable Grill Cleaning",
                word_count=500,
                internal_links=3,
                missing_fields="",
                seo_score=62,
            ),
        ]
    )
    db_session.add(
        SEOTask(
            page_url="https://example.com/guides/portable-grill-cleaning",
            keyword="portable grills",
            priority="high",
            status="recommended",
        )
    )
    db_session.commit()

    response = client.get("/seo/topical-clusters-view")

    assert response.status_code == 200
    assert "Topical clusters" in response.text
    assert "Portable Grills" in response.text
    assert "https://example.com/guides/portable-grills" in response.text
    assert "https://example.com/guides/portable-grill-cleaning" in response.text
    assert "Create or expand supporting article" in response.text


def test_create_seo_fixes_from_recommended_task(client: TestClient, db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed")
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add(
        PageAudit(
            crawl_run_id=crawl_run.id,
            url="https://example.com/recommended",
            title="Old title",
            h1="Old H1",
            meta_description="Old description",
            seo_score=55,
        )
    )
    db_session.add(
        SEOTask(
            page_url="https://example.com/recommended",
            status="recommended",
            suggested_title="New title",
            suggested_h1="New H1",
            meta_description="New description",
            recommendation_json=json.dumps({"confidence_score": 0.91}),
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.post(f"/seo/tasks/{task.id}/create-fixes")

    assert response.status_code == 201
    payload = response.json()
    assert payload["created_count"] == 3
    assert {fix["fix_type"] for fix in payload["fixes"]} == {"meta_title", "meta_description", "h1"}
    assert db_session.query(SEOFix).count() == 3
    meta_title = db_session.query(SEOFix).filter(SEOFix.fix_type == "meta_title").one()
    assert meta_title.current_value == "Old title"
    assert meta_title.proposed_value == "New title"


def test_create_seo_fixes_from_generated_article(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/article",
            status="recommended",
            suggested_title="Article title",
            suggested_h1="Article H1",
            meta_description="Article description",
            recommendation_json=json.dumps({"recommendations": ["Write article"]}),
            article_html="<h1>Article H1</h1><p>Body copy.</p>",
            faq_schema_json=json.dumps({"@type": "FAQPage"}),
            article_schema_json=json.dumps({"@type": "Article"}),
            article_status="generated",
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()

    response = client.post(f"/seo/tasks/{task.id}/create-fixes")

    assert response.status_code == 201
    payload = response.json()
    assert payload["created_count"] == 6
    assert {fix["fix_type"] for fix in payload["fixes"]} == {
        "meta_title",
        "meta_description",
        "h1",
        "article_html",
        "faq_schema",
        "article_schema",
    }


def test_create_seo_fixes_prevents_duplicate_drafts(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOTask(
            page_url="https://example.com/dupe-fix",
            status="recommended",
            suggested_title="Duplicate title",
            recommendation_json=json.dumps({"recommendations": ["Improve title"]}),
        )
    )
    db_session.commit()
    task = db_session.query(SEOTask).one()

    first_response = client.post(f"/seo/tasks/{task.id}/create-fixes")
    second_response = client.post(f"/seo/tasks/{task.id}/create-fixes")

    assert first_response.status_code == 201
    assert first_response.json()["created_count"] == 1
    assert second_response.status_code == 201
    assert second_response.json()["created_count"] == 0
    assert db_session.query(SEOFix).count() == 1


def test_approve_and_reject_seo_fix(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/review", recommendation_json=json.dumps({"ok": True})))
    db_session.commit()
    task = db_session.query(SEOTask).one()
    db_session.add_all(
        [
            SEOFix(task_id=task.id, page_url=task.page_url, fix_type="meta_title", proposed_value="Approve me"),
            SEOFix(task_id=task.id, page_url=task.page_url, fix_type="h1", proposed_value="Reject me"),
        ]
    )
    db_session.commit()
    approve_fix, reject_fix = db_session.query(SEOFix).order_by(SEOFix.id.asc()).all()

    approve_response = client.post(f"/seo/fixes/{approve_fix.id}/approve")
    reject_response = client.post(f"/seo/fixes/{reject_fix.id}/reject")

    assert approve_response.status_code == 200
    assert approve_response.json()["fix"]["status"] == "approved"
    assert reject_response.status_code == 200
    assert reject_response.json()["fix"]["status"] == "rejected"


def test_export_seo_fix_returns_copy_friendly_payload(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/export-fix", recommendation_json=json.dumps({"ok": True})))
    db_session.commit()
    task = db_session.query(SEOTask).one()
    db_session.add(
        SEOFix(
            task_id=task.id,
            page_url=task.page_url,
            fix_type="meta_description",
            current_value="Old",
            proposed_value="New",
            status="approved",
        )
    )
    db_session.commit()
    fix = db_session.query(SEOFix).one()

    response = client.get(f"/seo/fixes/{fix.id}/export")

    assert response.status_code == 200
    assert response.json() == {
        "page_url": "https://example.com/export-fix",
        "fix_type": "meta_description",
        "current_value": "Old",
        "proposed_value": "New",
        "publishing_instructions": [
            "Do not auto-publish; apply this fix manually only after approval.",
            "Update the page meta description with proposed_value.",
        ],
    }


def test_seo_fixes_endpoint_filters_results(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/filter-fix", recommendation_json=json.dumps({"ok": True})))
    db_session.commit()
    task = db_session.query(SEOTask).one()
    db_session.add_all(
        [
            SEOFix(
                task_id=task.id,
                page_url=task.page_url,
                fix_type="meta_title",
                proposed_value="Title",
                status="approved",
            ),
            SEOFix(task_id=task.id, page_url=task.page_url, fix_type="h1", proposed_value="H1", status="draft"),
        ]
    )
    db_session.commit()

    response = client.get(f"/seo/fixes?status=approved&fix_type=meta_title&task_id={task.id}")

    assert response.status_code == 200
    fixes = response.json()["fixes"]
    assert len(fixes) == 1
    assert fixes[0]["fix_type"] == "meta_title"
    assert fixes[0]["status"] == "approved"


def test_seo_fixes_view_loads(client: TestClient, db_session: Session) -> None:
    db_session.add(SEOTask(page_url="https://example.com/fixes-view", recommendation_json=json.dumps({"ok": True})))
    db_session.commit()
    task = db_session.query(SEOTask).one()
    db_session.add(SEOFix(task_id=task.id, page_url=task.page_url, fix_type="meta_title", proposed_value="View title"))
    db_session.commit()

    response = client.get("/seo/fixes-view")

    assert response.status_code == 200
    assert "Review SEO fixes" in response.text
    assert "View title" in response.text
    assert f'href="/seo/fixes/{db_session.query(SEOFix).one().id}/export"' in response.text


def _approved_fix(db_session: Session, status: str = "approved", fix_type: str = "meta_title") -> SEOFix:
    task = SEOTask(
        page_url="https://example.com/publish-me",
        keyword="publish keyword",
        priority="high",
        status="recommended",
    )
    db_session.add(task)
    db_session.flush()
    fix = SEOFix(
        task_id=task.id,
        page_url=task.page_url,
        fix_type=fix_type,
        current_value="Old value",
        proposed_value="New ISTORE-ready value",
        status=status,
        confidence_score=0.92,
        source="seo_task",
    )
    db_session.add(fix)
    db_session.commit()
    return fix


def test_cannot_create_publishing_package_from_unapproved_fix(client: TestClient, db_session: Session) -> None:
    fix = _approved_fix(db_session, status="draft")

    response = client.post(f"/seo/fixes/{fix.id}/create-publishing-package")

    assert response.status_code == 400
    assert response.json()["detail"] == "Only approved SEO fixes can be packaged"


def test_create_publishing_package_from_approved_fix(client: TestClient, db_session: Session) -> None:
    fix = _approved_fix(db_session, fix_type="meta_description")

    response = client.post(f"/seo/fixes/{fix.id}/create-publishing-package")

    assert response.status_code == 201
    payload = response.json()
    package = payload["package"]
    assert payload["success"] is True
    assert payload["duplicate"] is False
    assert package["fix_id"] == fix.id
    assert package["cms_type"] == "istore"
    assert package["status"] == "ready"
    assert package["payload_json"]["fix_type"] == "meta_description"
    assert package["payload_json"]["istore_fields"] == {"meta_description": "New ISTORE-ready value"}


def test_create_publishing_package_prevents_duplicate_draft_or_ready_package(
    client: TestClient, db_session: Session
) -> None:
    fix = _approved_fix(db_session)

    first_response = client.post(f"/seo/fixes/{fix.id}/create-publishing-package")
    second_response = client.post(f"/seo/fixes/{fix.id}/create-publishing-package")

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert second_response.json()["duplicate"] is True
    assert db_session.query(PublishingPackage).filter(PublishingPackage.fix_id == fix.id).count() == 1


def test_list_publishing_packages_filters_by_status_and_cms_type(client: TestClient, db_session: Session) -> None:
    fix = _approved_fix(db_session)
    db_session.add_all(
        [
            PublishingPackage(
                fix_id=fix.id,
                page_url=fix.page_url,
                cms_type="istore",
                payload_json='{"fix_type":"meta_title"}',
                status="ready",
            ),
            PublishingPackage(
                fix_id=fix.id,
                page_url=fix.page_url,
                cms_type="other",
                payload_json='{"fix_type":"meta_title"}',
                status="exported",
            ),
        ]
    )
    db_session.commit()

    response = client.get("/seo/publishing-packages?status=ready&cms_type=istore")

    assert response.status_code == 200
    packages = response.json()["packages"]
    assert len(packages) == 1
    assert packages[0]["status"] == "ready"
    assert packages[0]["cms_type"] == "istore"


def test_export_publishing_package_returns_copy_payload_and_marks_exported(
    client: TestClient, db_session: Session
) -> None:
    fix = _approved_fix(db_session)
    create_response = client.post(f"/seo/fixes/{fix.id}/create-publishing-package")
    package_id = create_response.json()["package"]["id"]

    response = client.get(f"/seo/publishing-packages/{package_id}/export")

    assert response.status_code == 200
    payload = response.json()
    assert payload["package"]["status"] == "exported"
    assert payload["copy_payload"]["page_url"] == fix.page_url
    assert "New ISTORE-ready value" in payload["copy_payload_json"]


def test_mark_publishing_package_applied(client: TestClient, db_session: Session) -> None:
    fix = _approved_fix(db_session)
    create_response = client.post(f"/seo/fixes/{fix.id}/create-publishing-package")
    package_id = create_response.json()["package"]["id"]

    response = client.post(f"/seo/publishing-packages/{package_id}/mark-applied")

    assert response.status_code == 200
    assert response.json()["package"]["status"] == "applied_manually"


def test_publishing_packages_view_loads(client: TestClient, db_session: Session) -> None:
    fix = _approved_fix(db_session)
    client.post(f"/seo/fixes/{fix.id}/create-publishing-package")

    response = client.get("/seo/publishing-packages-view")

    assert response.status_code == 200
    assert "Publishing packages" in response.text
    assert "https://example.com/publish-me" in response.text
