import json
from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.services.seo_url_filters import get_url_exclusion_reason


def _json_load(value: str | None, default: object | None = None) -> object:
    """Safely parse a JSON model field for API responses."""
    fallback = {} if default is None else default
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class CrawlRun(Base):
    """A single crawler execution."""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    target_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    average_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="running")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    pages: Mapped[list["PageAudit"]] = relationship(back_populates="crawl_run", cascade="all, delete-orphan")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "target_domain": self.target_domain,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "pages_crawled": self.pages_crawled,
            "average_score": self.average_score,
            "status": self.status or "running",
            "error_message": self.error_message,
        }


class PageAudit(Base):
    """SEO audit metrics for a crawled page."""

    __tablename__ = "page_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    h1: Mapped[str | None] = mapped_column(String(512), nullable=True)
    canonical: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    internal_links: Mapped[int] = mapped_column(Integer, default=0)
    missing_fields: Mapped[str] = mapped_column(String(512), default="")
    page_type: Mapped[str] = mapped_column(String(32), default="unknown")
    seo_score: Mapped[float] = mapped_column(Float, default=0.0)
    seo_score_delta: Mapped[float] = mapped_column(Float, default=0.0)
    seo_risk_level: Mapped[str] = mapped_column(String(32), default="low")
    remediation_suggestions: Mapped[str] = mapped_column(Text, default="[]")
    context_keywords: Mapped[str] = mapped_column(Text, default="[]")
    primary_intent: Mapped[str] = mapped_column(String(64), default="general")
    commercial_intent_score: Mapped[float] = mapped_column(Float, default=0.0)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    crawl_run: Mapped[CrawlRun] = relationship(back_populates="pages")
    score_snapshots: Mapped[list["PageScoreSnapshot"]] = relationship(
        back_populates="page_audit", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "crawl_run_id": self.crawl_run_id,
            "url": self.url,
            "status_code": self.status_code,
            "title": self.title,
            "meta_description": self.meta_description,
            "h1": self.h1,
            "canonical": self.canonical,
            "word_count": self.word_count,
            "internal_links": self.internal_links,
            "missing_fields": [field for field in self.missing_fields.split(",") if field],
            "page_type": self.page_type or "unknown",
            "seo_risk_level": self.seo_risk_level or "low",
            "remediation_suggestions": _json_load(self.remediation_suggestions, []),
            "context_keywords": _json_load(self.context_keywords, []),
            "primary_intent": self.primary_intent or "general",
            "commercial_intent_score": self.commercial_intent_score or 0.0,
            "seo_score": self.seo_score or 0.0,
            "seo_score_delta": self.seo_score_delta or 0.0,
            "crawled_at": self.crawled_at.isoformat() if self.crawled_at else None,
            "excluded_reason": get_url_exclusion_reason(self.url),
        }


