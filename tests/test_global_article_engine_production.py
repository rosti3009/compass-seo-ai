import re

from app.services import content_articles as service


def _profile(title: str, keyword: str = "נושא", intent: str = "commercial") -> dict[str, object]:
    return service._classify_topic(title, keyword, intent)


def _article(title: str, keyword: str = "נושא", intent: str = "commercial", links=None) -> tuple[str, dict[str, object]]:
    profile = _profile(title, keyword, intent)
    body = service._build_article_html(title, keyword, links or [], topic_profile=profile)
    body, _, _ = service._postprocess_article_assets(body, "", topic_profile=profile)
    return body, profile


def test_comparison_signal_routing_forces_global_comparison_contract() -> None:
    cases = [
        "טאבון גז או טאבון עצים",
        "גריל גז מול גריל פחמים",
        "שבבי עץ לעומת צ׳אנקים לעישון",
        "gas grill vs charcoal grill",
    ]
    for title in cases:
        profile = _profile(title, title, "commercial")
        assert profile["detected_intent"] == "comparison_article"
        assert profile["selected_contract"] == "comparison_article"
        assert profile["article_contract"] == "comparison_article"
        assert profile["router_reason"] == "comparison_signal_forced_contract"


def test_comparison_contract_validation_requires_table_pros_cons_and_recommendation() -> None:
    body, profile = _article("גריל גז מול גריל פחמים", "גריל גז", "commercial")
    validation = service.validate_article_contract(body, profile)

    assert validation["contract_validation_status"] == "passed"
    assert "<table" in body
    assert len(re.findall(r"<h2[^>]*>.*?יתרונות.*?</h2>", body, flags=re.S)) >= 2
    assert len(re.findall(r"<h2[^>]*>.*?חסרונות.*?</h2>", body, flags=re.S)) >= 2
    assert "המלצה סופית" in service._plain_text(body)

    invalid_body = "<p>השוואה קצרה</p><h2>שאלות נפוצות</h2><h3>❓ שאלה?</h3><p>✅ תשובה</p><div class='article-cta'>CTA</div>"
    invalid = service.validate_article_contract(invalid_body, profile)
    assert invalid["contract_validation_status"] == "failed"
    assert "comparison_table" in invalid["contract_validation_missing"]
    assert any("advantages" in item or "disadvantages" in item for item in invalid["contract_validation_missing"])


def test_product_recommendations_filter_weak_or_unrelated_matches_and_omit_empty_block() -> None:
    profile = _profile("מדריך כללי לגריל", "גריל", "commercial")
    weak = [{"title": "אביזר עם מילת גריל חלשה", "url": "https://compassgrill.co.il/products/weak", "type": "product", "relevance_score": 79}]
    assert service._links_section(weak, profile) == ""

    related_category = [{"title": "אביזרי גריל", "url": "https://compassgrill.co.il/categories/grill-accessories", "type": "category", "relevance_score": 91}]
    block = service._links_section(related_category, profile)
    assert "ציוד מומלץ לנושא המאמר" in block
    assert "אביזרי גריל" in block


def test_recommendation_output_contains_only_customer_facing_fields() -> None:
    profile = _profile("בריסקט", "בריסקט", "how-to")
    block = service._links_section([
        {
            "title": "בריסקט פרימיום",
            "url": "https://compassgrill.co.il/products/brisket",
            "type": "product",
            "relevance_score": 96,
            "link_role": "exact_entity",
            "reason": "exact_entity; התאמת ביטויי חיפוש: בריסקט; עדיפות סוג עמוד: product",
        }
    ], profile)
    forbidden = ["exact_entity", "complementary", "התאמת ביטויי חיפוש", "עדיפות סוג עמוד", "matching score", "category score"]
    assert all(term not in block for term in forbidden)
    assert "<strong><a href='https://compassgrill.co.il/products/brisket'>בריסקט פרימיום</a></strong>" in block
    assert "<span>" in block


def test_natural_writing_has_no_mechanical_intro_insights_or_recommendation_phrases() -> None:
    body, _profile_obj = _article("טאבון גז או טאבון עצים", "טאבון", "commercial")
    forbidden = [
        "אם נראה פשוט על הנייר אבל התוצאה לא עקבית",
        "מוצר רלוונטי ליישום ההמלצות במדריך",
        "עוזר לפתור צורך מעשי",
        "צריך להיבחר רק אם הוא פותר צורך אמיתי",
        "בדיקת התאמה אמיתית",
        "החלטה לפי סימנים",
        "קנייה רק כשיש צורך",
    ]
    assert all(phrase not in body for phrase in forbidden)


def test_hebrew_cta_category_names_and_english_minimized() -> None:
    body, _profile_obj = _article("גריל גז", "גריל גז", "category")
    assert "גרילי גז" in body
    assert "גרילי פחמים" in body
    assert "מטבחי חוץ" in body
    assert "Gas Grills" not in body
    assert "Charcoal Grills" not in body
    assert "Outdoor Kitchens" not in body


def test_publishing_qa_blocks_wrong_contract_invalid_comparison_and_unrelated_links() -> None:
    profile = _profile("גריל גז מול גריל פחמים", "גריל גז", "commercial")
    wrong_profile = dict(profile, article_contract="buying_guide_article", selected_contract="buying_guide_article")
    final = service.validate_final_article_quality(
        "<p>השוואה</p><h2>שאלות נפוצות</h2><h3>❓ שאלה?</h3><p>✅ תשובה</p><div class='article-cta'>CTA</div>",
        "כותרת",
        {"seo_keywords": [str(i) for i in range(8)], "seo_score": 95},
        wrong_profile,
        [{"title": "מוצר לא קשור", "url": "https://compassgrill.co.il/products/x", "type": "product", "relevance_score": 45}],
        "גריל גז מול גריל פחמים",
    )
    assert final["final_quality_passed"] is False
    assert any("contract_validation_failed" in issue for issue in final["final_quality_issues"])
    assert any("selected_link_relevance_below_80" in issue for issue in final["final_quality_issues"])
