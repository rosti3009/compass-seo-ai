from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, GSCKeywordMetric, PageAudit
from app.main import app
from app.services.hebrew_seo import classify_intent, israeli_seasonality, normalize_hebrew_keyword, remove_nikud


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


def add_compass_page(db_session: Session) -> PageAudit:
    crawl_run = CrawlRun(
        target_domain="https://compassgrill.co.il",
        status="completed",
        pages_crawled=1,
        average_score=76,
    )
    db_session.add(crawl_run)
    db_session.flush()
    page = PageAudit(
        crawl_run_id=crawl_run.id,
        url="https://compassgrill.co.il/collections/גרילי-גז/products/napoleon-rogue-low-stock",
        status_code=200,
        title="גרילי גז נפוליאון במבצע | Compass Grill",
        meta_description="קנו גריל גז נפוליאון מקורי עם אחריות, משלוח מהיר ומלאי מוגבל לקיץ וליום העצמאות.",
        h1="גריל גז נפוליאון לגינה",
        missing_fields="",
        seo_score=76,
    )
    db_session.add(page)
    db_session.add(
        GSCKeywordMetric(
            page_url=page.url,
            query="לקנות BBQ gas grill נפוליאון",
            clicks=31,
            impressions=1200,
            ctr=0.025,
            average_position=5.2,
            date=date(2026, 5, 1),
            source="gsc",
        )
    )
    db_session.commit()
    return page


def test_hebrew_keyword_normalization_removes_nikud_plurals_and_mixed_terms() -> None:
    assert remove_nikud("גְּרִילִים") == "גרילים"
    assert normalize_hebrew_keyword("BBQ gas grills נַפּוֹלֵיאוֹן מנגלים") == "גריל גז נפוליאון"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("לקנות גריל גז במבצע", "transactional"),
        ("איך לנקות גריל גז", "informational"),
        ("וובר מול נפוליאון השוואה", "comparison"),
        ("חנות גרילים בתל אביב", "local"),
        ("חוות דעת גריל נפוליאון", "commercial_investigation"),
    ],
)
def test_intent_classification(query: str, expected: str) -> None:
    assert classify_intent(query) == expected


def test_israeli_seasonality_marks_independence_day_window_active() -> None:
    helpers = israeli_seasonality(date(2026, 5, 1))
    active_names = {helper["name"] for helper in helpers if helper["active_now"]}
    assert "Independence Day grilling" in active_names


def test_hebrew_insights_endpoint_supports_compassgrill_ecommerce(client: TestClient, db_session: Session) -> None:
    add_compass_page(db_session)

    response = client.get("/seo/hebrew-insights")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["supported_site"] == "compassgrill.co.il"
    assert payload["summary"]["pages_analyzed"] == 1
    insight = payload["insights"][0]
    assert insight["domain_supported"] is True
    assert insight["intent"] == "transactional"
    assert insight["normalized_primary_keyword"] == "לקנות גריל גז נפוליאון"
    assert insight["ecommerce_signals"]["is_product_page"] is True
    assert insight["ecommerce_signals"]["is_low_stock_high_demand"] is True
    assert insight["score_breakdown"]["title_quality"]["score"] > 0


def test_hebrew_insights_view_renders(client: TestClient, db_session: Session) -> None:
    add_compass_page(db_session)

    response = client.get("/seo/hebrew-insights-view")

    assert response.status_code == 200
    assert "ניתוח SEO עברי" in response.text
    assert "compassgrill.co.il" in response.text


def test_hebrew_insights_can_request_openai_enrichment(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_compass_page(db_session)

    class MockOpenAIClient:
        def generate_hebrew_seo_enrichment(self, insights: list[dict]) -> dict:
            assert insights[0]["domain_supported"] is True
            return {
                "recommendations": [
                    {
                        "url": insights[0]["url"],
                        "hebrew_priority": "גבוה",
                        "suggested_title": "גריל גז נפוליאון במבצע",
                        "suggested_meta": "קנו גריל גז נפוליאון עם משלוח ואחריות.",
                        "keyword_plan": ["גריל גז נפוליאון", "מנגל גז"],
                        "seasonality_note": "להיערך ליום העצמאות ולעונת הקיץ.",
                        "ecommerce_action": "לחזק מלאי, משלוח ואחריות בעמוד המוצר.",
                    }
                ]
            }

    monkeypatch.setattr("app.api.routes.OpenAIClient", MockOpenAIClient)

    response = client.get("/seo/hebrew-insights?enrich=true")

    assert response.status_code == 200
    enrichment = response.json()["openai_enrichment"]
    assert enrichment["enabled"] is True
    assert enrichment["recommendations"][0]["hebrew_priority"] == "גבוה"
