from __future__ import annotations

from collections.abc import Generator
from difflib import SequenceMatcher
from pathlib import Path
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import _article_quality_summary
from app.db.database import Base
from app.services import content_articles as service


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = local()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def no_remote_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_discover_related_links", lambda _db, _topic, limit=6: ([], {"mocked_links": True}))
    monkeypatch.setattr(service, "_related_products", lambda _db, _topic, limit=6: [])


REGRESSION_CASES = [
    ("פיקניה", "פיקניה", "how-to", "meat_quick_grill_cut", ["שכבת שומן", "שיוש", "54–57°C", "חיתוך נגד הסיבים"], ["74°C", "גלייז", "זמן בעירה"], ["beef", "steak"]),
    ("בריסקט", "בריסקט", "how-to", "meat_low_slow_smoking", ["105–120°C", "Bark", "סטול", "נייר קצבים", "90–96°C"], ["74°C", "54–57°C", "גלייז", "אפר"], ["smoker", "bark"]),
    ("כנפיים קריספיות", "כנפיים קריספיות", "how-to", "poultry_grill_recipe", ["ייבוש", "בטיחות מזון", "74°C", "גלייז", "סוכר שרוף"], ["54–57°C", "סטול", "Bark", "אפר"], ["chicken", "wings"]),
    ("פחם / פחם קוקוס", "פחם / פחם קוקוס", "comparison", "fuel_comparison_or_guide", ["זמן בעירה", "יציבות חום", "רמת עשן", "אפר", "עלות מול ביצועים"], ["74°C", "54–57°C", "גלייז", "סטול"], ["charcoal", "fuel"]),
    ("שבבי עץ לעישון", "שבבי עץ לעישון", "commercial", "smoking_wood_guide", ["פרופיל טעם", "שבבים", "צ׳אנקים", "השריה", "thin blue smoke"], ["74°C", "54–57°C", "גלייז", "אפר"], ["wood", "smoke"]),
    ("אבני בזלת לגריל", "אבני בזלת לגריל", "commercial", "grill_accessory_guide", ["מה זה", "איך זה עובד", "התקנה", "ניקוי", "תחזוקה", "שיקולי קנייה"], ["74°C", "54–57°C", "גלייז", "סטול"], ["accessory", "grill"]),
    ("גריל גז", "גריל גז", "commercial", "equipment_buying_guide", ["תרחיש שימוש", "גודל", "BTU", "איכות חומר", "תחזוקה", "למי זה מתאים"], ["74°C", "54–57°C", "גלייז", "סטול"], ["equipment", "buying"]),
]


@pytest.mark.parametrize("title,keyword,intent,topic_type,required_terms,forbidden_terms,image_terms", REGRESSION_CASES)
def test_topic_type_contract_regression(
    db_session: Session,
    title: str,
    keyword: str,
    intent: str,
    topic_type: str,
    required_terms: list[str],
    forbidden_terms: list[str],
    image_terms: list[str],
) -> None:
    draft = service.generate_topic_article_draft(db_session, topic_title=title, focus_keyword=keyword, target_intent=intent)
    debug = service._classify_topic(title, keyword, intent)
    quality = _article_quality_summary(draft)

    assert debug["topic_type"] == topic_type
    assert draft.status == "READY_FOR_REVIEW"
    assert quality["publish_readiness"] == "READY_FOR_REVIEW"
    for term in required_terms:
        assert term in draft.article_body
    for term in forbidden_terms:
        assert term not in draft.article_body
    assert keyword.split()[0] in draft.meta_description
    assert any(term in draft.featured_image_prompt.lower() for term in image_terms)


