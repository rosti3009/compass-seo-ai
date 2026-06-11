from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base SQLAlchemy model."""


def _seo_task_source_sql() -> str:
    """Return SQL for backfilling a stable SEO task source on old SQLite rows."""
    return """
        CASE
            WHEN recommendation_json LIKE '%\"source\": \"gsc\"%' OR recommendation_json LIKE '%\"source\":\"gsc\"%'
                THEN 'gsc'
            WHEN recommendation_json LIKE '%\"source\": \"latest_crawl_gsc_enriched\"%'
                OR recommendation_json LIKE '%\"source\":\"latest_crawl_gsc_enriched\"%'
                THEN 'latest_crawl_gsc_enriched'
            WHEN recommendation_json LIKE '%\"source\": \"latest_crawl\"%'
                OR recommendation_json LIKE '%\"source\":\"latest_crawl\"%'
                THEN 'latest_crawl'
            WHEN recommendation_json LIKE '%\"source\": \"automation_latest_crawl_gsc_enriched\"%'
                OR recommendation_json LIKE '%\"source\":\"automation_latest_crawl_gsc_enriched\"%'
                THEN 'automation_latest_crawl_gsc_enriched'
            WHEN recommendation_json LIKE '%\"source\": \"automation_latest_crawl\"%'
                OR recommendation_json LIKE '%\"source\":\"automation_latest_crawl\"%'
                THEN 'automation_latest_crawl'
            ELSE 'seo_task'
        END
    """


def _remove_legacy_seo_tasks_page_url_unique_constraint(bind) -> None:
    """Drop/rebuild legacy SQLite uniqueness on seo_tasks.page_url without deleting rows.

    Old deployments created ``seo_tasks.page_url`` as globally unique. SQLite stores
    column/table unique constraints as an autoindex that cannot be dropped directly,
    so affected databases must be rebuilt in-place while preserving primary keys and
    dependent ``seo_fixes.task_id`` references.
    """
    with bind.connect() as connection:
        index_rows = connection.exec_driver_sql("PRAGMA index_list('seo_tasks')").fetchall()
        unique_page_url_indexes: list[str] = []
        requires_rebuild = False
        for index_row in index_rows:
            index_name = str(index_row[1])
            is_unique = bool(index_row[2])
            if not is_unique:
                continue
            index_columns = [
                str(column_row[2])
                for column_row in connection.exec_driver_sql(f"PRAGMA index_info('{index_name}')").fetchall()
            ]
            if index_columns == ["page_url"]:
                unique_page_url_indexes.append(index_name)
                requires_rebuild = requires_rebuild or index_name.startswith("sqlite_autoindex")

        if not unique_page_url_indexes:
            return

        if not requires_rebuild:
            for index_name in unique_page_url_indexes:
                connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index_name}"')
            connection.commit()
            print("[DB MIGRATION] Removed legacy unique index on seo_tasks.page_url")
            return

        table_columns = {
            str(column_row[1]) for column_row in connection.exec_driver_sql("PRAGMA table_info('seo_tasks')").fetchall()
        }
        source_select = "source" if "source" in table_columns else _seo_task_source_sql()

        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("ALTER TABLE seo_tasks RENAME TO seo_tasks_legacy_page_url_unique")
        connection.exec_driver_sql(
            """
            CREATE TABLE seo_tasks (
                id INTEGER NOT NULL,
                source VARCHAR(64),
                page_url VARCHAR(1024) NOT NULL,
                keyword VARCHAR(255),
                priority VARCHAR(32),
                status VARCHAR(32),
                suggested_title VARCHAR(512),
                suggested_h1 VARCHAR(512),
                meta_description VARCHAR(1024),
                recommendation_json TEXT,
                article_html TEXT,
                article_schema_json TEXT,
                faq_schema_json TEXT,
                article_status VARCHAR(32),
                created_at DATETIME,
                updated_at DATETIME,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            INSERT INTO seo_tasks (
                id, source, page_url, keyword, priority, status, suggested_title, suggested_h1,
                meta_description, recommendation_json, article_html, article_schema_json,
                faq_schema_json, article_status, created_at, updated_at
            )
            SELECT
                id,
                {source_select},
                page_url,
                keyword,
                COALESCE(priority, 'medium'),
                COALESCE(status, 'open'),
                suggested_title,
                suggested_h1,
                meta_description,
                COALESCE(recommendation_json, '{{}}'),
                article_html,
                COALESCE(article_schema_json, '{{}}'),
                COALESCE(faq_schema_json, '{{}}'),
                COALESCE(article_status, 'not_generated'),
                created_at,
                updated_at
            FROM seo_tasks_legacy_page_url_unique
            """
        )
        connection.exec_driver_sql("DROP TABLE seo_tasks_legacy_page_url_unique")
        for index_name, column_name in {
            "ix_seo_tasks_id": "id",
            "ix_seo_tasks_source": "source",
            "ix_seo_tasks_page_url": "page_url",
            "ix_seo_tasks_priority": "priority",
            "ix_seo_tasks_status": "status",
            "ix_seo_tasks_article_status": "article_status",
        }.items():
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index_name} ON seo_tasks ({column_name})")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_seo_tasks_source_keyword_page_url "
            "ON seo_tasks (source, keyword, page_url)"
        )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        print("[DB MIGRATION] Rebuilt seo_tasks without legacy unique page_url constraint")


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

    if "seo_tasks" in existing_tables:
        _remove_legacy_seo_tasks_page_url_unique_constraint(bind)
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
            "source_page_audit_id": "INTEGER",
            "source_url": "VARCHAR(1024)",
            "istore_product_id": "VARCHAR(255)",
            "publish_mapping_verified": "BOOLEAN DEFAULT 0",
            "mapping_conflict": "BOOLEAN DEFAULT 0",
            "mapping_confidence": "INTEGER DEFAULT 0",
            "mapping_source": "VARCHAR(64)",
            "field_path": "VARCHAR(255) DEFAULT ''",
            "current_value": "TEXT",
            "proposed_value": "TEXT DEFAULT ''",
            "seo_reason": "TEXT DEFAULT ''",
            "risk_level": "VARCHAR(32) DEFAULT 'low'",
            "source_audit_id": "INTEGER",
            "issue_type": "VARCHAR(64)",
            "priority_score": "FLOAT DEFAULT 0.0",
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
            "generated_engine_version": "VARCHAR(128)",
            "generated_at": "DATETIME",
            "invalidated_at": "DATETIME",
            "invalidation_reason": "TEXT",
            "regenerated_from_id": "INTEGER",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
        "istore_products": {
            "normalized_slug": "VARCHAR(512)",
        },
        "content_article_drafts": {
            "target_site_section": "VARCHAR(64) DEFAULT 'blog' NOT NULL",
            "target_publish_type": "VARCHAR(64) DEFAULT 'article' NOT NULL",
            "target_blog_base_url": "VARCHAR(512) DEFAULT 'https://compassgrill.co.il/blog/' NOT NULL",
            "target_path": "VARCHAR(512) DEFAULT '' NOT NULL",
            "target_url": "VARCHAR(1024) DEFAULT '' NOT NULL",
            "publish_destination_status": "VARCHAR(64) DEFAULT 'ready' NOT NULL",
            "featured_image_status": "VARCHAR(64) DEFAULT 'planned' NOT NULL",
            "featured_image_url": "VARCHAR(1024)",
            "featured_image_local_path": "VARCHAR(1024)",
            "verification_status": "VARCHAR(64) DEFAULT 'NOT_VERIFIED' NOT NULL",
            "published_url": "VARCHAR(1024)",
            "published_at": "DATETIME",
            "image_generation_metadata_json": "TEXT DEFAULT '{}'",
            "is_active_manual_article": "BOOLEAN DEFAULT 0 NOT NULL",
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
        "seo_tasks": {
            "source": "VARCHAR(64) DEFAULT 'seo_task'",
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
                    print(f"[DB MIGRATION] Added missing column {table_name}.{column_name}")

        compatibility_indexes = {
            "ix_istore_products_normalized_slug": ("istore_products", "normalized_slug"),
            "ix_istore_products_canonical_url": ("istore_products", "canonical_url"),
            "ix_istore_products_istore_product_id": ("istore_products", "istore_product_id"),
        }
        for index_name, (table_name, column_name) in compatibility_indexes.items():
            if table_name in existing_tables:
                connection.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})"))

        if "seo_tasks" in existing_tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_seo_tasks_source_keyword_page_url "
                    "ON seo_tasks (source, keyword, page_url)"
                )
            )
            connection.execute(
                text(
                    f"UPDATE seo_tasks SET source = {_seo_task_source_sql()} "  # noqa: S608 - static migration SQL.
                    "WHERE source IS NULL OR source = 'seo_task'"
                )
            )
