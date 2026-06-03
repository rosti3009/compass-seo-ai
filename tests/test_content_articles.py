from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import _blog_publish_adapter_ready
from app.db.database import Base, get_db
from app.db.models import ContentArticleDraft, IStoreProduct
from app.main import app
from app.services.istore_blog_publisher import IStoreBlogPublisher
from app.services.content_articles import (
    INTERNAL_SEO_CONTRACT_TERMS,
    _classify_topic,
    build_topic_seo_metadata,
    generate_topic_article_draft,
)


def _is_hebrew(text: str) -> bool:
    return any('\u0590' <= c <= '\u05FF' for c in text)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
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



def test_topic_seo_metadata_expands_entity_specific_keywords() -> None:
    topic_profile = _classify_topic("פיקניה", "פיקניה", "how-to")

    metadata = build_topic_seo_metadata("פיקניה", "פיקניה", topic_profile)

    assert metadata["meta_title"] == "איך לצלות פיקניה מושלמת על הגריל – מדריך מלא | Compass Grill"
    assert not any(term in metadata["meta_title"] for term in INTERNAL_SEO_CONTRACT_TERMS)
    assert 140 <= len(metadata["meta_description"]) <= 160
    assert "פיקניה" in metadata["meta_description"]
    assert "פיקניה על הגריל" in metadata["meta_description"]
    assert len(metadata["seo_keywords"]) >= 8
    assert all("פיקניה" in keyword for keyword in metadata["seo_keywords"])
    for phrase in [
        "פיקניה",
        "פיקניה על הגריל",
        "איך לצלות פיקניה",
        "טמפרטורת פיקניה",
        "פיקניה מדיום רייר",
        "Reverse Sear פיקניה",
        "חיתוך פיקניה",
        "סטייק פיקניה",
        "פיקניה גריל גז",
        "פיקניה על פחמים",
    ]:
        assert phrase in metadata["seo_keywords"]
    assert metadata["primary_keyword"] == "פיקניה"
    assert "פיקניה מדיום רייר" in metadata["secondary_keywords"]
    assert "Reverse Sear פיקניה" in metadata["long_tail_keywords"]
    assert metadata["seo_score"] >= 90


def test_topic_draft_diagnostics_show_expanded_seo_metadata(client: TestClient) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={"topic_title": "פיקניה", "focus_keyword": "פיקניה", "target_intent": "how-to", "preferred_slug": "picanha-on-grill"},
    )

    assert response.status_code == 200
    payload = response.json()
    diagnostics = payload["diagnostics"]
    draft = payload["draft"]
    assert diagnostics["primary_keyword"] == "פיקניה"
    assert "פיקניה מדיום רייר" in diagnostics["secondary_keywords"]
    assert "Reverse Sear פיקניה" in diagnostics["long_tail_keywords"]
    assert diagnostics["seo_score"] >= 90
    assert "how_to_grilling_guide" not in draft["meta_title"]
    assert "פיקניה" in draft["meta_description"]
    assert len(draft["debug"]["seo_keywords"]) >= 8

def test_workspace_has_article_controls(client: TestClient) -> None:
    response = client.get('/seo/simple-workspace')
    assert response.status_code == 200
    assert 'צור מאמר חדש' in response.text
    assert 'המאמר הפעיל לעבודה' in response.text
    assert 'כור מאמר לפי נושא' in response.text
    assert 'תמיכה בנושא יחיד בלבד' in response.text



def test_workspace_shows_single_active_article_and_compact_archive(client: TestClient) -> None:
    first = client.post('/content/articles/generate-daily-draft').json()['draft']
    second = client.post('/content/articles/generate-daily-draft').json()['draft']
    page = client.get('/seo/simple-workspace').text
    assert page.count('manual-upload-view · הכנה מהירה לעובד חנות') == 1
    assert 'מאמרים קודמים' in page
    assert second['title'] in page
    assert first['title'] in page


def test_newest_draft_becomes_active_after_generation(client: TestClient) -> None:
    first = client.post('/content/articles/generate-daily-draft').json()['draft']
    second = client.post('/content/articles/generate-random-daily-draft').json()
    drafts = client.get('/content/articles/drafts').json()['drafts']
    active = next(d for d in drafts if d['is_active_manual_article'])
    assert active['id'] == second['draft_id']
    assert active['id'] != first['id']


def test_set_active_and_archive_endpoints(client: TestClient) -> None:
    first = client.post('/content/articles/generate-daily-draft').json()['draft']
    second = client.post('/content/articles/generate-daily-draft').json()['draft']
    resp = client.post(f"/content/articles/{first['id']}/set-active")
    assert resp.status_code == 200
    drafts = client.get('/content/articles/drafts').json()['drafts']
    active = next(d for d in drafts if d['is_active_manual_article'])
    assert active['id'] == first['id']

    archive = client.post(f"/content/articles/{first['id']}/archive-manual-work")
    assert archive.status_code == 200
    after = client.get('/content/articles/drafts').json()['drafts']
    now_active = next(d for d in after if d['is_active_manual_article'])
    archived = next(d for d in after if d['id'] == first['id'])
    assert now_active['id'] == second['id']
    assert archived['is_active_manual_article'] is False

def test_generate_topic_draft_returns_single_draft_and_manual_fields(client: TestClient) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז",
            "focus_keyword": "אבני בזלת לגריל",
            "target_intent": "commercial_informational",
            "preferred_slug": "basalt-stones-for-gas-grill",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    draft = payload["draft"]
    assert payload["auto_publish"] is False
    assert draft["slug"] == "basalt-stones-for-gas-grill"
    assert draft["manual_upload_url"].startswith("/seo/simple-workspace#article-")
    assert "<h1" not in client.get(f"/content/articles/{draft['draft_id']}").json()["draft"]["article_body"].lower()


def test_generate_topic_draft_uses_only_first_topic_when_array_received(client: TestClient) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": ["נושא ראשון", "נושא שני"],
            "focus_keyword": "מילת מפתח",
            "target_intent": "commercial_informational",
            "preferred_slug": "custom-topic-slug",
        },
    )
    assert response.status_code == 200
    draft_id = response.json()["draft"]["draft_id"]
    draft = client.get(f"/content/articles/{draft_id}").json()["draft"]
    assert draft["title"] == "נושא ראשון"


def test_generate_topic_draft_no_additional_queue_or_cluster_or_schedule_runs(client: TestClient) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז",
            "focus_keyword": "אבני בזלת לגריל",
            "target_intent": "commercial_informational",
            "preferred_slug": "basalt-stones-for-gas-grill",
        },
    )
    assert response.status_code == 200
    drafts = client.get("/content/articles/drafts").json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["topic_title"] == "אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז"


