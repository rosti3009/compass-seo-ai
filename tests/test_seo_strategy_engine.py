from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, GSCKeywordMetric, PageAudit, SEOStrategyRecommendation, SEOTask
from app.main import app
from app.services.seo_strategy_engine import calculate_priority_scores, rank_recommendations


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


def _seed_strategy_inputs(db_session: Session) -> None:
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=2, average_score=62)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/high-intent",
                status_code=200,
                title="Old title",
                h1="High intent",
                meta_description="",
                missing_fields="meta_description",
                word_count=420,
                internal_links=2,
                seo_score=58,
            ),
            PageAudit(
                crawl_run_id=crawl_run.id,
                url="https://example.com/source",
                status_code=200,
                title="Source",
                h1="Source",
                meta_description="Strong source page",
                missing_fields="",
                word_count=1500,
                internal_links=35,
                seo_score=92,
            ),
        ]
    )
    db_session.add(
        GSCKeywordMetric(
            page_url="https://example.com/high-intent",
            query="commercial grill",
            clicks=3,
            impressions=2500,
            ctr=0.01,
            average_position=8.0,
            date=date.today(),
        )
    )
    db_session.add(
        SEOTask(
            page_url="https://example.com/high-intent",
            keyword="commercial grill",
            priority="high",
            status="recommended",
            article_status="not_generated",
        )
    )
    db_session.commit()


def test_strategy_recommendation_generation_uses_site_signals(client: TestClient, db_session: Session) -> None:
    _seed_strategy_inputs(db_session)

    response = client.post("/seo/strategy/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["created_count"] >= 1
    recommendation = payload["recommendations"][0]
    assert recommendation["page_url"] == "https://example.com/high-intent"
    assert recommendation["recommendation_type"] in {"rewrite_meta", "improve_ctr", "improve_internal_links"}
    assert recommendation["priority_score"] > 0
    assert recommendation["ai_summary"]
    assert recommendation["recommended_action"]
    assert recommendation["reasoning"]


def test_strategy_run_prevents_duplicate_pending_recommendations(client: TestClient, db_session: Session) -> None:
    _seed_strategy_inputs(db_session)

    first = client.post("/seo/strategy/run")
    second = client.post("/seo/strategy/run")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["created_count"] == 0
    assert second.json()["updated_count"] >= 1
    pending = db_session.query(SEOStrategyRecommendation).filter(SEOStrategyRecommendation.status == "pending").all()
    keys = {(item.page_url, item.recommendation_type) for item in pending}
    assert len(keys) == len(pending)


def test_strategy_ranking_logic_orders_by_priority_score() -> None:
    low = SEOStrategyRecommendation(
        page_url="https://example.com/b", recommendation_type="expand_content", priority_score=12
    )
    high = SEOStrategyRecommendation(
        page_url="https://example.com/a", recommendation_type="improve_ctr", priority_score=88
    )

    ranked = rank_recommendations([low, high])

    assert ranked == [high, low]


def test_strategy_summary_endpoint_returns_required_groups(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOStrategyRecommendation(
            page_url="https://example.com/ready",
            recommendation_type="publish_fix_package",
            priority_score=75,
            traffic_potential_score=60,
            topical_authority_score=70,
            publishing_readiness_score=90,
            ai_summary="Ready",
            recommended_action="Publish approved package.",
            reasoning="Ready package.",
        )
    )
    db_session.commit()

    response = client.get("/seo/strategy/summary")

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert set(summary) == {
        "highest_priority_pages",
        "quick_wins",
        "traffic_growth_opportunities",
        "pages_ready_to_publish",
        "weak_clusters",
        "recommended_next_actions",
    }
    assert summary["highest_priority_pages"][0]["page_url"] == "https://example.com/ready"


def test_strategy_dashboard_views_render(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOStrategyRecommendation(
            page_url="https://example.com/view",
            recommendation_type="expand_content",
            priority_score=42,
            ai_summary="Expand this page.",
            recommended_action="Add buyer FAQs.",
            reasoning="Thin content.",
        )
    )
    db_session.commit()

    recommendations_view = client.get("/seo/strategy-view")
    summary_view = client.get("/seo/strategy-summary-view")

    assert recommendations_view.status_code == 200
    assert "Prioritized SEO recommendations" in recommendations_view.text
    assert "https://example.com/view" in recommendations_view.text
    assert summary_view.status_code == 200
    assert "Site strategy summary" in summary_view.text


def test_strategy_engine_handles_missing_gsc_and_openai_gracefully(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.seo_strategy_engine.settings.openai_api_key", None)
    crawl_run = CrawlRun(target_domain="https://example.com", status="completed", pages_crawled=1, average_score=55)
    db_session.add(crawl_run)
    db_session.flush()
    db_session.add(
        PageAudit(
            crawl_run_id=crawl_run.id,
            url="https://example.com/no-gsc",
            status_code=200,
            title="",
            h1="No GSC",
            meta_description="",
            missing_fields="title,meta_description",
            word_count=350,
            internal_links=0,
            seo_score=45,
        )
    )
    db_session.commit()

    response = client.post("/seo/strategy/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["created_count"] == 1
    assert payload["recommendations"][0]["ai_summary"]


def test_calculate_priority_scores_weights_gsc_and_readiness() -> None:
    metric = GSCKeywordMetric(
        page_url="https://example.com/scored",
        query="scored query",
        impressions=4000,
        ctr=0.005,
        average_position=6,
        date=date.today(),
    )

    scores = calculate_priority_scores(gsc_metric=metric, internal_link_opportunity=80, topical_gap=70)

    assert scores["traffic_potential_score"] == 80
    assert scores["ctr_opportunity_score"] > 80
    assert scores["ranking_opportunity_score"] > 80
    assert scores["priority_score"] > 50