def test_end_to_end_random_contract_articles_are_distinct(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    ordered_topics = iter(service.TOPIC_POOL[:5])
    monkeypatch.setattr(service.random, "choice", lambda eligible: next((topic for topic in ordered_topics if topic in eligible), eligible[0]))

    drafts = [service.generate_daily_article_draft(db_session, randomize=True)[0] for _ in range(5)]
    titles = [draft.title for draft in drafts]
    bodies = [draft.article_body for draft in drafts]
    topic_types = [service._classify_topic(draft.topic_title, draft.focus_keyword, draft.target_intent)["topic_type"] for draft in drafts]

    assert len(set(titles)) == 5
    assert len(set(topic_types)) >= 4
    for i, body in enumerate(bodies):
        assert "נושא שמכריע אם תקבלו תוצאה בינונית" not in body
        assert service._meaningful_title_terms(titles[i], drafts[i].focus_keyword)[0] in body
        assert _article_quality_summary(drafts[i])["publish_readiness"] == "READY_FOR_REVIEW"
        for other in bodies[i + 1 :]:
            assert SequenceMatcher(None, body, other).ratio() < 0.72


def test_accessory_topic_uses_basalt_entity_contract(db_session: Session) -> None:
    draft = service.generate_topic_article_draft(
        db_session,
        topic_title="Basalt stones for gas grill",
        focus_keyword="Basalt stones for gas grill",
        target_intent="commercial_informational",
    )
    profile = service._classify_topic(draft.topic_title, draft.focus_keyword, draft.target_intent)

    assert profile["topic_type"] == "grill_accessory_guide"
    assert profile["entity_key"] == "basalt_stones"
    for term in [
        "אבני לבה",
        "אבני בזלת",
        "lava rocks",
        "פיזור חום",
        "הפחתת התלקחויות",
        "מבערים",
        "אידוי שומן",
        "יציבות טמפרטורה",
        "מרווחי החלפה",
        "טעויות נפוצות",
    ]:
        assert term in draft.article_body
    assert "probe" not in draft.article_body
    assert "basalt" in draft.featured_image_prompt.lower()
    assert "lava rocks" in draft.featured_image_prompt.lower()


def test_accessory_topic_uses_thermometer_entity_contract(db_session: Session) -> None:
    draft = service.generate_topic_article_draft(
        db_session,
        topic_title="איך לבחור מדחום לבשר",
        focus_keyword="מדחום לבשר",
        target_intent="commercial_informational",
    )
    profile = service._classify_topic(draft.topic_title, draft.focus_keyword, draft.target_intent)

    assert profile["topic_type"] == "grill_accessory_guide"
    assert profile["entity_key"] == "thermometer"
    for term in ["מדחום", "probe", "קריאה מהירה", "כיול", "טמפרטורה פנימית", "זמן תגובה", "ניקוי", "טעויות נפוצות"]:
        assert term in draft.article_body
    for basalt_term in ["lava rocks", "אידוי שומן", "הפחתת התלקחויות", "אבני לבה"]:
        assert basalt_term not in draft.article_body
    assert "thermometer" in draft.featured_image_prompt.lower()
    assert "basalt" not in draft.featured_image_prompt.lower()


def test_topic_seo_metadata_expands_entity_specific_keywords(db_session: Session) -> None:
    draft = service.generate_topic_article_draft(
        db_session,
        topic_title="פיקניה",
        focus_keyword="פיקניה",
        target_intent="how-to",
    )
    profile = service._classify_topic(draft.topic_title, draft.focus_keyword, draft.target_intent)
    metadata = service.build_topic_seo_metadata(draft.focus_keyword, draft.title, profile)
    forbidden_internal_names = service.INTERNAL_SEO_CONTRACT_TERMS

    assert not any(term in draft.meta_title for term in forbidden_internal_names)
    assert metadata["primary_keyword"] == "פיקניה"
    assert len(metadata["seo_keywords"]) >= 8
    assert all("פיקניה" in keyword or keyword in {"מדחום לבשר", "גריל פחמים", "מלח גס"} for keyword in metadata["seo_keywords"])
    assert "פיקניה" in draft.meta_description
    assert "פיקניה על הגריל" in draft.meta_description
    assert 140 <= len(draft.meta_description) <= 160
    assert metadata["seo_score"] >= 85
    for expected_phrase in ["איך לצלות פיקניה", "טמפרטורת פיקניה", "Reverse Sear פיקניה", "פיקניה גריל גז"]:
        assert expected_phrase in metadata["seo_keywords"]


def _normalized_html_text(value: str) -> str:
    return service._normalize_hebrew(service._plain_text(value))


def test_postprocess_removes_repeated_section_titles_paragraphs_and_faq_blocks() -> None:
    html = """
    <h2>הערת עומק נוספת לכנפיים</h2><p>אותו טקסט חוזר על ייבוש הכנפיים לפני הצלייה.</p>
    <h2>הערת עומק נוספת לכנפיים</h2><p>אותו טקסט חוזר על ייבוש הכנפיים לפני הצלייה.</p>
    <h3>שאלות נפוצות</h3><ul><li>שאלת כנפיים נפוצה?</li></ul>
    <h3>שאלות נפוצות</h3><ul><li>שאלת כנפיים נפוצה?</li></ul>
    """
    faq = {
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "כמה זמן צולים כנפיים?"},
            {"@type": "Question", "name": "כמה זמן צולים כנפיים?"},
        ],
    }

    body, _title, deduped_faq = service._postprocess_article_assets(html, "כנפיים קריספיות", faq)

    assert body.count("<h2>הערת עומק נוספת לכנפיים</h2>") == 1
    assert body.count("אותו טקסט חוזר על ייבוש הכנפיים לפני הצלייה") == 1
    assert body.count("<h3>שאלות נפוצות</h3>") == 1
    assert body.count("שאלת כנפיים נפוצה") == 1
    assert isinstance(deduped_faq, dict)
    assert len(deduped_faq["mainEntity"]) == 1