def test_generate_topic_draft_sets_basalt_featured_image_prompt(client: TestClient) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז",
            "focus_keyword": "אבני בזלת לגריל",
            "target_intent": "commercial_informational",
            "preferred_slug": "basalt-stones-for-gas-grill",
        },
    )
    draft_id = response.json()["draft"]["draft_id"]
    draft = client.get(f"/content/articles/{draft_id}").json()["draft"]
    assert "black basalt lava stones" in draft["featured_image_prompt"]


@pytest.mark.parametrize(
    ("topic_title", "focus_keyword", "slug", "must_have", "forbidden", "image_term"),
    [
        ("פיקניה על הגריל – מדריך מלא", "פיקניה", "picanha-on-grill", ["שכבת שומן", "54–56°C", "חיתוך נגד הסיבים"], ["74°C", "עוף"], "beef"),
        ("איך להכין כנפיים קריספיות על הגריל", "כנפיים קריספיות", "crispy-grilled-wings", ["ייבוש", "74°C", "גלייז"], ["פיקניה", "מדיום רייר"], "wings"),
        ("אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז", "אבני בזלת לגריל", "basalt-stones-for-gas-grill", ["פיזור חום", "התלקחויות", "יציבות חום"], ["74°C", "מתכון עוף"], "basalt"),
    ],
)
def test_topic_specific_generation_regression(
    client: TestClient,
    topic_title: str,
    focus_keyword: str,
    slug: str,
    must_have: list[str],
    forbidden: list[str],
    image_term: str,
) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={"topic_title": topic_title, "focus_keyword": focus_keyword, "target_intent": "commercial_informational", "preferred_slug": slug},
    )
    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["slug"] == slug
    full = client.get(f"/content/articles/{draft['draft_id']}").json()["draft"]
    for term in must_have:
        assert term in full["article_body"]
    for term in forbidden:
        assert term not in full["article_body"]
    assert image_term in full["featured_image_prompt"].lower()
    assert draft["quality"]["article_quality_score"] >= 85
    assert draft["quality"]["publish_readiness"] == "READY_FOR_REVIEW"
    assert draft["debug"]["generator_source"] == "contract_engine"




def _generate_topic(client: TestClient, topic_title: str, focus_keyword: str, target_intent: str, slug: str) -> dict:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={"topic_title": topic_title, "focus_keyword": focus_keyword, "target_intent": target_intent, "preferred_slug": slug},
    )
    assert response.status_code == 200
    draft = response.json()["draft"]
    return client.get(f"/content/articles/{draft['draft_id']}").json()["draft"]


def test_charcoal_comparison_body_matches_title_intent_and_contract(client: TestClient) -> None:
    draft = _generate_topic(client, "פחם / פחם קוקוס", "פחם / פחם קוקוס", "comparison", "coconut-charcoal-vs-wood-charcoal")
    for term in ["פחם קוקוס", "פחם עץ", "זמן בעירה", "יציבות חום", "עשן", "אפר"]:
        assert term in draft["article_body"]
    for term in ["74°C", "גלייז", "מנוחה של סטייק"]:
        assert term not in draft["article_body"]
    assert draft["debug"]["detected_topic_type"] == "fuel_comparison_or_guide"
    assert draft["debug"]["generator_source"] == "contract_engine"
    assert draft["debug"]["search_intent"] == "comparison"
    assert draft["debug"]["validation_passed"] is True
    assert draft["quality"]["publish_readiness"] == "READY_FOR_REVIEW"


def test_picanha_body_does_not_leak_poultry_or_fuel_terms(client: TestClient) -> None:
    draft = _generate_topic(client, "פיקניה", "פיקניה", "how-to", "picanha-on-grill")
    for term in ["שכבת שומן", "מלח גס", "54–56°C", "חיתוך נגד הסיבים"]:
        assert term in draft["article_body"]
    for term in ["74°C", "פחם קוקוס", "גלייז כנפיים"]:
        assert term not in draft["article_body"]
    assert draft["debug"]["detected_topic_type"] == "meat_quick_grill_cut"
    assert draft["debug"]["validation_passed"] is True


def test_basalt_accessory_body_does_not_leak_meat_recipe_terms(client: TestClient) -> None:
    draft = _generate_topic(client, "אבני בזלת לגריל", "אבני בזלת לגריל", "commercial_informational", "basalt-stones-for-gas-grill")
    for term in ["פיזור חום", "גריל גז", "התלקחויות", "ניקוי והחלפה"]:
        assert term in draft["article_body"]
    for term in ["טמפ' פנימית של בשר", "גלייז", "מדיום רייר"]:
        assert term not in draft["article_body"]
    assert draft["debug"]["detected_topic_type"] == "grill_accessory_guide"
    assert draft["debug"]["validation_passed"] is True


def test_generate_article_defaults_and_image_plan(client: TestClient) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    assert draft['target_site_section'] == 'blog'
    assert draft['target_url'].startswith('https://compassgrill.co.il/blog/')
    assert draft['publish_destination_status'] == 'ready'
    assert _is_hebrew(draft['image_alt_text'])
    assert draft['image_filename_slug'].replace('-', '').isalnum()
    assert draft['image_publish_status'] == 'NOT_PUBLISHED'
    assert '<h1' not in draft['article_body'].lower()
    assert draft['article_body'].lower().count('<h2') >= 5
    assert draft['article_body'].lower().count('<h3') >= 3
    assert draft['slug'] != 'compass-grill-article'
    assert '[' not in draft['article_body']
    image_plan = client.post(f"/content/articles/{draft['id']}/generate-image-plan")
    assert image_plan.status_code == 200
    assert image_plan.json()['message_he'] == 'תכנון התמונה עודכן בהצלחה'
    prompt_blob = (draft['featured_image_prompt'] + ' ' + ' '.join(i.get('prompt', '') for i in draft.get('section_image_prompts', []))).lower()
    assert any(word in prompt_blob for word in draft['slug'].split('-')[:2])


def test_generate_article_image_uses_provider(client: TestClient) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    response = client.post(f"/content/articles/{draft['id']}/generate-image")
    assert response.status_code == 200
    payload = response.json()
    assert payload['image_generation_enabled'] is False
    assert payload['draft']['featured_image_status'] == 'planned'
    assert payload['image_status'] == 'planned'
    assert payload['status'] == 'planned'
    assert payload['generated_image_url'] is None
    assert payload['featured_image_url'] is None
    assert payload['open_image_url'] is None


