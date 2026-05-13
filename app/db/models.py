from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    pages: Mapped[list["PageAudit"]] = relationship(back_populates="crawl_run", cascade="all, delete-orphan")

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "page_url": self.page_url,
            "keyword": self.keyword,
            "priority": self.priority,
            "status": self.status,
            "suggested_title": self.suggested_title,
            "suggested_h1": self.suggested_h1,
            "meta_description": self.meta_description,
            "recommendation_json": self.recommendation_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
