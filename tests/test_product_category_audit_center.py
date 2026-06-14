# ruff: noqa: E501
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
    assert category["statuses"]["meta_description"]["status"] == "חסר לפי נתונים זמינים"
    assert category["statuses"]["h1"]["status"] == "חסר לפי נתונים זמינים"
    assert category["data_confidence_score"] == 100
    assert category["missing_confirmed_count"] >= 3
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
    assert all("built-in method" not in fix["copy_text"] for fix in category["ready_to_copy_fixes"])
    assert all(isinstance(fix["copy_text"], str) for fix in category["ready_to_copy_fixes"])
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
    assert "יש להעתיק ידנית לאתר ISTORE לאחר בדיקה." in view.text
    assert "textarea" in view.text


def test_unknown_crawl_data_reduces_confidence_without_confirmed_missing(db_session: Session) -> None:
    db_session.add_all(
        [
            IStoreProduct(
                istore_product_id="p3",
                product_name="גריל ללא סריקה",
                canonical_url="https://example.com/products/no-crawl",
                category="גרילים",
                meta_title="גריל איכותי ללא סריקה",
                meta_description="תיאור מוצר מסונכרן מ-ISTORE שמספק מידע בסיסי וברור ללקוחות לפני רכישה באתר.",
            ),
            IStoreProduct(
                istore_product_id="p4",
                product_name="מוצר חסר מטא",
                canonical_url="https://example.com/products/missing-meta",
                category="אביזרים",
                meta_title="",
                meta_description="",
            ),
        ]
    )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session)
    product = next(item for item in dashboard["audits"] if item["url"] == "https://example.com/products/no-crawl")
    missing_product = next(
        item for item in dashboard["audits"] if item["url"] == "https://example.com/products/missing-meta"
    )

    assert product["statuses"]["h1"]["status"] == "לא נסרק — נדרש בדיקה ידנית"
    assert product["statuses"]["schema"]["state"] == "unknown"
    assert product["data_confidence_score"] < 100
    assert product["missing_confirmed_count"] == 0
    assert product["seo_score"] != 5
    assert product["seo_score"] != missing_product["seo_score"]


def test_gsc_url_matching_ignores_query_trailing_slash_scheme_and_encoded_hebrew(db_session: Session) -> None:
    product_url = "https://example.com/products/%D7%92%D7%A8%D7%99%D7%9C"
    db_session.add(
        IStoreProduct(
            istore_product_id="p5",
            product_name="גריל",
            canonical_url=product_url,
            category="גרילים",
            meta_title="גריל איכותי לבית",
            meta_description="תיאור מוצר מסונכרן מ-ISTORE שמספק מידע ברור ומספיק ארוך ללקוחות לפני רכישה באתר.",
        )
    )
    _add_metric(
        db_session,
        page_url="http://example.com/products/גריל/?from_admin=1",
        metric_date=date(2026, 5, 25),
        impressions=321,
        clicks=12,
    )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session)
    product = next(item for item in dashboard["audits"] if item["url"] == product_url)

    assert product["gsc"]["impressions"] == 321
    assert product["gsc"]["clicks"] == 12
    assert "GSC" in product["priority_reason"]


def test_category_discovery_from_sitemap_entries(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.product_category_audit_center._load_sitemap_index",
        lambda: (
            [{"url": "https://example.com/category/outdoor-kitchens", "page_type": "category", "title": "מטבחי חוץ"}],
            {},
        ),
    )

    dashboard = build_product_category_audit_center(db_session)

    assert dashboard["summary"]["categories"] == 1
    assert dashboard["audits"][0]["entity_type"] == "category"
    assert dashboard["audits"][0]["name"] == "מטבחי חוץ"


def test_ready_to_copy_product_suggestions_include_required_fields(db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="p6",
            product_name="טאבון גז",
            canonical_url="https://example.com/products/oven",
            category="טאבונים",
            meta_title="",
            meta_description="",
        )
    )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session)
    product = next(item for item in dashboard["audits"] if item["entity_type"] == "product")
    field_keys = {fix["field_key"] for fix in product["ready_to_copy_fixes"]}

    assert {
        "meta_title",
        "meta_description",
        "h1",
        "short_product_description",
        "long_product_description",
        "faq",
        "alt_text",
        "internal_link",
    } <= field_keys
    assert all(fix["suggested_value"] for fix in product["ready_to_copy_fixes"])
    assert all(
        fix["manual_notice"] == "יש להעתיק ידנית לאתר ISTORE לאחר בדיקה." for fix in product["ready_to_copy_fixes"]
    )


