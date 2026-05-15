from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.database import Base


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

    pages: Mapped[list["PageAudit"]] = relationship(
        back_populates="crawl_run",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "target_domain": self.target_domain,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "pages_crawled": self.pages_crawled,
            "average_score": self.average_score,
            "status": self.status,
            "error_message": self.error_message,
        }


class PageAudit(Base):
    """SEO audit metrics for a crawled page."""

    __tablename__ = "page_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    crawl_run_id: Mapped[int] = mapped_column(
        ForeignKey("crawl_runs.id"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    h1: Mapped[str | None] = mapped_column(String(512), nullable=True)
    canonical: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    internal_links: Mapped[int] = mapped_column(Integer, default=0)
    missing_fields: Mapped[str] = mapped_column(String(512), default="")
    seo_score: Mapped[float] = mapped_column(Float, default=0.0)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    crawl_run: Mapped[CrawlRun] = relationship(back_populates="pages")

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
            "seo_score": self.seo_score,
            "crawled_at": self.crawled_at.isoformat() if self.crawled_at else None,
        }


class SEOTask(Base):
    """AI-generated SEO task for tracking optimization work."""

    __tablename__ = "seo_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    page_url: Mapped[str] = mapped_column(Text, nullable=False)
    keyword: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False, default="seo_optimization")
    priority: Mapped[str] = mapped_column(String(50), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    current_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_h1: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)

    article_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    faq_schema: Mapped[str | None] = mapped_column(Text, nullable=True)
    article_schema: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_prompts: Mapped[str | None] = mapped_column(Text, nullable=True)
    cta_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    clicks_before: Mapped[float] = mapped_column(Float, default=0.0)
    impressions_before: Mapped[float] = mapped_column(Float, default=0.0)
    ctr_before: Mapped[float] = mapped_column(Float, default=0.0)
    position_before: Mapped[float] = mapped_column(Float, default=0.0)

    clicks_after: Mapped[float] = mapped_column(Float, default=0.0)
    impressions_after: Mapped[float] = mapped_column(Float, default=0.0)
    ctr_after: Mapped[float] = mapped_column(Float, default=0.0)
    position_after: Mapped[float] = mapped_column(Float, default=0.0)

    completed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_url": self.page_url,
            "keyword": self.keyword,
            "task_type": self.task_type,
            "priority": self.priority,
            "status": self.status,
            "current_title": self.current_title,
            "suggested_title": self.suggested_title,
            "current_meta": self.current_meta,
            "suggested_meta": self.suggested_meta,
            "suggested_h1": self.suggested_h1,
            "ai_recommendation": self.ai_recommendation,
            "article_html": self.article_html,
            "faq_schema": self.faq_schema,
            "article_schema": self.article_schema,
            "image_prompts": self.image_prompts,
            "cta_text": self.cta_text,
            "clicks_before": self.clicks_before,
            "impressions_before": self.impressions_before,
            "ctr_before": self.ctr_before,
            "position_before": self.position_before,
            "clicks_after": self.clicks_after,
            "impressions_after": self.impressions_after,
            "ctr_after": self.ctr_after,
            "position_after": self.position_after,
            "completed": self.completed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }