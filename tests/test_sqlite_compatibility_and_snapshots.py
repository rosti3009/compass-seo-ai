from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, ensure_sqlite_schema_compatibility, get_db
from app.db.models import PageAudit, PageScoreSnapshot, SEOStrategyRecommendation
from app.main import app
from app.services.crawler import PageSEOResult, SEOCrawler


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _client_for_session(db: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_sqlite_startup_migrates_legacy_strategy_columns_without_dropping_data(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE seo_strategy_recommendations (
                    id INTEGER PRIMARY KEY,
                    page_url VARCHAR(1024) NOT NULL,
                    recommendation_type VARCHAR(64) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO seo_strategy_recommendations (id, page_url, recommendation_type)
                VALUES (1, 'https://example.com/legacy', 'expand_content')
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE crawl_runs (
                    id INTEGER PRIMARY KEY,
                    target_domain VARCHAR(255) NOT NULL,
                    started_at DATETIME,
                    completed_at DATETIME,
                    pages_crawled INTEGER,
                    average_score FLOAT,
                    status VARCHAR(32),
                    error_message TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE page_audits (
                    id INTEGER PRIMARY KEY,
                    crawl_run_id INTEGER NOT NULL,
                    url VARCHAR(1024) NOT NULL,
                    status_code INTEGER,
                    title VARCHAR(512),
                    meta_description VARCHAR(1024),
                    h1 VARCHAR(512),
                    canonical VARCHAR(1024),
                    word_count INTEGER,
                    internal_links INTEGER,
                    missing_fields VARCHAR(512),
                    seo_score FLOAT,
                    crawled_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO crawl_runs (id, target_domain, started_at, pages_crawled, average_score, status)
                VALUES (1, 'https://example.com', '2026-05-15 00:00:00', 1, 0, 'completed')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO page_audits (
                    id, crawl_run_id, url, status_code, title, meta_description, h1, canonical,
                    word_count, internal_links, missing_fields, seo_score, crawled_at
                ) VALUES (
                    1, 1, 'https://example.com/legacy', 200, NULL, NULL, 'Legacy', NULL,
                    NULL, 0, 'title,meta_description', NULL, '2026-05-15 00:00:00'
                )
                """
            )
        )

    Base.metadata.create_all(bind=engine)
    ensure_sqlite_schema_compatibility(engine)

    inspector = inspect(engine)
    strategy_columns = {column["name"] for column in inspector.get_columns("seo_strategy_recommendations")}
    page_columns = {column["name"] for column in inspector.get_columns("page_audits")}
    assert "priority_score" in strategy_columns
    assert "traffic_potential_score" in strategy_columns
    assert "publishing_readiness_score" in strategy_columns
    assert "seo_score_delta" in page_columns
    assert "seo_risk_level" in page_columns
    assert "remediation_suggestions" in page_columns
    assert "context_keywords" in page_columns
    assert "commercial_intent_score" in page_columns
    assert "page_score_snapshots" in inspector.get_table_names()

    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_local()
    try:
        assert db.query(SEOStrategyRecommendation).count() == 1
        assert db.query(PageAudit).count() == 1
        assert db.query(PageScoreSnapshot).count() == 0
        client_context = _client_for_session(db)
        client = next(client_context)
        try:
            recommendations = client.get("/seo/strategy/recommendations")
            dashboard = client.get("/")
            strategy_view = client.get("/seo/strategy-view")
        finally:
            client_context.close()
        assert recommendations.status_code == 200
        assert recommendations.json()["recommendations"][0]["priority_score"] == 0.0
        assert dashboard.status_code == 200
        assert strategy_view.status_code == 200
    finally:
        db.close()


def test_score_snapshots_are_created_by_crawl_persistence_and_delta_uses_previous_snapshot(db_session: Session) -> None:
    crawler = SEOCrawler("https://example.com", max_pages=1)
    crawler._crawl = lambda: [  # type: ignore[method-assign]
        PageSEOResult(
            url="https://example.com/",
            status_code=200,
            title="Home",
            meta_description="Description",
            h1="Home",
            canonical="https://example.com/",
            word_count=900,
            internal_links=3,
            missing_fields=[],
            seo_score=70.0,
            page_type="general",
            is_product=False,
            is_category=False,
        )
    ]

    first_run, first_pages = crawler.run(db_session)

    assert first_run.status == "completed"
    assert first_pages[0].seo_score_delta == 0.0
    first_snapshot = db_session.query(PageScoreSnapshot).one()
    assert first_snapshot.previous_seo_score is None
    assert first_snapshot.seo_score_delta == 0.0

    crawler._crawl = lambda: [  # type: ignore[method-assign]
        PageSEOResult(
            url="https://example.com/",
            status_code=200,
            title="Home",
            meta_description="Description",
            h1="Home",
            canonical="https://example.com/",
            word_count=1200,
            internal_links=8,
            missing_fields=[],
            seo_score=86.5,
            page_type="general",
            is_product=False,
            is_category=False,
        )
    ]

    second_run, second_pages = crawler.run(db_session)

    assert second_run.status == "completed"
    assert second_pages[0].seo_score_delta == 16.5
    snapshots = db_session.query(PageScoreSnapshot).order_by(PageScoreSnapshot.id).all()
    assert len(snapshots) == 2
    assert snapshots[1].previous_seo_score == 70.0
    assert snapshots[1].seo_score_delta == 16.5


def test_snapshot_table_has_expected_model_columns(db_session: Session) -> None:
    snapshot = PageScoreSnapshot(
        page_audit_id=1,
        crawl_run_id=1,
        url="https://example.com/scored",
        seo_score=55.0,
        previous_seo_score=50.0,
        seo_score_delta=5.0,
        created_at=datetime.now(UTC),
    )

    payload = snapshot.to_dict()

    assert payload["url"] == "https://example.com/scored"
    assert payload["seo_score"] == 55.0
    assert payload["previous_seo_score"] == 50.0
    assert payload["seo_score_delta"] == 5.0

def test_sqlite_startup_migrates_content_article_draft_columns_and_preserves_rows(tmp_path) -> None:
    db_path = tmp_path / "legacy_drafts.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE content_article_drafts (
                    id INTEGER PRIMARY KEY,
                    status VARCHAR(32) NOT NULL,
                    topic_title VARCHAR(512) NOT NULL,
                    title VARCHAR(512) NOT NULL,
                    slug VARCHAR(255) NOT NULL,
                    meta_title VARCHAR(512) NOT NULL,
                    meta_description TEXT NOT NULL,
                    focus_keyword VARCHAR(255) NOT NULL,
                    target_intent VARCHAR(128) NOT NULL,
                    article_body TEXT NOT NULL,
                    suggested_related_products_json TEXT,
                    internal_links_json TEXT,
                    faq_schema_json TEXT,
                    featured_image_prompt TEXT,
                    section_image_prompts_json TEXT,
                    image_alt_text VARCHAR(512),
                    image_title VARCHAR(512),
                    image_caption VARCHAR(512),
                    image_filename_slug VARCHAR(255),
                    image_style_rules TEXT,
                    generated_image_url VARCHAR(1024),
                    uploaded_media_id VARCHAR(255),
                    image_publish_status VARCHAR(32),
                    review_notes TEXT,
                    approved_by VARCHAR(255),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO content_article_drafts (
                    id, status, topic_title, title, slug, meta_title, meta_description,
                    focus_keyword, target_intent, article_body, suggested_related_products_json,
                    internal_links_json, faq_schema_json, featured_image_prompt, section_image_prompts_json,
                    image_alt_text, image_title, image_caption, image_filename_slug, image_style_rules,
                    generated_image_url, uploaded_media_id, image_publish_status, review_notes,
                    approved_by, created_at, updated_at
                ) VALUES (
                    1, 'CONTENT_DRAFT', 'Legacy Topic', 'Legacy Title', 'legacy-title', 'Legacy Meta', 'Legacy Desc',
                    'legacy-keyword', 'informational', 'Legacy body', '[]', '[]', '{}', '', '[]',
                    '', '', '', '', '', NULL, NULL, 'NOT_PUBLISHED', NULL,
                    NULL, '2026-05-20 00:00:00', '2026-05-20 00:00:00'
                )
                """
            )
        )

    Base.metadata.create_all(bind=engine)
    ensure_sqlite_schema_compatibility(engine)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("content_article_drafts")}
    expected_new_columns = {
        "target_site_section",
        "target_publish_type",
        "target_blog_base_url",
        "target_path",
        "target_url",
        "publish_destination_status",
        "featured_image_status",
        "featured_image_url",
        "featured_image_local_path",
        "verification_status",
        "published_url",
        "published_at",
        "is_active_manual_article",
    }
    assert expected_new_columns.issubset(columns)

    with engine.connect() as connection:
        row_count = connection.execute(text("SELECT COUNT(*) FROM content_article_drafts")).scalar_one()
        legacy_row = connection.execute(
            text("SELECT topic_title, slug FROM content_article_drafts WHERE id = 1")
        ).one()

    assert row_count == 1
    assert legacy_row.topic_title == "Legacy Topic"
    assert legacy_row.slug == "legacy-title"
