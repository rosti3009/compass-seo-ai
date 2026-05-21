from app.services.content_articles import _semantic_topic_match_score
from app.services.image_generation import build_realistic_hero_prompt, get_image_provider
from app.services.seo_quality_decision import evaluate_seo_text


class P:
    def __init__(self, name: str, slug: str = ""):
        self.product_name = name
        self.slug = slug
        self.product_url = "https://compassgrill.co.il/p/1"


def test_no_action_needed_status() -> None:
    result = evaluate_seo_text(
        target_url="https://compassgrill.co.il/p/1",
        field_path="meta_description",
        old_text="טקסט תקין",
        new_text="נראה שהטקסט הקיים כבר איכותי",
        page_type="product",
    )
    assert result.decision == "NO_ACTION_NEEDED"


def test_semantic_links_relevant_for_smoking_topic() -> None:
    product = P("שבבי עץ תפוח לעישון", "apple-wood-chips")
    bad = P("ריהוט גן חיצוני", "outdoor-furniture")
    assert _semantic_topic_match_score("שבבי עץ לעישון", product) >= 70
    assert _semantic_topic_match_score("שבבי עץ לעישון", bad) < 40


def test_image_provider_safely_disabled_and_prompt_realistic(monkeypatch) -> None:
    monkeypatch.setattr("app.services.image_generation.settings.image_provider", None)
    provider = get_image_provider()
    result = provider.generate_hero_image("bbq", draft_slug="test")
    assert result.enabled is False
    prompt = build_realistic_hero_prompt("Premium BBQ hero")
    assert "no text inside image" in prompt
    assert "no unrealistic meat" in prompt