def test_topic_switch_replaces_active_article(client: TestClient) -> None:
    picanha = client.post(
        "/content/articles/generate-topic-draft",
        json={"topic_title": "פיקניה על הגריל – מדריך מלא", "focus_keyword": "פיקניה", "target_intent": "how-to", "preferred_slug": "picanha-on-grill"},
    ).json()
    basalt = client.post(
        "/content/articles/generate-topic-draft",
        json={"topic_title": "אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז", "focus_keyword": "אבני בזלת לגריל", "target_intent": "commercial_informational", "preferred_slug": "basalt-stones-for-gas-grill"},
    ).json()
    assert picanha["draft"]["draft_id"] != basalt["draft"]["draft_id"]
    drafts = client.get("/content/articles/drafts").json()["drafts"]
    active = next(d for d in drafts if d["is_active_manual_article"])
    old = next(d for d in drafts if d["id"] == picanha["draft"]["draft_id"])
    assert active["id"] == basalt["draft"]["draft_id"]
    assert old["is_active_manual_article"] is False
    page = client.get("/seo/simple-workspace").text
    assert basalt["draft"]["title"] in page
    assert picanha["draft"]["title"] in page
    assert basalt["diagnostics"]["selected_generator"] == "contract_grill_accessory_guide"


def test_latest_debug_endpoint(client: TestClient) -> None:
    client.post("/content/articles/generate-random-daily-draft")
    response = client.get("/seo/content-articles/latest-debug")
    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_article_id"] == payload["active_article_id"]
    assert payload["generator_version"] == "v4-production-quality-links-expansions-2026-06-03"
    assert payload["selected_generator"]


def test_publish_blocks_and_dry_run(client: TestClient) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    blocked = client.post(f"/content/articles/{draft['id']}/publish")
    assert blocked.status_code == 400
    dry = client.post(f"/content/articles/{draft['id']}/publish?dry_run=true")
    assert dry.status_code == 200
    payload = dry.json()
    assert payload['dry_run'] is True
    assert payload['target_url'].startswith('https://compassgrill.co.il/blog/')
    assert payload['allowed'] is False


def test_approved_article_manual_publish_flow(client: TestClient) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    client.post(f"/content/articles/{draft['id']}/approve")
    publish = client.post(f"/content/articles/{draft['id']}/publish")
    assert publish.status_code in (200, 400)


def test_generate_article_handles_missing_title_field_safely(client: TestClient, db_session: Session) -> None:
    db_session.add_all(
        [
            IStoreProduct(istore_product_id="sku-no-url", product_name=""),
            IStoreProduct(
                istore_product_id="sku-valid",
                product_name="גריל מומלץ",
                product_url="https://compassgrill.co.il/products/recommended-grill",
            ),
        ]
    )
    db_session.commit()

    response = client.post('/content/articles/generate-daily-draft')
    assert response.status_code == 200
    draft = response.json()['draft']
    links = draft['internal_links']
    assert any(link['title'] == 'גריל מומלץ' for link in links)
    assert all(link['url'] for link in links)


def test_blog_publish_adapter_uses_istore_x_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.settings.istore_base_url", "https://api.istore.local")
    monkeypatch.setattr("app.api.routes.settings.istore_company_id", "company-1")
    monkeypatch.setattr("app.api.routes.settings.istore_x_token", "token-x")
    monkeypatch.setattr("app.api.routes.settings.istore_api_token", None)

    assert _blog_publish_adapter_ready() is True


def test_dry_run_returns_safe_block_when_blog_publish_not_configured(client: TestClient) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    client.post(f"/content/articles/{draft['id']}/approve")

    response = client.post(f"/content/articles/{draft['id']}/publish?dry_run=true")
    assert response.status_code == 200
    payload = response.json()
    assert payload['allowed'] is False
    assert payload['blocked_reason'] == 'פרסום לבלוג עדיין לא מוגדר במערכת'



def test_publish_updates_status_only_after_live_verification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    client.post(f"/content/articles/{draft['id']}/approve")

    monkeypatch.setattr('app.api.routes._blog_publish_adapter_ready', lambda: True)

    class _Publisher:
        def publish(self, _draft):
            return {
                "external_content_id": "123",
                "live_url": "https://compassgrill.co.il/blog/slug1",
                "verification": {"status_code": 200, "title_found": True, "meta_title_found": True},
            }

    monkeypatch.setattr('app.api.routes.IStoreBlogPublisher.from_settings', lambda: _Publisher())

    response = client.post(f"/content/articles/{draft['id']}/publish")
    assert response.status_code == 200
    payload = response.json()
    assert payload['verification_status'] == 'VERIFIED'
    assert payload['publish_status'] == 'PUBLISHED'


