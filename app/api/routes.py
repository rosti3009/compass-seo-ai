import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import CrawlRun, PageAudit, SEOTask
from app.integrations.ga4 import GA4Client
from app.integrations.ga4 import MissingGoogleCredentialsError as MissingGA4CredentialsError
from app.integrations.gsc import GSCClient
from app.integrations.gsc import MissingGoogleCredentialsError as MissingGSCCredentialsError
from app.integrations.openai_client import OpenAIClient
from app.services.crawler import SEOCrawler
from app.services.sitemap import discover_sitemap_urls

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
DatabaseSession = Annotated[Session, Depends(get_db)]


def _seo_task_candidate(page: PageAudit) -> bool:
    missing_fields = {field.strip() for field in page.missing_fields.split(",") if field.strip()}
    missing_seo_basics = bool(missing_fields.intersection({"title", "meta_description", "h1"}))
    return page.seo_score < 70 or missing_seo_basics


def _priority_for_page(page: PageAudit) -> str:
    missing_fields = {field.strip() for field in page.missing_fields.split(",") if field.strip()}
    if page.seo_score < 50 or missing_fields.intersection({"title", "h1"}):
        return "high"
    if page.seo_score < 70 or "meta_description" in missing_fields:
        return "medium"
    return "low"


def _task_recommendation_payload(task: SEOTask) -> dict[str, object]:
    """Build a compact page/task payload for OpenAI recommendation generation."""
    try:
        existing_recommendation = json.loads(task.recommendation_json or "{}")
    except json.JSONDecodeError:
        existing_recommendation = task.recommendation_json

    return {
        "task_id": task.id,
        "page_url": task.page_url,
        "keyword": task.keyword,
        "priority": task.priority,
        "status": task.status,
        "suggested_title": task.suggested_title,
        "suggested_h1": task.suggested_h1,
        "meta_description": task.meta_description,
        "existing_recommendation": existing_recommendation,
    }


def _apply_recommendation_to_task(task: SEOTask, recommendation: dict[str, object]) -> None:
    """Persist generated recommendation fields on a task."""
    task.recommendation_json = json.dumps(recommendation, ensure_ascii=False)
    for field_name in ("suggested_title", "suggested_h1", "meta_description"):
        value = recommendation.get(field_name)
        if isinstance(value, str) and value:
            setattr(task, field_name, value)
    task.status = "recommended"


def _build_task_from_page(page: PageAudit) -> SEOTask:
    missing_fields = [field for field in page.missing_fields.split(",") if field]
    recommendation = {
        "source": "latest_crawl",
        "page_audit_id": page.id,
        "seo_score": page.seo_score,
        "missing_fields": missing_fields,
        "recommendations": [f"Add or improve {field.replace('_', ' ')}." for field in missing_fields]
        or ["Improve on-page SEO signals for this low-scoring page."],
    }
    return SEOTask(
        page_url=page.url,
        keyword=None,
        priority=_priority_for_page(page),
        status="open",
        suggested_title=page.title or None,
        suggested_h1=page.h1 or None,
        meta_description=page.meta_description or None,
        recommendation_json=json.dumps(recommendation),
    )


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


@router.post("/seo/tasks/from-latest-crawl", status_code=status.HTTP_201_CREATED)
def create_seo_tasks_from_latest_crawl(db: DatabaseSession) -> dict[str, int]:
    """Create SEO tasks from the latest crawl for pages that need on-page SEO work."""
    crawl_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    if not crawl_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No crawler runs found")

    pages = (
        db.query(PageAudit)
        .filter(PageAudit.crawl_run_id == crawl_run.id)
        .order_by(PageAudit.seo_score.asc(), PageAudit.url.asc())
        .all()
    )
    candidates = [page for page in pages if _seo_task_candidate(page)]
    existing_urls = {
        page_url
        for (page_url,) in db.query(SEOTask.page_url)
        .filter(SEOTask.page_url.in_([page.url for page in candidates]))
        .all()
    }
    new_tasks = [_build_task_from_page(page) for page in candidates if page.url not in existing_urls]
    db.add_all(new_tasks)
    db.commit()

    return {"created_count": len(new_tasks), "total_candidates": len(candidates)}


@router.get("/seo/tasks")
def list_seo_tasks(db: DatabaseSession) -> dict[str, object]:
    """Return saved SEO tasks ordered by creation date."""
    tasks = db.query(SEOTask).order_by(SEOTask.created_at.desc(), SEOTask.id.desc()).all()
    return {"tasks": [task.to_dict() for task in tasks]}


@router.post("/seo/tasks/{task_id}/generate-recommendation")
def generate_seo_task_recommendation(task_id: int, db: DatabaseSession) -> dict[str, object]:
    """Generate and persist an OpenAI SEO recommendation for a saved SEO task."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")

    payload = _task_recommendation_payload(task)
    try:
        recommendation = OpenAIClient().generate_seo_recommendation(page=payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    _apply_recommendation_to_task(task, recommendation)
    db.add(task)
    db.commit()
    db.refresh(task)

    return {"success": True, "task_id": task.id, "recommendation": recommendation}


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


@router.get("/sitemap/discover")
async def sitemap_discover() -> dict:
    sitemap_urls = await discover_sitemap_urls("https://compassgrill.co.il/sitemap.xml")

    return {
        "total_urls": len(sitemap_urls),
        "urls": [
            {
                "url": item.url,
                "type": item.type,
            }
            for item in sitemap_urls[:100]
        ],
    }