class PageScoreSnapshot(Base):
    """Point-in-time SEO score for a crawled page URL."""

    __tablename__ = "page_score_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    page_audit_id: Mapped[int] = mapped_column(ForeignKey("page_audits.id"), nullable=False, index=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    seo_score: Mapped[float] = mapped_column(Float, default=0.0)
    previous_seo_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    seo_score_delta: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    page_audit: Mapped[PageAudit] = relationship(back_populates="score_snapshots")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_audit_id": self.page_audit_id,
            "crawl_run_id": self.crawl_run_id,
            "url": self.url,
            "seo_score": self.seo_score or 0.0,
            "previous_seo_score": self.previous_seo_score,
            "seo_score_delta": self.seo_score_delta or 0.0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GoogleOAuthToken(Base):
    """Stored Google user OAuth token for Search Console and GA4 access."""

    __tablename__ = "google_oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="google", nullable=False, index=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_uri: Mapped[str] = mapped_column(String(1024), default="https://oauth2.googleapis.com/token", nullable=False)
    client_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    client_secret: Mapped[str] = mapped_column(Text, nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    @property
    def scopes(self) -> list[str]:
        try:
            parsed = json.loads(self.scopes_json or "[]")
        except json.JSONDecodeError:
            return []
        return [str(scope) for scope in parsed] if isinstance(parsed, list) else []

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "scopes": self.scopes,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GSCKeywordMetric(Base):
    """Google Search Console keyword performance for a page/query/date."""

    __tablename__ = "gsc_keyword_metrics"
    __table_args__ = (UniqueConstraint("page_url", "query", "date", "source", name="uq_gsc_keyword_metric"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    page_url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0, index=True)
    ctr: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    average_position: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), default="gsc", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_url": self.page_url,
            "query": self.query,
            "clicks": self.clicks,
            "impressions": self.impressions,
            "ctr": self.ctr,
            "average_position": self.average_position,
            "date": self.date.isoformat() if self.date else None,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SEOScheduleConfig(Base):
    """Safe scheduled SEO automation settings.

    Schedules only prepare reviewable SEO work. They do not approve fixes,
    publish changes, or mark publishing packages as applied.
    """

    __tablename__ = "seo_schedule_configs"

    VALID_FREQUENCIES = {"daily", "weekly"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    frequency: Mapped[str] = mapped_column(String(32), default="daily", nullable=False, index=True)
    hour_utc: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_tasks: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    generate_articles: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sync_gsc: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "frequency": self.frequency,
            "hour_utc": self.hour_utc,
            "max_tasks": self.max_tasks,
            "generate_articles": self.generate_articles,
            "sync_gsc": self.sync_gsc,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "safety": {
                "auto_approve_fixes": False,
                "auto_publish": False,
                "auto_mark_publishing_packages_applied": False,
            },
        }


class SEOTask(Base):
    """Actionable SEO task generated from crawled page audit data."""

    __tablename__ = "seo_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    page_url: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True, index=True)
    keyword: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[str] = mapped_column(String(32), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    suggested_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    suggested_h1: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    recommendation_json: Mapped[str] = mapped_column(Text, default="{}")
    article_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    article_schema_json: Mapped[str] = mapped_column(Text, default="{}")
    faq_schema_json: Mapped[str] = mapped_column(Text, default="{}")
    article_status: Mapped[str] = mapped_column(String(32), default="not_generated", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    fixes: Mapped[list["SEOFix"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_url": self.page_url,
            "keyword": self.keyword,
            "priority": self.priority,
            "status": self.status or "open",
            "suggested_title": self.suggested_title,
            "suggested_h1": self.suggested_h1,
            "meta_description": self.meta_description,
            "recommendation_json": self.recommendation_json,
            "article_html": self.article_html,
            "article_schema_json": self.article_schema_json,
            "faq_schema_json": self.faq_schema_json,
            "article_status": self.article_status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "excluded_reason": get_url_exclusion_reason(self.page_url),
        }


class SEOFix(Base):
    """Reviewable website update package created from an SEO task."""

    __tablename__ = "seo_fixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("seo_tasks.id"), nullable=False, index=True)
    page_url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    fix_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    current_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_value: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(64), default="seo_task")
    notes_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    task: Mapped["SEOTask"] = relationship(back_populates="fixes")
    publishing_packages: Mapped[list["PublishingPackage"]] = relationship(
        back_populates="fix", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "page_url": self.page_url,
            "fix_type": self.fix_type,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "status": self.status or "draft",
            "confidence_score": self.confidence_score,
            "source": self.source,
            "notes_json": self.notes_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "excluded_reason": get_url_exclusion_reason(self.page_url),
        }


class PublishingPackage(Base):
    """Manual CMS publishing package prepared from an approved SEO fix."""

    __tablename__ = "publishing_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    fix_id: Mapped[int] = mapped_column(ForeignKey("seo_fixes.id"), nullable=False, index=True)
    page_url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    cms_type: Mapped[str] = mapped_column(String(64), default="istore", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    fix: Mapped["SEOFix"] = relationship(back_populates="publishing_packages")

    def to_dict(self) -> dict[str, object]:
        try:
            payload: object = json.loads(self.payload_json or "{}")
        except json.JSONDecodeError:
            payload = self.payload_json
        return {
            "id": self.id,
            "fix_id": self.fix_id,
            "page_url": self.page_url,
            "cms_type": self.cms_type,
            "payload_json": payload,
            "status": self.status or "draft",
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "excluded_reason": get_url_exclusion_reason(self.page_url),
        }


class SEOAutomationRun(Base):
    """A safe, human-reviewed SEO automation workflow execution."""

    __tablename__ = "seo_automation_runs"

    VALID_STATUSES = {"running", "completed", "failed", "completed_with_warnings"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crawl_run_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_runs.id"), nullable=True, index=True)
    gsc_synced_rows: Mapped[int] = mapped_column(Integer, default=0)
    seo_tasks_created: Mapped[int] = mapped_column(Integer, default=0)
    recommendations_generated: Mapped[int] = mapped_column(Integer, default=0)
    articles_generated: Mapped[int] = mapped_column(Integer, default=0)
    fixes_created: Mapped[int] = mapped_column(Integer, default=0)
    publishing_packages_created: Mapped[int] = mapped_column(Integer, default=0)
    strategy_recommendations_created: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")

    crawl_run: Mapped[CrawlRun | None] = relationship()

    @property
    def errors(self) -> list[object]:
        try:
            parsed = json.loads(self.errors_json or "[]")
        except json.JSONDecodeError:
            return [self.errors_json]
        return parsed if isinstance(parsed, list) else [parsed]

    @property
    def summary(self) -> dict[str, object]:
        try:
            parsed = json.loads(self.summary_json or "{}")
        except json.JSONDecodeError:
            return {"raw": self.summary_json}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status or "running",
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "crawl_run_id": self.crawl_run_id,
            "gsc_synced_rows": self.gsc_synced_rows,
            "seo_tasks_created": self.seo_tasks_created,
            "recommendations_generated": self.recommendations_generated,
            "articles_generated": self.articles_generated,
            "fixes_created": self.fixes_created,
            "publishing_packages_created": self.publishing_packages_created,
            "strategy_recommendations_created": self.strategy_recommendations_created,
            "errors": self.errors,
            "summary": self.summary,
        }


class SEOStrategyRecommendation(Base):
    """Prioritized AI SEO strategy recommendation across crawl, GSC, content, and publishing signals."""

    __tablename__ = "seo_strategy_recommendations"

    VALID_STATUSES = {"pending", "accepted", "ignored", "completed"}
    VALID_RECOMMENDATION_TYPES = {
        "rewrite_title",
        "rewrite_meta",
        "generate_article",
        "improve_internal_links",
        "create_cluster_content",
        "improve_ctr",
        "expand_content",
        "publish_fix_package",
        "merge_content",
        "noindex_page",
    }

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    page_url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    recommendation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    traffic_potential_score: Mapped[float] = mapped_column(Float, default=0.0)
    ctr_opportunity_score: Mapped[float] = mapped_column(Float, default=0.0)
    ranking_opportunity_score: Mapped[float] = mapped_column(Float, default=0.0)
    internal_link_score: Mapped[float] = mapped_column(Float, default=0.0)
    topical_authority_score: Mapped[float] = mapped_column(Float, default=0.0)
    content_gap_score: Mapped[float] = mapped_column(Float, default=0.0)
    publishing_readiness_score: Mapped[float] = mapped_column(Float, default=0.0)
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_url": self.page_url,
            "recommendation_type": self.recommendation_type,
            "priority_score": self.priority_score or 0.0,
            "traffic_potential_score": self.traffic_potential_score or 0.0,
            "ctr_opportunity_score": self.ctr_opportunity_score or 0.0,
            "ranking_opportunity_score": self.ranking_opportunity_score or 0.0,
            "internal_link_score": self.internal_link_score or 0.0,
            "topical_authority_score": self.topical_authority_score or 0.0,
            "content_gap_score": self.content_gap_score or 0.0,
            "publishing_readiness_score": self.publishing_readiness_score or 0.0,
            "ai_summary": self.ai_summary or "",
            "recommended_action": self.recommended_action or "",
            "reasoning": self.reasoning or "",
            "status": self.status or "pending",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

class IStoreSEOApproval(Base):
    """Human-approved ISTORE SEO change draft with publish/rollback audit data."""

    __tablename__ = "istore_seo_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="product", nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_url: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    source_page_audit_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    istore_product_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    publish_mapping_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    mapping_conflict: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    field_path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    current_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_value: Mapped[str] = mapped_column(Text, default="")
    seo_reason: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(32), default="low", nullable=False, index=True)
    source_audit_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING_APPROVAL", nullable=False, index=True)
    before_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    proposed_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    rollback_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    publish_response_json: Mapped[str] = mapped_column(Text, default="{}")
    publish_log_json: Mapped[str] = mapped_column(Text, default="[]")
    publish_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approval_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_url": self.target_url,
            "source_page_audit_id": self.source_page_audit_id,
            "source_url": self.source_url,
            "istore_product_id": self.istore_product_id,
            "publish_mapping_verified": self.publish_mapping_verified,
            "mapping_conflict": self.mapping_conflict,
            "publishable": bool(
                self.target_type == "product"
                and self.status == "APPROVED"
                and self.publish_mapping_verified
                and self.istore_product_id
                and self.target_id == self.istore_product_id
                and not self.mapping_conflict
            ),
            "field_path": self.field_path,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "seo_reason": self.seo_reason,
            "risk_level": self.risk_level,
            "source_audit_id": self.source_audit_id,
            "issue_type": self.issue_type,
            "priority_score": self.priority_score or 0.0,
            "status": self.status,
            "before_snapshot": _json_load(self.before_snapshot_json),
            "proposed_payload": _json_load(self.proposed_payload_json),
            "rollback_payload": _json_load(self.rollback_payload_json),
            "publish_response": _json_load(self.publish_response_json),
            "publish_log": _json_load(self.publish_log_json),
            "publish_timestamp": self.publish_timestamp.isoformat() if self.publish_timestamp else None,
            "approved_by": self.approved_by,
            "approval_action": self.approval_action,
            "approval_metadata": _json_load(self.approval_metadata_json),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