def test_low_semantic_score_cannot_approve(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    monkeypatch.setattr(
        "app.api.routes._article_quality_summary",
        lambda _draft: {
            "seo_quality_score": 90.0,
            "semantic_relevance_score": 20.0,
            "suggested_link_relevance": 80.0,
            "article_quality_score": 80.0,
            "publish_readiness": "READY_FOR_REVIEW",
        },
    )
    response = client.post(f"/content/articles/{draft['id']}/approve")
    assert response.status_code == 400
    assert response.json()['detail'] == 'אי אפשר לאשר מאמר כי איכות/רלוונטיות נמוכה מדי'


def test_low_article_quality_cannot_approve(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    def _mock_summary(_draft):
        return {
            "seo_quality_score": 90.0,
            "semantic_relevance_score": 90.0,
            "suggested_link_relevance": 90.0,
            "article_quality_score": 74.9,
            "publish_readiness": "NEEDS_IMPROVEMENT",
        }

    monkeypatch.setattr("app.api.routes._article_quality_summary", _mock_summary)
    response = client.post(f"/content/articles/{draft['id']}/approve")
    assert response.status_code == 400
    assert response.json()['detail'] == 'אי אפשר לאשר מאמר כי איכות/רלוונטיות נמוכה מדי'


def test_needs_improvement_cannot_publish_and_publish_rechecks_gates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    monkeypatch.setattr('app.api.routes._blog_publish_adapter_ready', lambda: True)
    monkeypatch.setattr(
        "app.api.routes._content_quality_gate_passed",
        lambda _draft: False,
    )
    response = client.post(f"/content/articles/{draft['id']}/publish")
    assert response.status_code == 400
    assert response.json()['detail'] == 'אי אפשר לאשר מאמר כי איכות/רלוונטיות נמוכה מדי'


def test_simple_workspace_shows_blocked_warning_and_regen_button(client: TestClient) -> None:
    client.post('/content/articles/generate-daily-draft')
    import app.api.routes as routes

    original = routes._article_quality_summary
    routes._article_quality_summary = lambda draft: {  # type: ignore[assignment]
        **original(draft),
        "semantic_relevance_score": 20.0,
        "suggested_link_relevance": 20.0,
        "article_quality_score": 41.0,
        "publish_readiness": "NEEDS_IMPROVEMENT",
    }
    response = client.get('/seo/simple-workspace')
    routes._article_quality_summary = original
    assert response.status_code == 200
    assert 'נדרש שיפור לפני אישור' in response.text
    assert 'צור מאמר מחדש' in response.text




def test_manual_upload_readiness_text_mapping(client: TestClient) -> None:
    client.post('/content/articles/generate-daily-draft')
    import app.api.routes as routes

    original = routes._article_quality_summary
    try:
        routes._article_quality_summary = lambda draft: {  # type: ignore[assignment]
            **original(draft),
            "semantic_relevance_score": 90.0,
            "suggested_link_relevance": 90.0,
            "article_quality_score": 90.0,
            "publish_readiness": "READY_FOR_REVIEW",
        }
        response = client.get('/seo/simple-workspace')
        assert response.status_code == 200
        assert 'מוכן לבדיקה' in response.text
        assert 'נדרש שיפור לפני אישור' not in response.text

        routes._article_quality_summary = lambda draft: {  # type: ignore[assignment]
            **original(draft),
            "semantic_relevance_score": 90.0,
            "suggested_link_relevance": 90.0,
            "article_quality_score": 90.0,
            "publish_readiness": "APPROVED",
        }
        approved_response = client.get('/seo/simple-workspace')
        assert approved_response.status_code == 200
        assert 'אושר ומוכן לפרסום' in approved_response.text

        routes._article_quality_summary = lambda draft: {  # type: ignore[assignment]
            **original(draft),
            "semantic_relevance_score": 20.0,
            "suggested_link_relevance": 20.0,
            "article_quality_score": 40.0,
            "publish_readiness": "NEEDS_IMPROVEMENT",
        }
        needs_improvement_response = client.get('/seo/simple-workspace')
        assert needs_improvement_response.status_code == 200
        assert 'נדרש שיפור לפני אישור' in needs_improvement_response.text
    finally:
        routes._article_quality_summary = original
def test_hebrew_tabun_slug_is_not_gas_grill_vs_charcoal(client: TestClient) -> None:
    for _ in range(8):
        draft = client.post('/content/articles/generate-daily-draft').json()['draft']
        if draft['title'] == 'טאבון גז או טאבון עצים':
            assert draft['slug'] != 'gas-grill-vs-charcoal'
            assert draft['slug'] in {'gas-oven-vs-wood-oven', 'tabun-gas-vs-wood', 'tabun-gas-vs-tabun-wood'}
            return
    pytest.fail('Did not generate the expected tabun topic in 8 attempts')


def test_publish_404_verification_keeps_draft_not_published(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    client.post(f"/content/articles/{draft['id']}/approve")

    monkeypatch.setattr('app.api.routes._blog_publish_adapter_ready', lambda: True)

    from app.services.istore_blog_publisher import IStoreBlogPublishError

    class _Publisher:
        def publish(self, _draft):
            raise IStoreBlogPublishError('Live URL verification failed with HTTP 404')

    monkeypatch.setattr('app.api.routes.IStoreBlogPublisher.from_settings', lambda: _Publisher())

    response = client.post(f"/content/articles/{draft['id']}/publish")
    assert response.status_code == 400

    check = client.get(f"/content/articles/{draft['id']}").json()['draft']
    assert check['status'] == 'APPROVED'
    assert check['verification_status'] == 'NOT_VERIFIED'


def test_minimal_payload_publish_returns_success_without_marking_published(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    client.post(f"/content/articles/{draft['id']}/approve")

    monkeypatch.setattr('app.api.routes._blog_publish_adapter_ready', lambda: True)

    class _Publisher:
        def publish(self, _draft):
            return {
                "external_content_id": "555",
                "minimal_payload_test": True,
            }

    monkeypatch.setattr('app.api.routes.IStoreBlogPublisher.from_settings', lambda: _Publisher())

    response = client.post(f"/content/articles/{draft['id']}/publish")
    assert response.status_code == 200
    payload = response.json()
    assert payload['published'] is False
    assert payload['external_content_id'] == '555'
    assert payload['result_he'] == 'ISTORE minimal create test succeeded; full article payload still needs investigation.'

    check = client.get(f"/content/articles/{draft['id']}").json()['draft']
    assert check['status'] == 'APPROVED'

def test_regression_wood_chips_topic_quality_and_prompts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.content_articles._select_topic",
        lambda _db: ("איך לבחור שבבי עץ לעישון בשר", "שבבי עץ לעישון", "informational"),
    )
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    body = draft['article_body'].lower()
    assert '<h1' not in body
    assert draft['slug'] != 'compass-grill-article'
    assert draft['slug'] == 'wood-chips-for-smoking-meat'
    assert all(term in body for term in ['hickory', 'oak', 'apple'])
    assert 'thin blue smoke' in body
    prompt_blob = (draft['featured_image_prompt'] + ' ' + ' '.join(i.get('prompt', '') for i in draft.get('section_image_prompts', []))).lower()
    assert any(t in prompt_blob for t in ['smoker box', 'wood chips'])

    assert float(draft["quality"]["article_quality_score"]) > 75

    details = client.get(f"/content/articles/{draft['id']}").json()['draft']
    assert details['debug']['generator_version'] == 'v4-production-quality-links-expansions-2026-06-03'
    assert details['debug']['h1_removed'] is True
    assert details['debug']['slug_source'] in {'title', 'focus_keyword', 'topic_mapping', 'hard_fallback'}


def test_generate_image_returns_clear_error_when_provider_has_no_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _BadProvider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(
                enabled=True,
                provider='broken-provider',
                status='generated',
                image_url=None,
                message_he=f'generated for {draft_slug}',
            )

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _BadProvider())
    response = client.post(f"/content/articles/{draft['id']}/generate-image")
    assert response.status_code == 502
    payload = response.json()
    assert payload['success'] is False
    assert payload['error'] == 'Image provider returned no URL'
    assert payload['diagnostics']['provider_name'] == 'broken-provider'
    assert payload['diagnostics']['provider_response_received'] is True
    assert payload['diagnostics']['raw_provider_url_present'] is False


def test_generate_image_persists_urls_and_metadata_and_response(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _Provider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(
                enabled=True,
                provider='openai',
                status='generated',
                image_url=f'https://cdn.example.com/{draft_slug}.jpg',
                width=1280,
                height=720,
                generated_at='2026-05-25T00:00:00+00:00',
                message_he='ok',
            )

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _Provider())
    response = client.post(f"/content/articles/{draft['id']}/generate-image")
    assert response.status_code == 200
    payload = response.json()
    assert payload['generated_image_url'].startswith('https://')
    assert payload['featured_image_url'] == payload['generated_image_url']
    assert payload['open_image_url'] == payload['generated_image_url']
    assert payload['download_image_url'] == payload['generated_image_url']
    assert payload['copy_image_url'] == payload['generated_image_url']
    assert payload['image_metadata']['width'] == 1280
    assert payload['image_metadata']['height'] == 720
    assert payload['diagnostics']['provider_response_received'] is True
    assert payload['diagnostics']['image_url_present'] is True
    assert payload['diagnostics']['image_storage_success'] is True
    assert payload['diagnostics']['provider_name'] == 'openai'

    check = client.get(f"/content/articles/{draft['id']}").json()['draft']
    assert check['generated_image_url'] == payload['generated_image_url']
    assert check['featured_image_url'] == payload['generated_image_url']
    assert check['image_generation_metadata']['provider'] == 'openai'


def test_manual_upload_view_displays_generated_image_controls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _Provider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(
                enabled=True,
                provider='openai',
                status='generated',
                image_url=f'https://cdn.example.com/{draft_slug}.jpg',
                width=1280,
                height=720,
                generated_at='2026-05-25T00:00:00+00:00',
                message_he='ok',
            )

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _Provider())
    client.post(f"/content/articles/{draft['id']}/generate-image")

    response = client.get('/seo/simple-workspace')
    assert response.status_code == 200
    assert 'manual-upload-view · הכנה מהירה לעובד חנות' in response.text


def test_generate_image_success_must_include_generated_image_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _Provider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(
                enabled=True,
                provider='openai',
                status='generated',
                image_url=f'https://cdn.example.com/{draft_slug}.jpg',
                message_he='ok',
            )

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _Provider())
    response = client.post(f"/content/articles/{draft['id']}/generate-image")
    assert response.status_code == 200
    payload = response.json()
    assert payload['success'] is True
    assert payload['generated_image_url'] is not None


