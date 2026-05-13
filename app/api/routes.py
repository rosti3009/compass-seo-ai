import json
from html import escape
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
from app.services.internal_links import authority_score, best_anchor_text, opportunity_score
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


def _parse_task_recommendation(task: SEOTask) -> dict[str, object]:
    """Return the saved recommendation payload for a task, or an empty object when absent/invalid."""
    if not task.recommendation_json:
        return {}
    try:
        recommendation = json.loads(task.recommendation_json)
    except json.JSONDecodeError:
        return {}
    return recommendation if isinstance(recommendation, dict) else {}


def _task_article_payload(task: SEOTask) -> dict[str, object]:
    """Build the complete payload used for full article generation."""
    return {
        "task_id": task.id,
        "page_url": task.page_url,
        "keyword": task.keyword,
        "priority": task.priority,
        "status": task.status,
        "suggested_title": task.suggested_title,
        "suggested_h1": task.suggested_h1,
        "meta_description": task.meta_description,
        "recommendation": _parse_task_recommendation(task),
    }


def _apply_article_to_task(task: SEOTask, article: dict[str, object]) -> None:
    """Persist generated article content and schema payloads on a task."""
    article_html = article.get("article_html")
    task.article_html = article_html if isinstance(article_html, str) else ""
    task.article_schema_json = json.dumps(article.get("article_schema_json") or {}, ensure_ascii=False)
    task.faq_schema_json = json.dumps(article.get("faq_schema_json") or {}, ensure_ascii=False)
    task.article_status = "generated"


def _task_article_preview_html(task: SEOTask) -> str:
    """Render a simple standalone HTML article preview."""
    title = task.suggested_title or task.suggested_h1 or f"SEO Task {task.id} Article Preview"
    article_html = task.article_html or "<p>No article has been generated for this SEO task yet.</p>"
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "</head>"
        "<body>"
        f"<main>{article_html}</main>"
        "</body>"
        "</html>"
    )


def _latest_crawl_pages(db: Session) -> list[PageAudit]:
    """Return all pages from the latest crawl run, or an empty list when no crawl exists."""
    crawl_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    if not crawl_run:
        return []
    return (
        db.query(PageAudit)
        .filter(PageAudit.crawl_run_id == crawl_run.id)
        .order_by(PageAudit.seo_score.desc(), PageAudit.url.asc())
        .all()
    )


def _tasks_by_page_url(db: Session, page_urls: list[str]) -> dict[str, SEOTask]:
    """Return SEO tasks keyed by page URL for the supplied crawl URLs."""
    if not page_urls:
        return {}
    return {task.page_url: task for task in db.query(SEOTask).filter(SEOTask.page_url.in_(page_urls)).all()}


def _page_link_payload(page: PageAudit, task: SEOTask | None = None) -> dict[str, object]:
    """Build the compact payload used to score and enrich internal link opportunities."""
    page_payload = page.to_dict()
    page_payload["authority_score"] = authority_score(page)
    page_payload["opportunity_score"] = opportunity_score(page, task)
    page_payload["article_generated"] = task.article_status == "generated" if task else False
    if task:
        page_payload["task"] = {
            "id": task.id,
            "keyword": task.keyword,
            "priority": task.priority,
            "status": task.status,
            "article_status": task.article_status,
            "suggested_title": task.suggested_title,
            "suggested_h1": task.suggested_h1,
        }
    return page_payload


def _build_internal_link_opportunities(
    pages: list[PageAudit], tasks_by_url: dict[str, SEOTask]
) -> list[dict[str, object]]:
    """Pair strong source pages with weak target pages and produce deterministic suggestions."""
    scored_pages = [
        {
            "page": page,
            "task": tasks_by_url.get(page.url),
            "authority_score": authority_score(page),
            "opportunity_score": opportunity_score(page, tasks_by_url.get(page.url)),
        }
        for page in pages
        if page.status_code < 400
    ]
    strong_pages = sorted(
        [item for item in scored_pages if item["authority_score"] >= 60],
        key=lambda item: (-item["authority_score"], item["page"].url),
    )
    weak_pages = sorted(
        [item for item in scored_pages if item["opportunity_score"] >= 45],
        key=lambda item: (-item["opportunity_score"], item["page"].url),
    )

    opportunities = []
    for target in weak_pages:
        for source in strong_pages:
            if source["page"].url == target["page"].url:
                continue
            anchor_text = best_anchor_text(target["page"], target["task"])
            opportunities.append(
                {
                    "source_url": source["page"].url,
                    "target_url": target["page"].url,
                    "anchor_text": anchor_text,
                    "reason": (
                        "High-authority source page can pass relevance to a weaker page that needs more internal "
                        "link support."
                    ),
                    "authority_score": source["authority_score"],
                    "opportunity_score": target["opportunity_score"],
                }
            )
            break
    return opportunities[:25]


