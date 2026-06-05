import re

from app.services.content_articles import (
    _article_word_count,
    _classify_topic,
    _html_validation_issues,
    _postprocess_article_assets,
    _build_article_html,
    inject_internal_links_into_html,
    build_topic_seo_metadata,
    validate_final_article_quality,
    _generate_image_alt_text,
)


def _phase4_body(title: str, keyword: str, intent: str = "commercial_informational"):
    profile = _classify_topic(title, keyword, intent)
    related = [
        {
            "title": "אבני בזלת ולבה לגריל" if "בזלת" in keyword else "שבבי עץ לעישון",
            "url": "https://compassgrill.co.il/categories/basalt-lava-stones" if "בזלת" in keyword else "https://compassgrill.co.il/categories/smoking-wood-chips",
            "anchor_text": "אבני בזלת" if "בזלת" in keyword else "שבבי עץ",
            "reason": "התאמה ישירה לנושא המדריך",
            "relevance_score": 90,
            "link_role": "exact_entity",
        },
        {
            "title": "אביזרים לגריל",
            "url": "https://compassgrill.co.il/categories/grill-accessories",
            "anchor_text": "אביזרים לגריל",
            "reason": "ציוד משלים ליישום ההמלצות",
            "relevance_score": 75,
            "link_role": "related_category",
        },
    ]
    body = _build_article_html(title, keyword, related, topic_profile=profile)
    body, injected = inject_internal_links_into_html(body, related, profile)
    body, _, _ = _postprocess_article_assets(body, "", topic_profile=profile)
    return body, injected or related, profile


def test_phase4_expert_engine_outputs_human_expert_structure_and_single_faq_cta() -> None:
    body, links, profile = _phase4_body("אבני בזלת לגריל", "אבני בזלת לגריל")

    assert "expert-introduction" in body
    assert len(re.findall(r"class=['\"][^'\"]*expert-insight", body)) >= 3
    assert len(re.findall(r"<h2[^>]*>.*?שאלות נפוצות.*?</h2>", body, flags=re.S)) == 1
    assert 5 <= len(re.findall(r"<h3[^>]*>\s*❓", body)) <= 8
    assert body.count("article-cta") == 1
    assert "העמקה מעשית" not in body
    assert "שיקולים כלליים" not in body
    assert len(re.findall(r"<h2", body)) <= 12
    assert not _html_validation_issues(body)
    assert links


def test_phase4_quality_score_reaches_minimum_and_reports_all_dimensions() -> None:
    body, links, profile = _phase4_body("שבבי עץ לעישון", "שבבי עץ לעישון", "informational")
    seo = build_topic_seo_metadata("שבבי עץ לעישון", "שבבי עץ לעישון", profile)
    alt, _ = _generate_image_alt_text("שבבי עץ לעישון", "שבבי עץ לעישון", profile)
    result = validate_final_article_quality(body, str(seo["meta_title"]), seo, profile, links, alt)

    assert result["overall_quality_score"] >= 90
    assert result["expertise_score"] >= 90
    assert result["practical_value_score"] >= 90
    assert result["technical_accuracy_score"] >= 90
    assert result["duplicate_content_score"] >= 90
    assert result["human_review_validation"]["html_valid"] is True
    assert result["human_review_validation"]["faq_valid"] is True
    assert result["human_review_validation"]["cta_valid"] is True
    assert _article_word_count(body) >= result["required_word_count"]
