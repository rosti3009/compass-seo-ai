from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
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


def test_daily_article_draft_and_required_fields(client: TestClient) -> None:
    res = client.post('/content/articles/generate-daily-draft')
    assert res.status_code == 200
    draft = res.json()['draft']
    assert draft['status'] == 'CONTENT_DRAFT'
    assert '<h1>' in draft['article_body']
    assert '<h2>' in draft['article_body']
    assert 'שאלות נפוצות' in draft['article_body']
    assert draft['featured_image_prompt']
    assert _is_hebrew(draft['image_alt_text'])
    assert draft['image_filename_slug'].replace('-', '').isalnum()
    assert draft['image_publish_status'] == 'NOT_PUBLISHED'


def test_duplicate_topic_avoided_and_publish_requires_approval(client: TestClient) -> None:
    first = client.post('/content/articles/generate-daily-draft').json()['draft']
    second = client.post('/content/articles/generate-daily-draft').json()['draft']
    assert first['topic_title'] != second['topic_title']
    publish_block = client.post(f"/content/articles/{second['id']}/publish")
    assert publish_block.status_code == 400
    client.post(f"/content/articles/{second['id']}/approve")
    publish_ok = client.post(f"/content/articles/{second['id']}/publish")
    assert publish_ok.status_code == 200
    assert publish_ok.json()['published'] is False


def test_draft_visible_in_simple_workspace(client: TestClient) -> None:
    client.post('/content/articles/generate-daily-draft')
    response = client.get('/seo/simple-workspace')
    assert response.status_code == 200
    assert 'מאמרים לאישור' in response.text