def test_generate_image_response_includes_required_diagnostics(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _Provider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(
                enabled=True,
                provider='openai',
                status='generated',
                image_url=f'https://cdn.example.com/{draft_slug}.jpg',
                width=1280,
                height=720,
                generated_at='2026-05-25T00:00:00+00:00',
                message_he='ok',
            )

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _Provider())
    payload = client.post(f"/content/articles/{draft['id']}/generate-image").json()
    diagnostics = payload['diagnostics']
    for key in ['provider_name','provider_response_received','raw_provider_url_present','generated_image_url','featured_image_url','image_url_present','image_storage_success','image_file_saved','image_public_url','image_file_path','image_generation_metadata']:
        assert key in diagnostics


def test_stub_provider_saves_local_image_and_returns_static_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    class _Resp:
        class D:
            b64_json = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4KDa8PAAAAAElFTkSuQmCC"
        data = [D()]

    class _Images:
        @staticmethod
        def generate(**_kwargs):
            return _Resp()

    class _Client:
        images = _Images()

    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    monkeypatch.setattr(settings, 'image_provider', 'openai')
    monkeypatch.setattr(settings, 'openai_api_key', 'test-key')
    monkeypatch.setattr('app.services.image_generation.OpenAI', lambda api_key: _Client())

    response = client.post(f"/content/articles/{draft['id']}/generate-image")
    assert response.status_code == 200
    payload = response.json()

    image_url = payload['generated_image_url']
    assert image_url.startswith('https://compass-seo-ai-1.onrender.com/static/generated-images/')
    assert 'images.example.com' not in image_url

    file_check = client.get(image_url)
    assert file_check.status_code == 200
    assert file_check.headers['content-type'].startswith('image/')
    assert len(file_check.content) > 0

    assert payload['open_image_url'] == image_url
    assert payload['download_image_url'] == image_url
    assert payload['copy_image_url'] == image_url
    assert payload['diagnostics']['image_file_saved'] is True
    assert payload['diagnostics']['image_public_url'] == image_url
    assert payload['diagnostics']['image_file_path'] == "app/static/generated-images/wood-chips-for-smoking-meat-hero.png"


def test_generate_image_creates_separate_hero_and_banner_assets_with_absolute_urls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _Provider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(enabled=True, provider='openai', status='generated', image_url=f'/static/generated-images/{draft_slug}.png')

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _Provider())
    payload = client.post(f"/content/articles/{draft['id']}/generate-image").json()
    assert payload["article_hero_image"]["public_url"].startswith("https://")
    assert payload["general_banner_image"]["public_url"].startswith("https://")
    assert payload["article_hero_image"]["prompt"] != payload["general_banner_image"]["prompt"]
    assert "wide" in payload["general_banner_image"]["prompt"].lower()
    assert "empty space" in payload["general_banner_image"]["prompt"].lower()


def test_internal_links_injected_into_html_body(client: TestClient, db_session: Session) -> None:
    db_session.add(IStoreProduct(istore_product_id="sku-b", product_name="אבני בזלת לגריל", product_url="https://compassgrill.co.il/product/basalt"))
    db_session.add(IStoreProduct(istore_product_id="sku-c", product_name="מדחום לבשר", product_url="https://compassgrill.co.il/product/thermometer"))
    db_session.commit()
    response = client.post("/content/articles/generate-topic-draft", json={"topic_title": "אבני בזלת לגריל", "focus_keyword": "אבני בזלת לגריל", "target_intent": "commercial"})
    draft_id = response.json()["draft"]["draft_id"]
    full = client.get(f"/content/articles/{draft_id}").json()["draft"]
    assert "<a href='https://compassgrill.co.il/product/" in full["article_body"]


def test_generated_images_output_contains_img_and_removes_marker(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']

    class _Provider:
        def generate_hero_image(self, _prompt: str, *, draft_slug: str):
            from app.services.image_generation import ImageGenerationResult
            return ImageGenerationResult(
                enabled=True,
                provider='openai',
                status='generated',
                image_url=f'https://cdn.example.com/{draft_slug}.jpg',
                message_he='ok',
            )

    monkeypatch.setattr('app.api.routes.get_image_provider', lambda: _Provider())
    image_payload = client.post(f"/content/articles/{draft['id']}/generate-image").json()
    generated_image_url = image_payload['generated_image_url']

    generated_output = (
        f"{draft['article_body']}\n[IMAGE_1_HERE]\n"
        .replace('[IMAGE_1_HERE]', f'<img src="{generated_image_url}" alt="{draft["image_alt_text"]}" />')
    )

    assert generated_image_url in generated_output
    assert '[IMAGE_1_HERE]' not in generated_output
    assert '[IMAGE_2_HERE]' not in generated_output
    assert f'alt="{draft["image_alt_text"]}"' in generated_output
    assert 'ALT:' not in generated_output
    assert '<img' in generated_output

def test_generate_random_daily_endpoint_response(client: TestClient) -> None:
    response = client.post('/content/articles/generate-random-daily-draft')
    assert response.status_code == 200
    payload = response.json()
    assert payload['success'] is True
    assert payload['selected_topic']
    assert isinstance(payload['reused'], bool)
    assert payload['draft_id'] > 0
    assert payload['slug']


def test_wings_topic_regression(client: TestClient) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "כנפיים קריספיות על הגריל",
            "focus_keyword": "כנפיים קריספיות על הגריל",
            "target_intent": "how-to",
        },
    )
    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["slug"] == "crispy-grilled-wings"
    full = client.get(f"/content/articles/{draft['draft_id']}").json()["draft"]
    body = full["article_body"]
    assert "כנפיים" in body and "קריספי" in body and "74°C" in body and "גלייז" in body
    prompt = full["featured_image_prompt"].lower()
    assert "wings" in prompt and "chicken" in prompt and "grill" in prompt
    assert "wood chips" not in prompt
    assert all((p.get("relevance_score") or 0) >= 40 for p in (draft.get("suggested_related_products") or []))
    quality = draft["quality"]
    assert float(quality["article_quality_score"]) > 75
    assert quality["publish_readiness"] == "READY_FOR_REVIEW"


