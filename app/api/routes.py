from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import CrawlRun, PageAudit
from app.integrations.ga4 import GA4Client
from app.integrations.ga4 import MissingGoogleCredentialsError as MissingGA4CredentialsError
from app.integrations.gsc import GSCClient
from app.integrations.gsc import MissingGoogleCredentialsError as MissingGSCCredentialsError
from app.services.crawler import SEOCrawler

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.get("/health")
def health() -> dict[str, str]:
    """Return a lightweight health check response."""
    return {"status": "ok", "service": settings.app_name}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the SEO dashboard."""
    latest_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    latest_pages = []
    if latest_run:
        latest_pages = (
            db.query(PageAudit)
            .filter(PageAudit.crawl_run_id == latest_run.id)
            .order_by(PageAudit.seo_score.asc(), PageAudit.url.asc())
            .limit(25)
            .all()
        )
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "target_domain": settings.target_domain, "latest_run": latest_run, "pages": latest_pages},
    )


@router.post("/crawler/run", status_code=status.HTTP_201_CREATED)
def run_crawler(db: DatabaseSession) -> dict[str, object]:
    """Run a bounded crawl and persist page audit results."""
    crawler = SEOCrawler(settings.target_domain, max_pages=settings.crawler_max_pages)
    crawl_run, pages = crawler.run(db)
    return {
        "crawl_run_id": crawl_run.id,
        "target_domain": crawl_run.target_domain,
        "pages_crawled": crawl_run.pages_crawled,
        "average_score": crawl_run.average_score,
        "results": [page.to_dict() for page in pages],
    }


@router.get("/crawler/latest")
def latest_crawler_results(db: DatabaseSession) -> dict[str, object]:
    """Return the most recent crawl run and its page audit results."""
    crawl_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    if not crawl_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No crawler runs found")
    pages = (
        db.query(PageAudit)
        .filter(PageAudit.crawl_run_id == crawl_run.id)
        .order_by(PageAudit.seo_score.asc(), PageAudit.url.asc())
        .all()
    )
    return {"crawl_run": crawl_run.to_dict(), "results": [page.to_dict() for page in pages]}


@router.get("/stats")
def stats(db: DatabaseSession) -> dict[str, object]:
    """Return aggregate SEO crawl statistics."""
    total_runs = db.query(CrawlRun).count()
    total_pages = db.query(PageAudit).count()
    latest_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    return {
        "target_domain": settings.target_domain,
        "total_runs": total_runs,
        "total_pages_audited": total_pages,
        "latest_run": latest_run.to_dict() if latest_run else None,
    }


@router.get("/integrations/gsc/status")
def gsc_status() -> dict[str, object]:
    """Validate Google Search Console credentials configuration."""
    try:
        return GSCClient.from_settings().status()
    except MissingGSCCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/integrations/ga4/status")
def ga4_status() -> dict[str, object]:
    """Validate GA4 credentials configuration."""
    try:
        return GA4Client.from_settings().status()
    except MissingGA4CredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
