from bs4 import BeautifulSoup

from app.services.crawler import SEOCrawler


def _soup(body: str) -> BeautifulSoup:
    return BeautifulSoup(body, "html.parser")


def _crawler() -> SEOCrawler:
    return SEOCrawler("https://example.com", max_pages=5)


def test_brand_archive_path_overrides_product_signals() -> None:
    crawler = _crawler()
    soup = _soup(
        """
        <html><head>
            <title>Boretti grills and accessories archive</title>
            <meta name="description"
                  content="Boretti brand archive with grills, pizza ovens and premium accessories.">
            <link rel="canonical" href="https://example.com/brand/boretti">
            <meta property="og:type" content="product">
        </head><body>
            <h1>Boretti</h1><span itemprop="price">₪299</span><button>הוסף לסל</button>
            <article class="product-card"><span itemprop="brand">Boretti</span>גריל</article>
            <article class="product-card"><span itemprop="brand">Boretti</span>טאבון</article>
        </body></html>
        """
    )

    page_type, is_product, is_category = crawler._detect_page_type("https://example.com/brand/boretti", soup)

    assert page_type == "brand"
    assert is_product is False
    assert is_category is False


def test_article_blog_home_system_and_category_classification() -> None:
    crawler = _crawler()
    article_soup = _soup(
        '<html><head><meta property="og:type" content="article"></head><body><h1>Guide</h1></body></html>'
    )
    empty_soup = _soup("<html><body><h1>Page</h1></body></html>")

    assert crawler._detect_page_type("https://example.com/", empty_soup)[0] == "home"
    assert crawler._detect_page_type("https://example.com/account/login", empty_soup)[0] == "system"
    assert crawler._detect_page_type("https://example.com/blog", empty_soup)[0] == "blog"
    assert crawler._detect_page_type("https://example.com/blog/grill-guide", article_soup)[0] == "article"
    assert crawler._detect_page_type("https://example.com/category/grills", empty_soup)[0] == "category"


def test_generic_ai_metadata_penalizes_score_and_adds_remediation() -> None:
    crawler = _crawler()
    soup = _soup(
        """
        <html><head>
            <title>פתרון איכותי לגריל מקצועי לגינה</title>
            <meta name="description"
                  content="פתרון איכותי עם ביצועים מעולים, מקסימום נוחות ומתאים לשימוש מקצועי וביתי.">
            <link rel="canonical" href="https://example.com/products/gas-grill">
        </head><body><h1>גריל גז</h1><p>פתרון איכותי ביצועים מעולים פתרון איכותי ביצועים מעולים.</p></body></html>
        """
    )

    result = crawler._audit_page("https://example.com/products/gas-grill", 200, soup, 0)

    assert "generic_ai_title" in result.missing_fields
    assert "generic_ai_meta" in result.missing_fields
    assert "repetitive_ai_content" in result.missing_fields
    assert result.seo_score < 70
    assert result.seo_risk_level == "high"
    assert "rewrite_meta_description" in result.remediation_suggestions
    assert "shorten_title" in result.remediation_suggestions


def test_similarity_detection_flags_near_duplicate_titles_meta_and_openings() -> None:
    crawler = _crawler()
    pages = [
        crawler._audit_page(
            f"https://example.com/products/grill-{index}",
            200,
            _soup(
                f"""
                <html><head>
                    <title>גריל גז {title_word} לגינה ולמרפסת עם מבערים</title>
                    <meta name="description"
                          content="גריל גז {meta_word} לגינה ולמרפסת עם מבערים חזקים ומשלוח מהיר.">
                    <link rel="canonical" href="https://example.com/products/grill-{index}">
                </head><body><h1>גריל גז</h1><p>גריל איכותי לגינה עם מחיר ומשלוח.</p></body></html>
                """
            ),
            2,
        )
        for index, title_word, meta_word in [
            (1, "איכותי", "איכותי"),
            (2, "מקצועי", "מקצועי"),
            (3, "מעולה", "מעולה"),
        ]
    ]

    processed = crawler._apply_similarity_detection(pages)

    assert "duplicate_title_similarity" in processed[1].missing_fields
    assert "duplicate_meta_similarity" in processed[1].missing_fields
    assert "rewrite_meta_description" in processed[1].remediation_suggestions


def test_context_intelligence_detects_commercial_product_intent() -> None:
    crawler = _crawler()
    soup = _soup(
        """
        <html><head>
            <title>טאבון פיצה מקצועי לחצר</title>
            <meta name="description" content="טאבון פיצה לחצר עם משלוח מהיר, מחיר משתלם ואביזרים לאפייה.">
            <link rel="canonical" href="https://example.com/products/pizza-oven">
        </head><body>
            <nav class="breadcrumbs">בית / טאבונים / פיצה</nav>
            <h1>טאבון פיצה</h1><p>קנה טאבון פיצה עם משלוח ומבצע.</p>
        </body></html>
        """
    )

    result = crawler._audit_page("https://example.com/products/pizza-oven", 200, soup, 3)

    assert "pizza_ovens" in result.context_keywords
    assert result.primary_intent == "pizza_ovens"
    assert result.commercial_intent_score >= 0.6


def test_page_audit_api_payload_exposes_intelligence_fields() -> None:
    from app.db.models import PageAudit

    audit = PageAudit(
        crawl_run_id=1,
        url="https://example.com/products/pizza-oven",
        status_code=200,
        missing_fields="generic_ai_meta,thin_content",
        page_type="product",
        seo_risk_level="high",
        remediation_suggestions='["rewrite_meta_description", "expand_content"]',
        context_keywords='["pizza_ovens"]',
        primary_intent="pizza_ovens",
        commercial_intent_score=0.8,
    )

    payload = audit.to_dict()

    assert payload["page_type"] == "product"
    assert payload["seo_risk_level"] == "high"
    assert payload["remediation_suggestions"] == ["rewrite_meta_description", "expand_content"]
    assert payload["context_keywords"] == ["pizza_ovens"]
    assert payload["commercial_intent_score"] == 0.8
    assert "remediation_suggestions" not in payload["missing_fields"]