def _merge_openai_internal_link_suggestions(
    opportunities: list[dict[str, object]], suggestions: dict[str, object]
) -> list[dict[str, object]]:
    """Overlay OpenAI-provided anchor text and reasons on deterministic opportunities."""
    suggested_items = suggestions.get("opportunities", [])
    if not isinstance(suggested_items, list):
        return opportunities
    by_pair = {
        (item.get("source_url"), item.get("target_url")): item for item in suggested_items if isinstance(item, dict)
    }
    merged = []
    for opportunity in opportunities:
        updated = opportunity.copy()
        suggestion = by_pair.get((opportunity["source_url"], opportunity["target_url"]))
        if suggestion:
            for field in ("anchor_text", "reason"):
                value = suggestion.get(field)
                if isinstance(value, str) and value.strip():
                    updated[field] = value.strip()
        merged.append(updated)
    return merged


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


@router.get("/seo/internal-link-opportunities")
def internal_link_opportunities(db: DatabaseSession) -> dict[str, object]:
    """Return internal link opportunities from the latest crawl and saved SEO task context."""
    pages = _latest_crawl_pages(db)
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in pages])
    strong_pages_count = sum(1 for page in pages if page.status_code < 400 and authority_score(page) >= 60)
    weak_pages_count = sum(
        1 for page in pages if page.status_code < 400 and opportunity_score(page, tasks_by_url.get(page.url)) >= 45
    )
    opportunities = _build_internal_link_opportunities(pages, tasks_by_url)

    if opportunities and settings.openai_api_key:
        opportunity_payloads = []
        pages_by_url = {page.url: page for page in pages}
        for opportunity in opportunities:
            source_page = pages_by_url.get(str(opportunity["source_url"]))
            target_page = pages_by_url.get(str(opportunity["target_url"]))
            opportunity_payload = opportunity.copy()
            if source_page:
                opportunity_payload["source_page"] = _page_link_payload(source_page, tasks_by_url.get(source_page.url))
            if target_page:
                opportunity_payload["target_page"] = _page_link_payload(target_page, tasks_by_url.get(target_page.url))
            opportunity_payloads.append(opportunity_payload)
        try:
            suggestions = OpenAIClient().generate_internal_link_suggestions(pages=opportunity_payloads)
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            suggestions = {}
        opportunities = _merge_openai_internal_link_suggestions(opportunities, suggestions)

    return {
        "success": True,
        "summary": {
            "strong_pages": strong_pages_count,
            "weak_pages": weak_pages_count,
            "link_opportunities": len(opportunities),
        },
        "opportunities": opportunities,
    }


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


@router.post("/seo/tasks/{task_id}/generate-article")
def generate_seo_task_article(task_id: int, db: DatabaseSession) -> dict[str, object]:
    """Generate and persist a complete SEO article for a recommended SEO task."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")

    if not _parse_task_recommendation(task):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SEO task recommendation is required")

    try:
        article = OpenAIClient().generate_full_article(task=_task_article_payload(task))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    _apply_article_to_task(task, article)
    db.add(task)
    db.commit()
    db.refresh(task)

    return {"success": True, "task_id": task.id, "article": article}


@router.get("/seo/tasks/{task_id}/preview", response_class=HTMLResponse)
def preview_seo_task_article(task_id: int, db: DatabaseSession) -> HTMLResponse:
    """Return a simple HTML preview for a generated SEO task article."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")
    return HTMLResponse(content=_task_article_preview_html(task))


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
