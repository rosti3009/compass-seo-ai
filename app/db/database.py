from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base SQLAlchemy model."""


def get_db() -> Generator[Session, None, None]:
    """Yield a database session for FastAPI dependencies."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_sqlite_schema_compatibility(bind_engine=None) -> None:
    """Add backward-compatible columns that SQLite ``create_all`` cannot add to existing tables."""
    bind = bind_engine or engine

    if bind.dialect.name != "sqlite":
        return

    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    compatibility_columns = {
        "page_audits": {
            "seo_score_delta": "FLOAT DEFAULT 0.0",
            "page_type": "VARCHAR(32) DEFAULT 'unknown'",
            "seo_risk_level": "VARCHAR(32) DEFAULT 'low'",
            "remediation_suggestions": "TEXT DEFAULT '[]'",
            "context_keywords": "TEXT DEFAULT '[]'",
            "primary_intent": "VARCHAR(64) DEFAULT 'general'",
            "commercial_intent_score": "FLOAT DEFAULT 0.0",
        },
        "istore_seo_approvals": {
            "target_type": "VARCHAR(32) DEFAULT 'product'",
            "target_id": "VARCHAR(255) DEFAULT ''",
            "target_url": "VARCHAR(1024)",
            "field_path": "VARCHAR(255) DEFAULT ''",
            "current_value": "TEXT",
            "proposed_value": "TEXT DEFAULT ''",
            "seo_reason": "TEXT DEFAULT ''",
            "risk_level": "VARCHAR(32) DEFAULT 'low'",
            "status": "VARCHAR(32) DEFAULT 'PENDING_APPROVAL'",
            "before_snapshot_json": "TEXT DEFAULT '{}'",
            "proposed_payload_json": "TEXT DEFAULT '{}'",
            "rollback_payload_json": "TEXT DEFAULT '{}'",
            "publish_response_json": "TEXT DEFAULT '{}'",
            "publish_log_json": "TEXT DEFAULT '[]'",
            "publish_timestamp": "DATETIME",
            "approved_by": "VARCHAR(255)",
            "approval_action": "VARCHAR(64)",
            "approval_metadata_json": "TEXT DEFAULT '{}'",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
        "seo_strategy_recommendations": {
            "priority_score": "FLOAT DEFAULT 0.0",
            "traffic_potential_score": "FLOAT DEFAULT 0.0",
            "ctr_opportunity_score": "FLOAT DEFAULT 0.0",
            "ranking_opportunity_score": "FLOAT DEFAULT 0.0",
            "internal_link_score": "FLOAT DEFAULT 0.0",
            "topical_authority_score": "FLOAT DEFAULT 0.0",
            "content_gap_score": "FLOAT DEFAULT 0.0",
            "publishing_readiness_score": "FLOAT DEFAULT 0.0",
            "ai_summary": "TEXT DEFAULT ''",
            "recommended_action": "TEXT DEFAULT ''",
            "reasoning": "TEXT DEFAULT ''",
            "status": "VARCHAR(32) DEFAULT 'pending'",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
    }

    with bind.begin() as connection:
        for table_name, columns in compatibility_columns.items():
            if table_name not in existing_tables:
                continue

            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}

            for column_name, column_definition in columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {column_name} {column_definition}"
                        )
                    )