def test_generated_article_has_no_repeated_section_titles_or_paragraphs(db_session: Session) -> None:
    draft = service.generate_topic_article_draft(
        db_session,
        topic_title="כנפיים קריספיות על הגריל",
        focus_keyword="כנפיים קריספיות על הגריל",
        target_intent="how-to",
    )
    section_titles = [_normalized_html_text(match) for match in re.findall(r"<h2[^>]*>(.*?)</h2>", draft.article_body)]
    paragraphs = [_normalized_html_text(match) for match in re.findall(r"<p[^>]*>(.*?)</p>", draft.article_body)]

    assert len(section_titles) == len(set(section_titles))
    assert len(paragraphs) == len(set(paragraphs))


def test_meta_title_normalization_removes_duplicated_phrases() -> None:
    normalized = service._normalize_meta_title("כנפיים קריספיות על הגריל על הגריל | Compass Grill")

    assert "על הגריל על הגריל" not in normalized
    assert normalized == "כנפיים קריספיות על הגריל | Compass Grill"


def test_employee_copy_keyword_field_uses_expanded_keyword_list() -> None:
    template = Path("app/templates/seo_simple_workspace.html").read_text()

    assert "expanded_keywords_text = expanded_keywords | join(', ')" in template
    assert '<h5>7. מילות מפתח (Keywords)</h5><textarea readonly>{{ expanded_keywords_text }}</textarea>' in template
    assert 'data-copy-text="{{ expanded_keywords_text }}"' in template


def test_poultry_semantic_gate_rejects_unrelated_products() -> None:
    topic = "כנפיים קריספיות על הגריל"
    terms = [*service._match_terms_for_topic(topic), *service._topic_synonyms(topic)]
    unrelated = [
        "cast iron meat holder מחזיק בשר יצוק אביזרים לגריל",
        "cinnamon kebab קינמון קבב גריל",
        "bear claws טופרי דוב לפירוק בשר",
        "gas burner מבער גז לגריל",
    ]
    related = "מדחום לבשר כנפיים chicken wings thermometer"

    for candidate in unrelated:
        scores = service._score_link_candidate(topic, candidate, "product", terms)
        assert not service._passes_link_semantic_gate(topic, candidate, "product", scores)
    related_scores = service._score_link_candidate(topic, related, "product", terms)
    assert service._passes_link_semantic_gate(topic, related, "product", related_scores)
