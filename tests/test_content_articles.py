from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import _blog_publish_adapter_ready
from app.db.database import Base, get_db
from app.db.models import IStoreProduct
from app.main import app


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


def test_workspace_has_article_controls(client: TestClient) -> None:
    response = client.get('/seo/simple-workspace')
    assert response.status_code == 200
    assert 'צור מאמר חדש' in response.text
    assert 'מאמרים לאישור' in response.text


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
    assert payload['download_image_url'] is None


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
    assert all(term in body for term in ['hickory', 'oak', 'apple', 'mesquite'])
    assert 'thin blue smoke' in body
    prompt_blob = (draft['featured_image_prompt'] + ' ' + ' '.join(i.get('prompt', '') for i in draft.get('section_image_prompts', []))).lower()
    assert any(t in prompt_blob for t in ['smoker box', 'wood chips'])

    assert float(draft["quality"]["article_quality_score"]) > 75

    details = client.get(f"/content/articles/{draft['id']}").json()['draft']
    assert details['debug']['generator_version'] == 'v2-topic-specific-2026-05-25'
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
    client.post(f"/content/articles/{draft['id']}/approve")

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

    page = client.get('/seo/simple-workspace').text
    assert 'https://cdn.example.com/' in page
    assert 'פתח תמונה' in page
    assert 'העתק קישור תמונה' in page
    assert 'הורד תמונה' in page


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
    for key in ['provider_name','provider_response_received','raw_provider_url_present','generated_image_url','featured_image_url','image_url_present','image_storage_success','image_generation_metadata']:
        assert key in diagnostics