def test_live_page_html_analysis_extracts_schema_images_and_unknown_state() -> None:
    from app.services.product_category_audit_center import _extract_live_page_html, _text_field_status

    html = """<!doctype html><html><head><title>אבני בזלת לגריל גז</title><meta name="description" content="אבני בזלת לגריל גז לשיפור פיזור החום והפחתת התלקחויות."><link rel="canonical" href="https://example.com/basalt-stones"><script type="application/ld+json">{"@type":"Product","offers":{"price":"99","availability":"InStock"}}</script></head><body><nav class="breadcrumbs">בית > גרילים</nav><h1>אבני בזלת לגריל גז</h1><h2>שיפור פיזור חום בגריל</h2><img src="/stone.jpg" alt="אבני בזלת לגריל"><a href="/category/grills">גרילים</a></body></html>"""
    analysis = _extract_live_page_html(html, url="https://example.com/basalt-stones/?from_admin=1")

    assert analysis["title_tag"] == "אבני בזלת לגריל גז"
    assert analysis["h1"] == "אבני בזלת לגריל גז"
    assert analysis["product_schema_present"] is True
    assert analysis["price"] == "99"
    assert analysis["image_alt_attributes"] == ["אבני בזלת לגריל"]
    assert analysis["internal_links"][0]["href"] == "/category/grills"

    unknown = _text_field_status("Meta title", "", "", 25, 65, {"meta_title"}, set())
    confirmed = _text_field_status("Meta title", "", "crawl", 25, 65, {"meta_title"}, {"meta_title"})
    assert unknown["state"] == "unknown"
    assert confirmed["state"] == "missing_confirmed"


def test_recommendations_are_product_specific_and_not_generic(db_session: Session) -> None:
    products = [
        ("basalt", "basalt stones", "אבני בזלת לגריל גז"),
        ("vacuum", "vacuum grooved bags size 20x30", "שקיות ואקום"),
        ("tandoor", "tandoor roma model", "קאזן"),
        ("grill", "professional grill", "גריל גז"),
        ("smoker", "pellet smoker", "מעשנת"),
        ("knife", "chef knife", "סכין"),
    ]
    for pid, name, _expected in products:
        db_session.add(
            IStoreProduct(
                istore_product_id=pid,
                product_name=name,
                canonical_url=f"https://example.com/products/{pid}",
                category="ציוד",
                meta_title="",
                meta_description="",
            )
        )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=20)
    copies = "\n".join(fix["copy_text"] for item in dashboard["audits"] for fix in item["ready_to_copy_fixes"])

    for forbidden in ["מידע ברור, השוואה וטיפים", "מתאים ללקוחות שמחפשים", "התאמה לשימוש יומיומי", "פתרון איכותי"]:
        assert forbidden not in copies
    for _pid, _name, expected in products:
        assert expected in copies
    assert all(
        isinstance(fix["copy_text"], str) and "built-in method" not in fix["copy_text"]
        for item in dashboard["audits"]
        for fix in item["ready_to_copy_fixes"]
    )


def test_prioritization_non_food_categories_quick_wins_and_unclear_products(db_session: Session) -> None:
    db_session.add_all(
        [
            IStoreProduct(
                istore_product_id="bad",
                product_name="assman",
                canonical_url="https://example.com/products/assman",
                category="unknown",
                meta_title="",
                meta_description="",
            ),
            IStoreProduct(
                istore_product_id="food",
                product_name="סטייק אנטריקוט",
                canonical_url="https://example.com/products/steak",
                category="בשר",
                meta_title="",
                meta_description="",
            ),
            IStoreProduct(
                istore_product_id="good",
                product_name="אבני בזלת לגריל גז",
                canonical_url="https://example.com/products/basalt",
                category="אבני בזלת",
                meta_title="",
                meta_description="",
            ),
        ]
    )
    _add_metric(
        db_session,
        page_url="https://example.com/products/basalt",
        metric_date=date(2026, 5, 25),
        impressions=500,
        clicks=2,
        query="אבני בזלת לגריל",
    )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=10)
    names = [item["name"] for item in dashboard["audits"]]
    assert names.index("אבני בזלת") < names.index("assman")
    basalt = next(item for item in dashboard["audits"] if item["url"] == "https://example.com/products/basalt")
    assert basalt["quick_win"] is True
    assert basalt["traffic_opportunity_score"] > 0
    assert dashboard["audits"][0]["entity_type"] == "category"


def test_command_center_and_fix_center_render_manual_workflow(client: TestClient, db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="p7",
            product_name="basalt stones",
            canonical_url="https://example.com/products/basalt",
            category="אבני בזלת",
            meta_title="",
            meta_description="",
        )
    )
    db_session.commit()

    command = client.get("/seo/command-center")
    assert command.status_code == 200
    assert "Top 20 Work Queue" in command.text
    assert "ידני בלבד" in command.text
    assert "אבני בזלת לגריל גז" in command.text

    fix_center = client.get("/seo/fix-center")
    assert fix_center.status_code == 200
    assert "שדה לשינוי" in fix_center.text
    assert "ערך מוצע" in fix_center.text
    assert "איפה להדביק" in fix_center.text
    assert "אין פרסום אוטומטי" in fix_center.text