def test_employee_view_shows_copy_before_advanced_and_single_main_copy_box(client: TestClient) -> None:
    draft = client.post('/content/articles/generate-daily-draft').json()['draft']
    page = client.get('/seo/simple-workspace').text
    assert "העתקה לאתר לפי החלונות ב־ISTORE" in page
    assert page.index("העתקה לאתר לפי החלונות ב־ISTORE") < page.index("<summary>מתקדם</summary>")
    assert "<details><summary>מתקדם</summary>" in page
    assert page.count('<textarea id="final-content-box-') == 1


def test_topic_reuse_after_pool_exhaustion(client: TestClient) -> None:
    reused_flags = []
    for _ in range(14):
        reused_flags.append(client.post('/content/articles/generate-random-daily-draft').json()['reused'])
    assert any(flag is False for flag in reused_flags)
    assert reused_flags[-1] is True

def test_basalt_topic_matches_products_and_includes_links(client: TestClient, db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="sku-basalt",
            product_name="אבני בזלת לגריל גז",
            slug="basalt-lava-stones",
            product_url="https://compassgrill.co.il/product/basalt-lava-stones",
        )
    )
    db_session.commit()

    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "אבני בזלת לגריל – איך הן משפרות צלייה בגריל גז",
            "focus_keyword": "אבני בזלת לגריל",
            "target_intent": "commercial_informational",
        },
    )
    assert response.status_code == 200
    payload = response.json()["draft"]
    assert payload["debug"]["matched_product_count"] >= 1
    assert payload["debug"]["best_match_url"]
    draft = client.get(f"/content/articles/{payload['draft_id']}").json()["draft"]
    assert draft["internal_links"]
    assert draft["suggested_related_products"]
    assert "<h2>מוצרים רלוונטיים באתר</h2>" in draft["article_body"]


def test_basalt_synonyms_match_lava_terms(client: TestClient, db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="sku-lava",
            product_name="Lava Rocks for Grill",
            slug="lava-rocks-grill",
            product_url="https://compassgrill.co.il/product/lava-rocks-grill",
        )
    )
    db_session.commit()

    response = client.get("/debug/internal-link-match", params={"query": "אבן לבה לגריל"})
    assert response.status_code == 200
    payload = response.json()
    assert any("lava" in item["url"] for item in payload["matches"])
    assert "lava rocks" in [t.lower() for t in payload["debug"]["searched_terms"]]


def test_manual_upload_view_hides_empty_link_message_when_match_exists(client: TestClient, db_session: Session) -> None:
    db_session.add(
        IStoreProduct(
            istore_product_id="sku-basalt-2",
            product_name="אבני לבה לגריל",
            product_url="https://compassgrill.co.il/product/lava-stone",
        )
    )
    db_session.commit()

    client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "אבני בזלת לגריל",
            "focus_keyword": "אבני בזלת לגריל",
            "target_intent": "commercial",
        },
    )
    html = client.get('/seo/simple-workspace').text
    assert 'כרגע אין קישורים פנימיים רלוונטיים להצגה' not in html
    assert 'Copy product URL' in html

from app.services import content_articles as content_articles_service


