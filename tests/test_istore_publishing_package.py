from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.main import app

from app.services.content_articles import (
    build_multi_image_package,
    build_istore_copy_paste_package,
    calculate_diversity_score,
    _classify_topic,
    validate_complete_publishing_package,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = local()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("topic_title", "focus_keyword", "slug"),
    [
        ("מדריך עישון בריסקט למתחילים", "בריסקט", "brisket-smoking-guide"),
        ("אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז", "אבני בזלת לגריל", "basalt-stones-for-gas-grill"),
        ("איך לבחור שבבי עץ לעישון בשר", "שבבי עץ לעישון", "wood-chips-for-smoking-meat"),
        ("איך לבחור גריל גז לגינה", "גריל גז", "choose-gas-grill-for-garden"),
    ],
)
def test_topic_articles_include_visual_formatter_and_istore_package(client: TestClient, topic_title: str, focus_keyword: str, slug: str) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": topic_title,
            "focus_keyword": focus_keyword,
            "target_intent": "commercial_informational",
            "preferred_slug": slug,
        },
    )
    assert response.status_code == 200
    draft_id = response.json()["draft"]["draft_id"]
    draft = client.get(f"/content/articles/{draft_id}").json()["draft"]
    body = draft["article_body"]
    metadata = draft["image_generation_metadata"]

    assert "intro-summary" in body
    assert "professional-tip" in body
    assert "common-mistake" in body
    assert "article-checklist" in body
    assert "<table" in body
    assert "article-cta" in body
    assert "❓" in body
    for marker in ["<!-- IMAGE_1 -->", "<!-- IMAGE_2 -->", "<!-- IMAGE_3 -->", "<!-- IMAGE_4 -->"]:
        assert marker in body

    assert len(metadata["image_package"]) == 5
    assert len(metadata["image_placement_guide"]) >= 5
    assert metadata["istore_copy_paste_package"]["mode"] == "ISTORE_COPY_PASTE"
    assert metadata["final_qa_validation"]["publish_readiness"] == "READY_FOR_REVIEW"


def test_multi_image_package_alt_validator_rejects_duplicates_and_generic_values() -> None:
    profile = _classify_topic("מדריך עישון בריסקט למתחילים", "בריסקט", "how-to")
    package = build_multi_image_package("מדריך עישון בריסקט למתחילים", "בריסקט", "brisket-smoking-guide", profile, "finished brisket")
    images = package["image_package"]
    alts = [image["alt"] for image in images]
    assert len(images) == 5
    assert len(set(alts)) == 5
    assert not {alt.lower() for alt in alts} & {"image", "photo", "grill image", "bbq image"}
    assert all(image["filename"] and image["caption"] and image["prompt"] for image in images)


def test_istore_copy_paste_mode_splits_blocks_and_image_steps() -> None:
    images = [
        {"key": "featured_image", "filename": "cover.jpg", "alt": "cover alt", "caption": "cover", "prompt": "cover", "image_url": ""},
        {"key": "image_1", "filename": "one.jpg", "alt": "one alt", "caption": "one", "prompt": "one", "image_url": ""},
        {"key": "image_2", "filename": "two.jpg", "alt": "two alt", "caption": "two", "prompt": "two", "image_url": ""},
        {"key": "image_3", "filename": "three.jpg", "alt": "three alt", "caption": "three", "prompt": "three", "image_url": ""},
        {"key": "image_4", "filename": "four.jpg", "alt": "four alt", "caption": "four", "prompt": "four", "image_url": ""},
    ]
    body = "<p>פתיח</p><!-- IMAGE_1 --><h2>🔥 חלק ראשון</h2><p>תוכן</p><!-- IMAGE_2 --><h2>❓ שאלות נפוצות</h2><h3>❓ שאלה</h3><p>✅ תשובה</p>"
    package = build_istore_copy_paste_package("Title", "slug", "Meta", "Description", body, images)
    labels = [step["label"] for step in package["steps"]]
    assert labels[:5] == ["Copy into Title field", "Copy into Slug field", "Copy into Meta Title", "Copy into Meta Description", "Upload Featured Image"]
    assert "Insert image_1" in labels
    assert "Insert image_2" in labels


def test_diversity_scoring_detects_repeated_structure() -> None:
    first = "<div class='intro-summary'></div><h2>🔥 הכנת המעשנה</h2><!-- IMAGE_1 --><h2>🌳 בחירת עצי עישון</h2><div class='article-cta'></div>"
    similar = "<div class='intro-summary'></div><h2>🔥 הכנת המעשנה</h2><!-- IMAGE_1 --><h2>🌳 בחירת עצי עישון</h2><div class='article-cta'></div>"
    different = "<h2>📊 טבלת השוואה</h2><h2>✅ צ׳קליסט</h2><!-- IMAGE_3 -->"
    assert calculate_diversity_score(similar, [first])["passed"] is False
    assert calculate_diversity_score(different, [first])["passed"] is True


def test_final_qa_validation_requires_complete_package() -> None:
    body = "<div class='intro-summary'></div><p>Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון Article body with enough Hebrew תוכן מקצועי לצלייה ועישון</p><!-- IMAGE_1 --><!-- IMAGE_2 --><!-- IMAGE_3 --><!-- IMAGE_4 --><div class='professional-tip'>טיפ מקצועי</div><div class='common-mistake'>טעות נפוצה</div><ul class='article-checklist'><li>✅ ציוד</li></ul><table><tr><td>x</td></tr></table><h2>❓ שאלות נפוצות</h2><div class='article-cta'>CTA</div>"
    images = [{"key": f"image_{i}", "filename": f"{i}.jpg", "alt": f"תיאור ייחודי לתמונה {i}", "caption": "caption", "prompt": "prompt", "image_url": ""} for i in range(5)]
    images[0]["key"] = "featured_image"
    guide = [{"image": image["key"], "instruction": "Place", "section": "section"} for image in images]
    istore = {"mode": "ISTORE_COPY_PASTE", "steps": [{"step": 1, "label": "Copy into Title field", "value": "Title"}]}
    qa = validate_complete_publishing_package(body, images, guide, istore, {"passed": True})
    assert qa["publish_readiness"] == "READY_FOR_REVIEW"
