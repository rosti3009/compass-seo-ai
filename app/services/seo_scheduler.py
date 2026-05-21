import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import SEOAutomationRun, SEOScheduleConfig
from app.services.content_articles import generate_daily_article_draft
from app.services.seo_automation import run_seo_automation

DEFAULT_SCHEDULE_CONFIG = {
    "name": "Daily SEO Automation",
    "enabled": False,
    "frequency": "daily",
    "hour_utc": 5,
    "max_tasks": 10,
    "generate_articles": False,
    "sync_gsc": True,
}

SCHEDULER_SAFETY_RULES = {
    "auto_approve_fixes": False,
    "auto_publish": False,
    "auto_mark_publishing_packages_applied": False,
}


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_frequency(frequency: str) -> str:
    normalized = frequency.strip().lower()
    if normalized not in SEOScheduleConfig.VALID_FREQUENCIES:
        raise ValueError("frequency must be 'daily' or 'weekly'")
    return normalized


def _validate_hour(hour_utc: int) -> int:
    hour = int(hour_utc)
    if hour < 0 or hour > 23:
        raise ValueError("hour_utc must be between 0 and 23")
    return hour


def calculate_next_run(frequency: str, hour_utc: int, from_time: datetime | None = None) -> datetime:
    """Calculate the next UTC run time for a daily or weekly schedule."""
    frequency = _validate_frequency(frequency)
    hour_utc = _validate_hour(hour_utc)
    reference = _utc(from_time)
    candidate = reference.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate <= reference:
        candidate += timedelta(days=1 if frequency == "daily" else 7)
    return candidate


def ensure_default_schedule_config(db: Session) -> SEOScheduleConfig:
    """Create the default disabled scheduler config when no schedule exists."""
    config = db.query(SEOScheduleConfig).order_by(SEOScheduleConfig.id.asc()).first()
    if config is not None:
        return config
    config = SEOScheduleConfig(
        **DEFAULT_SCHEDULE_CONFIG,
        next_run_at=calculate_next_run(DEFAULT_SCHEDULE_CONFIG["frequency"], DEFAULT_SCHEDULE_CONFIG["hour_utc"]),
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def create_schedule_config(
    db: Session,
    name: str,
    frequency: str = "daily",
    hour_utc: int = 5,
    max_tasks: int = 10,
    generate_articles: bool = False,
    sync_gsc: bool = True,
    enabled: bool = False,
) -> SEOScheduleConfig:
    frequency = _validate_frequency(frequency)
    hour_utc = _validate_hour(hour_utc)
    max_tasks = max(1, min(int(max_tasks), 100))
    config = SEOScheduleConfig(
        name=name.strip() or "SEO Automation",
        enabled=enabled,
        frequency=frequency,
        hour_utc=hour_utc,
        max_tasks=max_tasks,
        generate_articles=generate_articles,
        sync_gsc=sync_gsc,
        next_run_at=calculate_next_run(frequency, hour_utc),
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def set_schedule_enabled(db: Session, config: SEOScheduleConfig, enabled: bool) -> SEOScheduleConfig:
    config.enabled = enabled
    if enabled and config.next_run_at is None:
        config.next_run_at = calculate_next_run(config.frequency, config.hour_utc)
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def get_due_schedules(db: Session, now: datetime | None = None) -> list[SEOScheduleConfig]:
    """Return enabled schedules whose next_run_at is due at or before now."""
    ensure_default_schedule_config(db)
    current = _utc(now)
    schedules = (
        db.query(SEOScheduleConfig)
        .filter(SEOScheduleConfig.enabled.is_(True), SEOScheduleConfig.next_run_at.is_not(None))
        .order_by(SEOScheduleConfig.next_run_at.asc(), SEOScheduleConfig.id.asc())
        .all()
    )
    return [schedule for schedule in schedules if schedule.next_run_at and _utc(schedule.next_run_at) <= current]


def update_schedule_after_run(
    db: Session, config: SEOScheduleConfig, ran_at: datetime | None = None
) -> SEOScheduleConfig:
    """Record a scheduler execution and advance next_run_at without approving or publishing anything."""
    run_time = _utc(ran_at)
    config.last_run_at = run_time
    config.next_run_at = calculate_next_run(config.frequency, config.hour_utc, run_time)
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def _tag_run_with_schedule(db: Session, run: SEOAutomationRun, config: SEOScheduleConfig) -> SEOAutomationRun:
    summary = run.summary if isinstance(run.summary, dict) else {}
    summary["scheduler"] = {
        "schedule_config_id": config.id,
        "schedule_name": config.name,
        "frequency": config.frequency,
        "hour_utc": config.hour_utc,
    }
    summary["safety"] = {**summary.get("safety", {}), **SCHEDULER_SAFETY_RULES}
    run.summary_json = json.dumps(summary, ensure_ascii=False)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def run_due_schedules(db: Session, now: datetime | None = None) -> dict[str, object]:
    """Run every due enabled schedule using the safe SEO automation pipeline."""
    due = get_due_schedules(db, now=now)
    runs: list[SEOAutomationRun] = []
    for config in due:
        article_draft_id = None
        if settings.content_daily_articles_enabled:
            article = generate_daily_article_draft(db)
            article_draft_id = article.id
        run = run_seo_automation(
            db,
            max_tasks=config.max_tasks,
            generate_articles=config.generate_articles,
            sync_gsc=config.sync_gsc,
        )
        if article_draft_id:
            summary = run.summary if isinstance(run.summary, dict) else {}
            summary["content_daily_article_draft_id"] = article_draft_id
            run.summary_json = json.dumps(summary, ensure_ascii=False)
            db.add(run)
            db.commit()
            db.refresh(run)
        runs.append(_tag_run_with_schedule(db, run, config))
        update_schedule_after_run(db, config, ran_at=now)
    return {
        "success": True,
        "due_count": len(due),
        "runs": [run.to_dict() for run in runs],
        "safety": SCHEDULER_SAFETY_RULES,
    }