def test_meat_products_classified_as_meat_food_not_grill(db_session: Session) -> None:
    for pid, name in [
        ("asado", "נשנושי אסאדו אנגוס FL ללא עצם"),
        ("entrecote", "סטייק אנטריקוט טרי"),
        ("burger", "המבורגר אנגוס קפוא"),
    ]:
        db_session.add(
            IStoreProduct(
                istore_product_id=pid,
                product_name=name,
                canonical_url=f"https://example.com/products/{pid}",
                category="בשר",
                meta_title="",
                meta_description="",
            )
        )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=20)

    for pid in ["asado", "entrecote", "burger"]:
        product = next(item for item in dashboard["audits"] if item["url"] == f"https://example.com/products/{pid}")
        assert product["product_family"] == "meat_food"
        assert product["product_family"] != "grill"


def test_meat_product_copy_is_meat_specific_and_not_gas_grill(db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="asado",
            product_name="נשנושי אסאדו אנגוס FL ללא עצם",
            canonical_url="https://example.com/products/asado",
            category="בשר",
            meta_title="",
            meta_description="",
        )
    )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=20)
    product = next(item for item in dashboard["audits"] if item["url"] == "https://example.com/products/asado")
    copy = "\n".join(fix["copy_text"] for fix in product["ready_to_copy_fixes"])

    assert product["product_family"] == "meat_food"
    assert "נשנושי אסאדו אנגוס" in copy
    assert "בישול איטי" in copy or "בישול ארוך" in copy
    assert "גריל גז מקצועי לגינה ולמטבח חוץ" not in copy
    assert "איך בוחרים גריל גז" not in copy


def test_default_top_20_queue_uses_non_food_focus_before_meat_food(db_session: Session) -> None:
    for pid, name, category in [
        ("asado", "נשנושי אסאדו אנגוס FL ללא עצם", "בשר"),
        ("basalt", "אבני בזלת לגריל גז", "אבני בזלת"),
        ("vacuum", "שקיות ואקום מחורצות", "ואקום"),
    ]:
        db_session.add(
            IStoreProduct(
                istore_product_id=pid,
                product_name=name,
                canonical_url=f"https://example.com/products/{pid}",
                category=category,
                meta_title="",
                meta_description="",
            )
        )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=20)
    queue_urls = [item["url"] for item in dashboard["top_20_work_queue"]]

    assert dashboard["non_food_focus_default"] is True
    assert "https://example.com/products/basalt" in queue_urls
    assert "https://example.com/products/vacuum" in queue_urls
    assert "https://example.com/products/asado" not in queue_urls


def test_non_food_products_keep_product_specific_copy(db_session: Session) -> None:
    products = [
        ("kazan", "קאזן אסייתי", "קאזן"),
        ("tandoor", "טנדור דגם רומא", "טנדור"),
        ("vacuum", "שקיות ואקום מחורצות", "שקיות ואקום"),
        ("skewer", "שיפודים רחבים", "שיפוד"),
    ]
    for pid, name, category in products:
        db_session.add(
            IStoreProduct(
                istore_product_id=pid,
                product_name=name,
                canonical_url=f"https://example.com/products/{pid}",
                category=category,
                meta_title="",
                meta_description="",
            )
        )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=20)

    expected = {
        "kazan": "קאזן",
        "tandoor": "טנדור",
        "vacuum": "שקיות ואקום",
        "skewer": "שיפוד",
    }
    for pid, term in expected.items():
        product = next(item for item in dashboard["audits"] if item["url"] == f"https://example.com/products/{pid}")
        copy = "\n".join(fix["copy_text"] for fix in product["ready_to_copy_fixes"])
        assert product["product_family"] != "meat_food"
        assert term in copy


def test_kazan_product_uses_istore_engine_without_hallucinated_unscanned_fields(db_session: Session) -> None:
    product_name = "קאזן אסייתי 6 ליטר עם מכסה מברזל יצוק ללא ציפוי"
    db_session.add(
        IStoreProduct(
            istore_product_id="kazan-6l",
            product_name=product_name,
            canonical_url="https://example.com/products/קאזן-אסייתי-6-ליטר-עם-מכסה",
            category="קאזן",
            meta_title="",
            meta_description="",
        )
    )
    db_session.commit()

    dashboard = build_product_category_audit_center(db_session, limit=20)
    product = next(item for item in dashboard["audits"] if item["url"].startswith("https://example.com/products/"))
    copy = "\n".join(fix["copy_text"] for fix in product["ready_to_copy_fixes"])
    slug_fix = next(fix for fix in product["ready_to_copy_fixes"] if fix["field_key"] == "suggested_slug")
    field_keys = {fix["field_key"] for fix in product["ready_to_copy_fixes"]}

    assert product["detected_family"] == "kazan"
    assert product["product_family"] == "kazan"
    assert product["confidence_score"] >= 90
    assert product["review_status"] == "Unknown – manual review required"
    assert slug_fix["copy_text"] == "kazan-asian-6-liter-lid-cast-iron"
    assert not any("\u0590" <= char <= "\u05FF" for char in slug_fix["copy_text"])
    assert "טנדור" not in copy
    assert "tandoor" not in copy.lower()
    assert {"h1", "short_product_description", "long_product_description", "faq"}.isdisjoint(field_keys)
