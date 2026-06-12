from collections.abc import Generator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import CrawlRun, GSCKeywordMetric, IStoreProduct, PageAudit
from app.main import app
from app.services.product_category_audit_center import build_product_category_audit_center


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


def _add_metric(
    db: Session,
    *,
    page_url: str,
    metric_date: date,
    impressions: int,
    clicks: int,
    query: str = "גריל גז מומלץ",
) -> None:
    db.add(
        GSCKeywordMetric(
            page_url=page_url,
            query=query,
            clicks=clicks,
            impressions=impressions,
            ctr=clicks / impressions if impressions else 0.0,
            average_position=8.0,
            date=metric_date,
            source="gsc",
        )
    )


def test_product_category_audit_center_prioritizes_categories_and_copy_ready_hebrew(db_session: Session) -> None:
    crawl = CrawlRun(
        target_domain="https://example.com",
        status="completed",
        completed_at=datetime(2026, 6, 1, tzinfo=UTC),
        pages_crawled=2,
        average_score=60,
    )
    db_session.add(crawl)
    db_session.flush()
    category_url = "https://example.com/category/grills"
    product_url = "https://example.com/products/premium-grill"
    db_session.add_all(
        [
            PageAudit(
                crawl_run_id=crawl.id,
                url=category_url,
                status_code=200,
                title="גרילים ומעשנות",
                meta_description="קצר מדי",
                h1="",
                word_count=80,
                internal_links=1,
                missing_fields="h1,meta_description,image_alt,schema",
                page_type="category",
                seo_score=58,
            ),
            PageAudit(
                crawl_run_id=crawl.id,
                url=product_url,
                status_code=200,
                title="גריל גז פרימיום",
                meta_description="תיאור מוצר איכותי ומפורט בעברית שמסביר היטב על המוצר, יתרונותיו, שימושים נפוצים והתאמה ללקוחות לפני רכישה.",
                h1="גריל גז פרימיום",
                word_count=180,
                internal_links=4,
                missing_fields="",
                page_type="product",
                seo_score=82,
            ),
            IStoreProduct(
                istore_product_id="p1",
                product_name="גריל גז פרימיום",
                canonical_url=product_url,
                category="גרילים",
                meta_title="גריל גז פרימיום",
                meta_description="תיאור מוצר איכותי ומפורט בעברית שמסביר היטב על המוצר, יתרונותיו ושימושים נפוצים.",
            ),
        ]
    )
    _add_metric(db_session, page_url=category_url, metric_date=date(2026, 4, 20), impressions=1200, clicks=80)
    _add_metric(db_session, page_url=category_url, metric_date=date(2026, 5, 25), impressions=500, clicks=30)
    _add_metric(db_session, page_url=product_url, metric_date=date(2026, 5, 25), impressions=900, clicks=40)
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session)

    assert dashboard["audits"][0]["entity_type"] == "category"
    category = dashboard["audits"][0]
    assert category["statuses"]["meta_description"]["status"] == "דורש טיפול"
    assert category["statuses"]["h1"]["status"] == "דורש טיפול"
    assert category["revenue_risk"]["estimated_lost_clicks"] == 50
    assert category["safety"] == {
        "auto_publish": False,
        "edits_live_content": False,
        "manual_review_required": True,
        "message": "המלצות בלבד: לא מפרסם ולא עורך תוכן חי.",
    }
    copies = "\n".join(fix["copy"] for fix in category["ready_to_copy_fixes"])
    assert "גרילים ומעשנות" in copies
    assert "שאלה:" in copies
    assert dashboard["summary"]["with_gsc_impressions"] >= 2


def test_product_category_audit_center_endpoint_and_view_are_read_only(client: TestClient, db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="p2",
            product_name="מעשנת פחמים",
            canonical_url="https://example.com/products/smoker",
            category="מעשנות",
            meta_title="",
            meta_description="",
        )
    )
    db_session.commit()

    response = client.get("/seo/product-category-audit-center")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["summary"]["products"] == 1
    assert payload["summary"]["categories"] == 1
    assert payload["audits"][0]["entity_type"] == "category"
    assert payload["audits"][0]["safety"]["auto_publish"] is False

    view = client.get("/seo/product-category-audit-center/view")
    assert view.status_code == 200
    assert "מרכז ביקורת SEO למוצרים וקטגוריות" in view.text
    assert "לא פרסום ולא עריכה באתר חי" in view.text
