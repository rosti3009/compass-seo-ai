from app.services.content_articles import (
    _classify_topic,
    _enforce_phase2_article_quality,
    _html_duplicate_issues,
    build_multi_image_package,
    validate_complete_publishing_package,
)
from app.services.image_generation import IMAGE_PROVIDER_REGISTRY, build_realistic_hero_prompt


def test_phase2_quality_gate_removes_filler_merges_duplicates_and_limits_h2() -> None:
    profile = _classify_topic("אבני בזלת לגריל", "אבני בזלת לגריל", "commercial_informational")
    html = "<p>פתיח</p>"
    html += "<h2>מה זה אבני בזלת</h2><p>אבני בזלת לגריל גז מפזרות חום.</p>"
    html += "<h2>מה זה ולמי זה מתאים</h2><p>אבני בזלת לגריל גז מפזרות חום.</p>"
    html += "<h2>העמקה מעשית 1</h2><p>תכנון מראש וניהול לחות.</p>"
    for idx in range(20):
        html += f"<h2>סעיף בזלת ייחודי {idx}</h2><p>מידע על אבני בזלת ופיזור חום {idx}.</p>"
    html += "<h2>❓ שאלות נפוצות</h2><h3>❓ האם אבני בזלת מתאימות לכל גריל?</h3><p>✅ לא, בודקים התאמה.</p>"
    html += "<div class='article-cta'><h2>🛒 CTA</h2><p>אבני בזלת.</p></div>"

    cleaned = _enforce_phase2_article_quality(html, profile)

    assert "העמקה מעשית" not in cleaned
    assert "תכנון מראש" not in cleaned
    assert len(__import__("re").findall(r"<h2", cleaned)) <= 12
    assert cleaned.count("article-cta") == 1
    assert len(__import__("re").findall(r"<h3[^>]*>\s*❓", cleaned)) >= 5
    issues, duplicates = _html_duplicate_issues(cleaned)
    assert "duplicate_h2_titles" not in issues
    assert duplicates == []


def test_phase2_image_package_has_clean_filenames_complete_metadata_and_commercial_prompts() -> None:
    profile = _classify_topic("אבני בזלת לגריל", "אבני בזלת לגריל", "commercial_informational")
    package = build_multi_image_package("אבני בזלת לגריל", "אבני בזלת לגריל", "basalt-stones-for-gas-grill", profile, "basalt stones in gas grill")
    images = package["image_package"]

    assert len(images) == 5
    for image in images:
        assert " " not in image["filename"]
        assert image["filename"] == image["filename"].lower()
        assert image["filename"].endswith(".jpg")
        assert all(image.get(field) is not None for field in ["filename", "title", "alt", "caption", "prompt", "generated_url", "preview_url", "status"])
        assert "photorealistic commercial quality BBQ magazine photography" in image["prompt"]
        assert image["status"] == "planned"


def test_ready_for_publishing_requires_generated_images() -> None:
    body = " ".join(["<p>תוכן מקצועי אבני בזלת לגריל עם קישור <a href='https://compassgrill.co.il/categories/basalt-lava-stones'>אבני בזלת</a></p>"] * 20)
    body += "<!-- IMAGE_1 --><!-- IMAGE_2 --><!-- IMAGE_3 --><!-- IMAGE_4 --><div class='professional-tip'>טיפ מקצועי</div><div class='common-mistake'>טעות נפוצה</div><ul class='article-checklist'><li>✅ בדיקה</li></ul><table><tr><td>x</td></tr></table><h2>❓ שאלות נפוצות</h2><div class='article-cta'>CTA</div>"
    images = [
        {"key": "featured_image" if idx == 0 else f"image_{idx}", "filename": f"img-{idx}.jpg", "title": "title", "alt": f"תיאור ספציפי {idx}", "caption": "caption", "prompt": "prompt", "generated_url": "", "preview_url": "", "status": "planned", "image_url": ""}
        for idx in range(5)
    ]
    guide = [{"image": image["key"], "instruction": "Place", "section": "section"} for image in images]
    istore = {"mode": "ISTORE_COPY_PASTE", "steps": [{"step": 1, "label": "Copy into Title field", "value": "Title"}]}

    review = validate_complete_publishing_package(body, images, guide, istore, {"passed": True})
    assert review["publish_readiness"] == "NEEDS_REWRITE"
    assert "legacy_template_free" in review["publishing_package_failed_checks"]

    for image in images:
        image["status"] = "generated"
        image["generated_url"] = f"https://cdn.example/{image['filename']}"
        image["preview_url"] = image["generated_url"]
    ready = validate_complete_publishing_package(body, images, guide, istore, {"passed": True})
    assert ready["publish_readiness"] == "NEEDS_REWRITE"
    assert "legacy_template_free" in ready["publishing_package_failed_checks"]


def test_image_provider_registry_and_realistic_prompt_rules() -> None:
    assert {"openai", "flux", "ideogram", "recraft"}.issubset(IMAGE_PROVIDER_REGISTRY)
    prompt = build_realistic_hero_prompt("אבני בזלת בגריל גז")
    assert "photorealistic commercial quality BBQ magazine photography" in prompt
    assert build_realistic_hero_prompt(prompt) == prompt