def test_internal_link_matching_from_mocked_sitemaps(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """
    <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <url><loc>https://compassgrill.co.il/products/picanha-steak</loc><lastmod>2026-05-01</lastmod></url>
      <url><loc>https://compassgrill.co.il/products/basalt-lava-stones</loc><lastmod>2026-05-01</lastmod></url>
      <url><loc>https://compassgrill.co.il/categories/wood-chips-smoking</loc><lastmod>2026-05-01</lastmod></url>
      <url><loc>https://compassgrill.co.il/products/meat-thermometer</loc><lastmod>2026-05-01</lastmod></url>
      <url><loc>https://compassgrill.co.il/products/unrelated-fish-knife</loc><lastmod>2026-05-01</lastmod></url>
    </urlset>
    """

    class _Resp:
        text = xml

    monkeypatch.setattr(content_articles_service.requests, "get", lambda *a, **k: _Resp())
    content_articles_service.refresh_internal_link_index()

    picanha = client.post('/content/articles/generate-topic-draft', json={"topic_title": "פיקניה", "focus_keyword": "פיקניה", "target_intent": "commercial"}).json()["draft"]
    full_picanha = client.get(f"/content/articles/{picanha['draft_id']}").json()["draft"]
    assert any("picanha" in (l["url"]).lower() for l in full_picanha["internal_links"])

    basalt = client.post('/content/articles/generate-topic-draft', json={"topic_title": "אבני בזלת לגריל", "focus_keyword": "אבני בזלת", "target_intent": "commercial"}).json()["draft"]
    full_basalt = client.get(f"/content/articles/{basalt['draft_id']}").json()["draft"]
    assert any("basalt" in (l["url"]).lower() or "lava" in (l["url"]).lower() for l in full_basalt["internal_links"])

    chips = client.post('/content/articles/generate-topic-draft', json={"topic_title": "שבבי עץ לעישון", "focus_keyword": "שבבי עץ", "target_intent": "commercial"}).json()["draft"]
    full_chips = client.get(f"/content/articles/{chips['draft_id']}").json()["draft"]
    assert any("wood-chips" in (l["url"]).lower() or "smoking" in (l["url"]).lower() for l in full_chips["internal_links"])

    thermo = client.post('/content/articles/generate-topic-draft', json={"topic_title": "מדחום לבשר", "focus_keyword": "מדחום לבשר", "target_intent": "commercial"}).json()["draft"]
    full_thermo = client.get(f"/content/articles/{thermo['draft_id']}").json()["draft"]
    assert any("thermometer" in (l["url"]).lower() for l in full_thermo["internal_links"])
    assert not any("unrelated-fish-knife" in (l["url"]).lower() for l in full_thermo["internal_links"])


def test_refresh_internal_links_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        text = "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>"

    monkeypatch.setattr(content_articles_service.requests, "get", lambda *a, **k: _Resp())
    response = client.post('/content/articles/internal-links/refresh-index')
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "sitemap_loaded_count" in payload


def test_charcoal_specialized_output_reaches_response_workspace_and_publish_payload(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.post(
        "/content/articles/generate-topic-draft",
        json={
            "topic_title": "ההבדל בין פחם קוקוס לפחם עץ",
            "focus_keyword": "פחם / פחם קוקוס",
            "target_intent": "comparison",
            "preferred_slug": "coconut-charcoal-vs-wood-charcoal",
        },
    )

    assert response.status_code == 200
    generated = response.json()
    draft = generated["draft"]
    specialized_terms = ["טבלת השוואה", "זמן בעירה", "יציבות חום", "פחם קוקוס"]
    generic_terms = ["טמפרטורה הפנימית של הנתח", "74°C לעוף", "גלייז נשרף"]

    assert draft["debug"]["selected_generator"] == "contract_fuel_comparison_or_guide"
    assert draft["debug"]["detected_topic_type"] == "fuel_comparison_or_guide"
    for term in specialized_terms:
        assert term in draft["article_body"]
    for term in generic_terms:
        assert term not in draft["article_body"]

    full = client.get(f"/content/articles/{draft['draft_id']}").json()["draft"]
    for term in specialized_terms:
        assert term in full["article_body"]
    for term in generic_terms:
        assert term not in full["article_body"]

    workspace = client.get("/seo/simple-workspace").text
    assert "העתקה לאתר לפי החלונות ב־ISTORE" in workspace
    assert "תצוגה מקדימה" in workspace
    for term in specialized_terms:
        assert term in workspace
    for term in generic_terms:
        assert term not in workspace

    monkeypatch.setattr("app.services.istore_blog_publisher.settings.istore_create_minimal_payload", False)
    publisher = IStoreBlogPublisher(
        base_url="https://admin.example.com",
        admin_cookie="session=fake",
        xsrf_token="fake-xsrf",
        language_id=2,
        blog_is_blog=1,
    )
    dry_run = publisher.publish(db_session.get(ContentArticleDraft, draft["draft_id"]), dry_run=True)
    description = dry_run["payload"]["descriptions"]["2"]["description"]
    for term in specialized_terms:
        assert term in description
    for term in generic_terms:
        assert term not in description


def _word_count_from_html(html: str) -> int:
    import re

    return len(re.findall(r"[\w\u0590-\u05FF]+", re.sub(r"<[^>]+>", " ", html or "")))


def test_thermometer_seo_keywords_meta_and_depth(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.content_articles._load_sitemap_index", lambda force_refresh=False: ([], {"sitemap_loaded_count": 0, "products_loaded_count": 0, "categories_loaded_count": 0}))

    draft = generate_topic_article_draft(
        db_session,
        topic_title="איך לבחור מדחום לבשר",
        focus_keyword="מדחום לבשר",
        target_intent="commercial_informational",
    )
    profile = _classify_topic(draft.topic_title, draft.focus_keyword, draft.target_intent)
    metadata = build_topic_seo_metadata(draft.focus_keyword, draft.title, profile)

    assert len(metadata["seo_keywords"]) >= 8
    for phrase in ["מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז"]:
        assert phrase in metadata["seo_keywords"]
    assert not any(term in draft.meta_title for term in INTERNAL_SEO_CONTRACT_TERMS)
    assert "מדחום לבשר" in draft.meta_description
    assert "זמן תגובה" in draft.meta_description or "כיול" in draft.meta_description
    assert _word_count_from_html(draft.article_body) >= 700


def test_basalt_seo_metadata_and_category_fallback(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.content_articles as articles

    category_url = "https://compassgrill.co.il/categories/grill-accessories"
    entries = [
        {
            "url": category_url,
            "slug": "grill-accessories-basalt-lava-stones",
            "title": "אביזרים לגריל גז אבני לבה",
            "type": "category",
            "tokens": articles._tokenize_hebrew("אביזרים לגריל גז אבני לבה בזלת פיזור חום"),
        }
    ]
    monkeypatch.setattr("app.services.content_articles._load_sitemap_index", lambda force_refresh=False: (entries, {"sitemap_loaded_count": 1, "products_loaded_count": 0, "categories_loaded_count": 1}))

    draft = articles.generate_topic_article_draft(
        db_session,
        topic_title="אבני בזלת לגריל",
        focus_keyword="אבני בזלת לגריל",
        target_intent="commercial_informational",
    )
    profile = _classify_topic(draft.topic_title, draft.focus_keyword, draft.target_intent)
    metadata = build_topic_seo_metadata(draft.focus_keyword, draft.title, profile)
    links = __import__("json").loads(draft.suggested_related_products_json)

    assert len(metadata["seo_keywords"]) >= 8
    for phrase in ["אבני לבה לגריל", "פיזור חום בגריל גז", "ניקוי אבני בזלת"]:
        assert phrase in metadata["seo_keywords"]
    assert "how_to" not in draft.meta_title
    assert "accessory" not in draft.meta_title
    assert "פיזור חום" in draft.meta_description
    assert "התלקחויות" in draft.meta_description
    assert any(link["url"] == category_url and link.get("type") == "category" for link in links)


def test_picanha_seo_keywords_and_natural_title() -> None:
    profile = _classify_topic("פיקניה", "פיקניה", "how-to")
    metadata = build_topic_seo_metadata("פיקניה", "פיקניה", profile)

    assert len(metadata["seo_keywords"]) >= 8
    for phrase in ["פיקניה על הגריל", "טמפרטורת פיקניה", "Reverse Sear פיקניה"]:
        assert phrase in metadata["seo_keywords"]
    assert metadata["meta_title"].startswith("איך לצלות פיקניה")
    assert not any(term in metadata["meta_title"] for term in INTERNAL_SEO_CONTRACT_TERMS)


def test_internal_links_are_selected_injected_and_unrelated_excluded(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.content_articles as articles

    db_session.add_all(
        [
            IStoreProduct(istore_product_id="thermo", product_name="מדחום דיגיטלי לבשר", slug="digital-meat-thermometer", product_url="https://compassgrill.co.il/products/meat-thermometer", category="אביזרים לגריל"),
            IStoreProduct(istore_product_id="basalt", product_name="אבני בזלת לגריל גז", slug="basalt-lava-stones", product_url="https://compassgrill.co.il/products/basalt-stones", category="אביזרים לגריל"),
            IStoreProduct(istore_product_id="picanha", product_name="פיקניה מובחרת", slug="picanha-steak", product_url="https://compassgrill.co.il/products/picanha", category="בשר לגריל"),
            IStoreProduct(istore_product_id="sofa", product_name="ספה לגינה", slug="garden-sofa", product_url="https://compassgrill.co.il/products/garden-sofa", category="ריהוט גן"),
        ]
    )
    db_session.commit()
    entries = [
        {"url": "https://compassgrill.co.il/categories/grill-accessories", "slug": "grill-accessories", "title": "אביזרים לגריל", "type": "category", "tokens": articles._tokenize_hebrew("אביזרים לגריל מדחום לבשר אבני בזלת")},
        {"url": "https://compassgrill.co.il/products/picanha", "slug": "picanha", "title": "פיקניה", "type": "product", "tokens": articles._tokenize_hebrew("פיקניה על הגריל")},
    ]
    monkeypatch.setattr("app.services.content_articles._load_sitemap_index", lambda force_refresh=False: (entries, {"sitemap_loaded_count": 1, "products_loaded_count": 1, "categories_loaded_count": 1}))

    links, debug = articles._discover_related_links(db_session, "מדחום לבשר", limit=6)
    body = "<p>מדחום לבשר מאפשר מדידה מדויקת בגריל.</p><p>בחירת אביזרים נכונה משפרת את התוצאה.</p>"
    injected_body, injected = articles.inject_internal_links_into_html(body, links)

    assert any("thermometer" in link["url"] for link in links)
    assert any("grill-accessories" in link["url"] for link in links)
    assert "garden-sofa" not in {link["url"].split("/")[-1] for link in links}
    assert injected
    assert "<a href='https://compassgrill.co.il/products/meat-thermometer'>" in injected_body
    assert all(float(link.get("relevance_score", 0)) >= 40 for link in links)
    assert debug["excluded_low_relevance_links"]

def test_required_keyword_expansion_examples_cover_basalt_thermometer_and_picanha() -> None:
    cases = [
        ("אבני בזלת לגריל", "אבני בזלת לגריל", "commercial_informational", ["אבני לבה לגריל", "אבני בזלת לגריל גז", "פיזור חום בגריל גז", "ניקוי אבני בזלת"]),
        ("מדחום לבשר", "מדחום לבשר", "commercial_informational", ["מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז"]),
        ("פיקניה", "פיקניה", "how-to", ["פיקניה על הגריל", "טמפרטורת פיקניה", "Reverse Sear פיקניה"]),
    ]
    for title, keyword, intent, required in cases:
        profile = _classify_topic(title, keyword, intent)
        metadata = build_topic_seo_metadata(keyword, title, profile)
        assert len(metadata["seo_keywords"]) >= 8
        for phrase in required:
            assert phrase in metadata["seo_keywords"]


def test_internal_link_index_entity_matching_priority_and_fallback(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.content_articles as articles

    entries = [
        {"url": "https://compassgrill.co.il/basalt-stones-for-gas-grill", "slug": "basalt-stones-for-gas-grill", "title": "אבני בזלת לגריל גז", "type": "product", "page_type": "product", "tokens": articles._tokenize_hebrew("אבני בזלת לגריל גז lava rocks basalt stones")},
        {"url": "https://compassgrill.co.il/category/grill-accessories", "slug": "grill-accessories", "title": "אביזרים לגריל", "type": "category", "page_type": "category", "tokens": articles._tokenize_hebrew("אביזרים לגריל גריל גז אבני לבה מדחום")},
        {"url": "https://compassgrill.co.il/meat-thermometer", "slug": "meat-thermometer", "title": "מדחום לבשר", "type": "product", "page_type": "product", "tokens": articles._tokenize_hebrew("מדחום לבשר meat thermometer probe")},
        {"url": "https://compassgrill.co.il/feedlot-picanha", "slug": "feedlot-picanha", "title": "פיקניה פידלוט", "type": "product", "page_type": "product", "tokens": articles._tokenize_hebrew("פיקניה picanha beef steak")},
        {"url": "https://compassgrill.co.il/unrelated-fish-knife", "slug": "unrelated-fish-knife", "title": "סכין דגים", "type": "product", "page_type": "product", "tokens": articles._tokenize_hebrew("סכין דגים")},
    ]
    stats = {"sitemap_loaded_count": 1, "products_loaded_count": 3, "categories_loaded_count": 1, "internal_link_index_status": "loaded"}
    monkeypatch.setattr("app.services.content_articles._load_sitemap_index", lambda force_refresh=False: (entries, stats))

    basalt_links, basalt_debug = articles._discover_related_links(db_session, "אבני בזלת לגריל", limit=6)
    assert basalt_links[0]["url"] == "https://compassgrill.co.il/basalt-stones-for-gas-grill"
    assert any(link["url"] == "https://compassgrill.co.il/category/grill-accessories" for link in basalt_links)
    assert basalt_debug["link_candidates_count"] >= 2

    fallback_entries = [entry for entry in entries if "basalt-stones" not in entry["url"]]
    monkeypatch.setattr("app.services.content_articles._load_sitemap_index", lambda force_refresh=False: (fallback_entries, stats))
    fallback_links, _ = articles._discover_related_links(db_session, "אבני בזלת לגריל", limit=6)
    assert fallback_links[0]["url"] == "https://compassgrill.co.il/category/grill-accessories"

    thermometer_links, _ = articles._discover_related_links(db_session, "מדחום לבשר", limit=6)
    assert thermometer_links[0]["url"] == "https://compassgrill.co.il/meat-thermometer"

    picanha_links, _ = articles._discover_related_links(db_session, "פיקניה", limit=6)
    assert picanha_links[0]["url"] == "https://compassgrill.co.il/feedlot-picanha"
    assert not any("unrelated-fish-knife" in link["url"] for link in basalt_links + thermometer_links + picanha_links)


def test_employee_workspace_keywords_field_uses_expanded_comma_separated_keywords(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.content_articles._load_sitemap_index", lambda force_refresh=False: ([], {"sitemap_loaded_count": 0, "products_loaded_count": 0, "categories_loaded_count": 0}))
    client.post("/content/articles/generate-topic-draft", json={"topic_title": "אבני בזלת לגריל", "focus_keyword": "אבני בזלת לגריל", "target_intent": "commercial_informational"})
    html = client.get("/seo/simple-workspace").text
    assert "מילות מפתח לקידום במנועי חיפוש" in html
    assert "אבני בזלת לגריל, אבני לבה לגריל" in html
    assert "ניקוי אבני בזלת" in html
