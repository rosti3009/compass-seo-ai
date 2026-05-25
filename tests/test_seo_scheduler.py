from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import SEOAutomationRun, SEOScheduleConfig
from app.main import app
from app.services.seo_scheduler import calculate_next_run, ensure_default_schedule_config


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


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_next_run_calculation_daily() -> None:
    before_hour = datetime(2026, 5, 13, 4, 30, tzinfo=UTC)
    after_hour = datetime(2026, 5, 13, 5, 30, tzinfo=UTC)

    assert calculate_next_run("daily", 5, before_hour) == datetime(2026, 5, 13, 5, 0, tzinfo=UTC)
    assert calculate_next_run("daily", 5, after_hour) == datetime(2026, 5, 14, 5, 0, tzinfo=UTC)


def test_next_run_calculation_weekly() -> None:
    reference = datetime(2026, 5, 13, 5, 30, tzinfo=UTC)

    assert calculate_next_run("weekly", 5, reference) == datetime(2026, 5, 20, 5, 0, tzinfo=UTC)


def test_default_config_creation(db_session: Session) -> None:
    config = ensure_default_schedule_config(db_session)

    assert config.name == "Daily SEO Automation"
    assert config.frequency == "daily"
    assert config.hour_utc == 5
    assert config.max_tasks == 10
    assert config.generate_articles is False
    assert config.sync_gsc is True
    assert config.enabled is False
    assert config.next_run_at is not None


def test_enable_disable(client: TestClient) -> None:
    list_response = client.get("/seo/scheduler/configs")
    config_id = list_response.json()["configs"][0]["id"]

    enable_response = client.post(f"/seo/scheduler/configs/{config_id}/enable")
    disable_response = client.post(f"/seo/scheduler/configs/{config_id}/disable")

    assert enable_response.status_code == 200
    assert enable_response.json()["config"]["enabled"] is True
    assert disable_response.status_code == 200
    assert disable_response.json()["config"]["enabled"] is False


def test_run_due_schedules(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_seo_automation(
        db: Session, max_tasks: int = 10, generate_articles: bool = False, sync_gsc: bool = True
    ) -> SEOAutomationRun:
        run = SEOAutomationRun(
            status="completed",
            seo_tasks_created=max_tasks,
            fixes_created=1,
            publishing_packages_created=0,
            summary_json='{"safety":{"auto_publish":false}}',
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr("app.services.seo_scheduler.run_seo_automation", fake_run_seo_automation)
    config = SEOScheduleConfig(
        name="Due daily",
        enabled=True,
        frequency="daily",
        hour_utc=5,
        max_tasks=4,
        generate_articles=False,
        sync_gsc=True,
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(config)
    db_session.commit()

    response = client.post("/seo/scheduler/run-due")

    assert response.status_code == 200
    payload = response.json()
    assert payload["due_count"] == 1
    assert payload["runs"][0]["seo_tasks_created"] == 4
    assert payload["runs"][0]["publishing_packages_created"] == 0
    db_session.refresh(config)
    assert config.last_run_at is not None
    assert config.next_run_at is not None
    assert config.next_run_at > config.last_run_at


def test_scheduler_dashboard_view_loads(client: TestClient, db_session: Session) -> None:
    db_session.add(
        SEOScheduleConfig(
            name="Weekly SEO Automation",
            enabled=True,
            frequency="weekly",
            hour_utc=6,
            max_tasks=3,
            generate_articles=False,
            sync_gsc=True,
            next_run_at=datetime(2026, 5, 20, 6, 0, tzinfo=UTC),
        )
    )
    db_session.commit()

    response = client.get("/seo/scheduler-view")

    assert response.status_code == 200
    assert "SEO Scheduler" in response.text
    assert "Enabled schedules" in response.text
    assert "Related automation runs" in response.text


def test_scheduler_preserves_no_auto_publish_safety(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_seo_automation(
        db: Session, max_tasks: int = 10, generate_articles: bool = False, sync_gsc: bool = True
    ) -> SEOAutomationRun:
        run = SEOAutomationRun(
            status="completed",
            fixes_created=2,
            publishing_packages_created=0,
            summary_json="{}",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr("app.services.seo_scheduler.run_seo_automation", fake_run_seo_automation)
    db_session.add(
        SEOScheduleConfig(
            name="Safe due",
            enabled=True,
            frequency="daily",
            hour_utc=5,
            max_tasks=2,
            generate_articles=False,
            sync_gsc=False,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    db_session.commit()

    response = client.post("/seo/scheduler/run-due")

    assert response.status_code == 200
    safety = response.json()["runs"][0]["summary"]["safety"]
    assert safety["auto_approve_fixes"] is False
    assert safety["auto_publish"] is False
    assert safety["auto_mark_publishing_packages_applied"] is False
    assert response.json()["runs"][0]["publishing_packages_created"] == 0

def test_daily_article_env_disabled_by_default() -> None:
    from app.core.config import Settings

    settings = Settings()
    assert settings.daily_article_generation_enabled is False


def test_scheduler_generates_one_draft_per_day_guard(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.models import ContentArticleDraft

    monkeypatch.setattr('app.services.seo_scheduler.settings.daily_article_generation_enabled', True)
    monkeypatch.setattr('app.services.seo_scheduler.settings.daily_article_generation_timezone', 'Asia/Jerusalem')

    def fake_run_seo_automation(db: Session, max_tasks: int = 10, generate_articles: bool = False, sync_gsc: bool = True) -> SEOAutomationRun:
        run = SEOAutomationRun(status='completed', summary_json='{}')
        db.add(run); db.commit(); db.refresh(run)
        return run

    monkeypatch.setattr('app.services.seo_scheduler.run_seo_automation', fake_run_seo_automation)
    for i in range(2):
        db_session.add(SEOScheduleConfig(name=f'due {i}', enabled=True, frequency='daily', hour_utc=5, max_tasks=1, generate_articles=False, sync_gsc=False, next_run_at=datetime.now(UTC)-timedelta(minutes=1)))
    db_session.commit()

    response = client.post('/seo/scheduler/run-due')
    assert response.status_code == 200
    assert db_session.query(ContentArticleDraft).count() == 1
