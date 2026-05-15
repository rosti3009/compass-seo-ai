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
