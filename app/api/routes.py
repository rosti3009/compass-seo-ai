import json
import re
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import CrawlRun, PageAudit, PublishingPackage, SEOFix, SEOTask
from app.integrations.ga4 import GA4Client
from app.integrations.ga4 import MissingGoogleCredentialsError as MissingGA4CredentialsError
from app.integrations.gsc import GSCClient
from app.integrations.gsc import MissingGoogleCredentialsError as MissingGSCCredentialsError
from app.integrations.openai_client import OpenAIClient
from app.services.crawler import SEOCrawler
from app.services.internal_links import authority_score, best_anchor_text, opportunity_score
from app.services.sitemap import discover_sitemap_urls
from app.services.topical_clusters import build_cluster_summary

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
DatabaseSession = Annotated[Session, Depends(get_db)]

SEO_FIX_TYPES = {
    "meta_title",
    "meta_description",
    "h1",
    "article_html",
    "faq_schema",
    "article_schema",
    "internal_links",
    "noindex_recommendation",
}
SEO_FIX_STATUSES = {"draft", "ready_for_review", "approved", "rejected", "exported", "applied_manually"}
PUBLISHING_PACKAGE_STATUSES = {"draft", "ready", "exported", "applied_manually", "failed"}


def _payload_field_for_fix_type(fix_type: str) -> str:
    """Return the ISTORE payload field that should receive the approved fix value."""
    return {
        "meta_title": "meta_title",
        "meta_description": "meta_description",
        "h1": "h1",
        "article_html": "body_html",
        "faq_schema": "faq_schema_json",
        "article_schema": "article_schema_json",
        "internal_links": "internal_links",
        "noindex_recommendation": "robots_directive",
    }.get(fix_type, "manual_update")


def _publishing_package_payload(fix: SEOFix, cms_type: str = "istore") -> dict[str, object]:
    """Build the copyable manual CMS payload for an approved SEO fix."""
    payload_field = _payload_field_for_fix_type(fix.fix_type)
    return {
        "cms_type": cms_type,
        "page_url": fix.page_url,
        "fix_id": fix.id,
        "task_id": fix.task_id,
        "fix_type": fix.fix_type,
        "target_field": payload_field,
        "current_value": fix.current_value or "",
        "proposed_value": fix.proposed_value or "",
        "istore_fields": {payload_field: fix.proposed_value or ""},
        "manual_instructions": _fix_publishing_instructions(fix),
        "safety": {
            "auto_publish": False,
            "requires_manual_istore_application": True,
        },
    }


def _publishing_packages_by_status(packages: list[PublishingPackage]) -> dict[str, list[PublishingPackage]]:
    """Group publishing packages for the manual publishing dashboard."""
    return {
        "ready": [package for package in packages if package.status == "ready"],
        "exported": [package for package in packages if package.status == "exported"],
        "applied": [package for package in packages if package.status == "applied_manually"],
    }


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

    meta_title = article.get("meta_title") or article.get("article_title")
    meta_description = article.get("meta_description")
    article_title = article.get("article_title")
    if isinstance(meta_title, str) and meta_title:
        task.suggested_title = meta_title
    if isinstance(meta_description, str) and meta_description:
        task.meta_description = meta_description
    if isinstance(article_title, str) and article_title:
        task.suggested_h1 = article_title

    task.article_status = "generated"


def _json_object_from_text(value: str | None) -> dict[str, object]:
    """Parse a stored JSON object string, falling back to an empty object for invalid values."""
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _slugify(value: str) -> str:
    """Return a CMS-friendly slug suggestion for generated article exports."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "seo-article"


def _task_has_generated_article(task: SEOTask) -> bool:
    """Return whether a task has generated article content available to export."""
    return task.article_status == "generated" and bool((task.article_html or "").strip())


def _latest_page_audit_for_url(db: Session, page_url: str) -> PageAudit | None:
    """Return the newest audit row for a page URL when crawl context exists."""
    return (
        db.query(PageAudit)
        .filter(PageAudit.url == page_url)
        .order_by(PageAudit.crawled_at.desc(), PageAudit.id.desc())
        .first()
    )


def _stored_json_fix_value(value: str | None) -> str:
    """Normalize stored JSON strings for readable fix proposals."""
    payload = _json_object_from_text(value)
    return _pretty_json(payload) if payload else ""


def _seo_fix_candidate_specs(task: SEOTask, current_page: PageAudit | None) -> list[dict[str, object]]:
    """Build SEO fix specs from recommendation fields and generated article assets."""
    current_title = current_page.title if current_page else None
    current_description = current_page.meta_description if current_page else None
    current_h1 = current_page.h1 if current_page else None
    recommendation = _parse_task_recommendation(task)
    has_generated_article = _task_has_generated_article(task)
    confidence_score = float(recommendation.get("confidence_score", 0.8) or 0.8) if recommendation else 0.9
    primary_source = "generated_article" if has_generated_article else "recommendation"
    specs = [
        {
            "fix_type": "meta_title",
            "current_value": current_title,
            "proposed_value": task.suggested_title,
            "source": primary_source,
        },
        {
            "fix_type": "meta_description",
            "current_value": current_description,
            "proposed_value": task.meta_description,
            "source": primary_source,
        },
        {
            "fix_type": "h1",
            "current_value": current_h1,
            "proposed_value": task.suggested_h1,
            "source": primary_source,
        },
    ]
    if has_generated_article:
        specs.extend(
            [
                {
                    "fix_type": "article_html",
                    "current_value": None,
                    "proposed_value": task.article_html,
                    "source": "generated_article",
                },
                {
                    "fix_type": "faq_schema",
                    "current_value": None,
                    "proposed_value": _stored_json_fix_value(task.faq_schema_json),
                    "source": "generated_article",
                },
                {
                    "fix_type": "article_schema",
                    "current_value": None,
                    "proposed_value": _stored_json_fix_value(task.article_schema_json),
                    "source": "generated_article",
                },
            ]
        )
    return [
        {**spec, "confidence_score": confidence_score}
        for spec in specs
        if isinstance(spec.get("proposed_value"), str) and str(spec["proposed_value"]).strip()
    ]


def _fix_publishing_instructions(fix: SEOFix) -> list[str]:
    """Return copy-friendly manual publishing instructions for a fix package."""
    instructions_by_type = {
        "meta_title": ["Update the page meta title/title tag with proposed_value."],
        "meta_description": ["Update the page meta description with proposed_value."],
        "h1": ["Update the page H1 heading with proposed_value."],
        "article_html": ["Paste proposed_value into the CMS body/editor after human review."],
        "faq_schema": ["Add proposed_value as FAQPage JSON-LD in the page schema area."],
        "article_schema": ["Add proposed_value as Article JSON-LD in the page schema area."],
        "internal_links": ["Manually add the proposed internal links in the CMS."],
        "noindex_recommendation": ["Review the recommendation before changing robots/noindex settings."],
    }
    return [
        "Do not auto-publish; apply this fix manually only after approval.",
        *instructions_by_type.get(fix.fix_type, ["Review proposed_value and apply manually if appropriate."]),
    ]


def _fixes_by_status(fixes: list[SEOFix]) -> dict[str, list[SEOFix]]:
    """Group fixes for the review dashboard."""
    return {
        "pending": [fix for fix in fixes if fix.status in {"draft", "ready_for_review"}],
        "approved": [fix for fix in fixes if fix.status == "approved"],
        "rejected": [fix for fix in fixes if fix.status == "rejected"],
    }


def _task_export_payload(task: SEOTask) -> dict[str, object]:
    """Build a CMS-copyable export payload for a generated SEO article."""
    meta_title = task.suggested_title or task.suggested_h1 or ""
    h1 = task.suggested_h1 or task.suggested_title or ""
    slug_source = h1 or meta_title or task.page_url.rsplit("/", maxsplit=1)[-1]
    publishing_notes = [
        "Copy article_html into the CMS body field.",
        "Add faq_schema_json and article_schema_json to the page schema/script area.",
    ]

    return {
        "success": True,
        "task_id": task.id,
        "page_url": task.page_url,
        "meta_title": meta_title,
        "meta_description": task.meta_description or "",
        "h1": h1,
        "article_html": task.article_html or "",
        "faq_schema_json": _json_object_from_text(task.faq_schema_json),
        "article_schema_json": _json_object_from_text(task.article_schema_json),
        "slug_suggestion": _slugify(slug_source),
        "publishing_notes": publishing_notes,
    }


def _pretty_json(payload: object) -> str:
    """Render JSON for human-friendly copy sections."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _task_export_view_html(export: dict[str, object]) -> str:
    """Render a standalone CMS export view for a generated article."""
    faq_schema = _pretty_json(export["faq_schema_json"])
    article_schema = _pretty_json(export["article_schema_json"])
    notes = "".join(f"<li>{escape(str(note))}</li>" for note in export["publishing_notes"])

    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>SEO Article Export: {escape(str(export['meta_title'] or export['task_id']))}</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;line-height:1.5;margin:2rem;max-width:1100px}"
        "section{border:1px solid #ddd;border-radius:8px;margin:1rem 0;padding:1rem}"
        "textarea{box-sizing:border-box;font-family:monospace;min-height:8rem;width:100%}"
        "pre{background:#f7f7f7;overflow:auto;padding:1rem;white-space:pre-wrap}"
        "</style>"
        "</head>"
        "<body>"
        "<main>"
        f"<h1>Export Article for Task {escape(str(export['task_id']))}</h1>"
        f"<p><strong>Page URL:</strong> {escape(str(export['page_url']))}</p>"
        f"<p><strong>Slug suggestion:</strong> <code>{escape(str(export['slug_suggestion']))}</code></p>"
        "<section>"
        "<h2>Meta title</h2>"
        f"<textarea readonly>{escape(str(export['meta_title']))}</textarea>"
        "</section>"
        "<section>"
        "<h2>Meta description</h2>"
        f"<textarea readonly>{escape(str(export['meta_description']))}</textarea>"
        "</section>"
        "<section>"
        "<h2>H1</h2>"
        f"<textarea readonly>{escape(str(export['h1']))}</textarea>"
        "</section>"
        "<section>"
        "<h2>Article HTML</h2>"
        f"<textarea readonly>{escape(str(export['article_html']))}</textarea>"
        "<h3>Rendered preview</h3>"
        f"<article>{export['article_html']}</article>"
        "</section>"
        "<section>"
        "<h2>FAQ schema</h2>"
        f"<textarea readonly>{escape(faq_schema)}</textarea>"
        f"<pre>{escape(faq_schema)}</pre>"
        "</section>"
        "<section>"
        "<h2>Article schema</h2>"
        f"<textarea readonly>{escape(article_schema)}</textarea>"
        f"<pre>{escape(article_schema)}</pre>"
        "</section>"
        "<section>"
        "<h2>Publishing notes</h2>"
        f"<ul>{notes}</ul>"
        "</section>"
        "</main>"
        "</body>"
        "</html>"
    )


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


def _infer_page_type(page: PageAudit) -> str:
    """Infer a compact page type from the URL and content shape."""
    path = page.url.split("?", maxsplit=1)[0].rstrip("/").rsplit("/", maxsplit=1)[-1].lower()
    segments = [segment for segment in page.url.split("?", maxsplit=1)[0].split("/")[3:] if segment]
    if not segments:
        return "homepage"
    if any(segment in {"blog", "articles", "guides", "guide"} for segment in segments):
        return "guide"
    if any(segment in {"services", "service"} for segment in segments):
        return "service"
    if any(segment in {"products", "product", "shop", "collections"} for segment in segments):
        return "product"
    if path in {"category", "tag"} or len(segments) == 1:
        return "category"
    return "page"


def _page_cluster_payload(page: PageAudit, task: SEOTask | None = None) -> dict[str, object]:
    """Build page, crawl, task, and article fields used by topical cluster planning."""
    return {
        "url": page.url,
        "title": page.title or "",
        "h1": page.h1 or "",
        "meta": page.meta_description or "",
        "word_count": page.word_count,
        "seo_score": page.seo_score,
        "page_type": _infer_page_type(page),
        "task_status": task.status if task else "none",
        "article_status": task.article_status if task else "not_generated",
        "keyword": task.keyword if task else None,
        "priority": task.priority if task else None,
    }


def _valid_topical_clusters(payload: dict[str, object]) -> bool:
    """Return whether a topical cluster payload matches the public response shape."""
    clusters = payload.get("clusters")
    if not isinstance(clusters, list):
        return False
    required_keys = {"cluster_name", "pillar_page", "supporting_pages", "missing_articles", "internal_link_strategy"}
    for cluster in clusters:
        if not isinstance(cluster, dict) or not required_keys.issubset(cluster):
            return False
        if not isinstance(cluster.get("supporting_pages"), list):
            return False
        if not isinstance(cluster.get("missing_articles"), list):
            return False
        if not isinstance(cluster.get("internal_link_strategy"), list):
            return False
    return True


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


def _dashboard_metrics(db: Session, latest_pages: list[PageAudit]) -> dict[str, int]:
    """Return SEO workflow counts for dashboard cards."""
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in latest_pages])
    internal_link_opportunities_count = len(_build_internal_link_opportunities(latest_pages, tasks_by_url))
    page_payloads = [_page_cluster_payload(page, tasks_by_url.get(page.url)) for page in latest_pages]
    return {
        "total_tasks": db.query(SEOTask).count(),
        "recommended_tasks": db.query(SEOTask).filter(SEOTask.status == "recommended").count(),
        "generated_articles": db.query(SEOTask).filter(SEOTask.article_status == "generated").count(),
        "internal_link_opportunities": internal_link_opportunities_count,
        "topical_clusters": len(build_cluster_summary(page_payloads)),
    }


def _latest_crawl_context(db: Session, limit: int | None = None) -> tuple[CrawlRun | None, list[PageAudit]]:
    """Return latest crawl run and ordered pages, optionally limited for dashboard tables."""
    latest_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    if not latest_run:
        return None, []

    query = (
        db.query(PageAudit)
        .filter(PageAudit.crawl_run_id == latest_run.id)
        .order_by(PageAudit.seo_score.asc(), PageAudit.url.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return latest_run, query.all()


@router.get("/health")
def health() -> dict[str, str]:
    """Return a lightweight health check response."""
    return {"status": "ok", "service": settings.app_name}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the SEO dashboard."""
    latest_run, latest_pages = _latest_crawl_context(db, limit=25)
    metrics_pages = _latest_crawl_pages(db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "target_domain": settings.target_domain,
            "latest_run": latest_run,
            "pages": latest_pages,
            "metrics": _dashboard_metrics(db, metrics_pages),
        },
    )


@router.get("/seo/tasks-view", response_class=HTMLResponse)
def seo_tasks_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render saved SEO tasks for human review."""
    tasks = db.query(SEOTask).order_by(SEOTask.created_at.desc(), SEOTask.id.desc()).all()
    return templates.TemplateResponse(request, "seo_tasks.html", {"tasks": tasks})


@router.get("/seo/fixes-view", response_class=HTMLResponse)
def seo_fixes_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render SEO fixes grouped by review status."""
    fixes = db.query(SEOFix).order_by(SEOFix.created_at.desc(), SEOFix.id.desc()).all()
    return templates.TemplateResponse(
        request,
        "seo_fixes.html",
        {"fix_groups": _fixes_by_status(fixes), "fixes": fixes},
    )


@router.get("/seo/publishing-packages-view", response_class=HTMLResponse)
def publishing_packages_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render manual ISTORE publishing packages grouped by publishing status."""
    packages = (
        db.query(PublishingPackage).order_by(PublishingPackage.created_at.desc(), PublishingPackage.id.desc()).all()
    )
    fixes_by_id = (
        {fix.id: fix for fix in db.query(SEOFix).filter(SEOFix.id.in_([p.fix_id for p in packages])).all()}
        if packages
        else {}
    )
    return templates.TemplateResponse(
        request,
        "publishing_packages.html",
        {
            "package_groups": _publishing_packages_by_status(packages),
            "fixes_by_id": fixes_by_id,
        },
    )


@router.get("/seo/internal-link-opportunities-view", response_class=HTMLResponse)
def internal_link_opportunities_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render deterministic internal link opportunities from the latest crawl."""
    pages = _latest_crawl_pages(db)
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in pages])
    return templates.TemplateResponse(
        request,
        "internal_link_opportunities.html",
        {
            "opportunities": _build_internal_link_opportunities(pages, tasks_by_url),
            "pages_analyzed": len(pages),
        },
    )


@router.get("/seo/topical-clusters-view", response_class=HTMLResponse)
def topical_clusters_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render topical cluster summaries from the latest crawl."""
    pages = _latest_crawl_pages(db)
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in pages])
    page_payloads = [_page_cluster_payload(page, tasks_by_url.get(page.url)) for page in pages]
    return templates.TemplateResponse(
        request,
        "topical_clusters.html",
        {"clusters": build_cluster_summary(page_payloads), "pages_analyzed": len(page_payloads)},
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


@router.post("/seo/tasks/{task_id}/create-fixes", status_code=status.HTTP_201_CREATED)
def create_seo_fixes_for_task(task_id: int, db: DatabaseSession) -> dict[str, object]:
    """Create reviewable SEO fix packages from a task recommendation or generated article."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")
    if not _parse_task_recommendation(task) and not _task_has_generated_article(task):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SEO task recommendation or generated article is required",
        )

    existing_draft_types = {
        fix_type
        for (fix_type,) in db.query(SEOFix.fix_type).filter(SEOFix.task_id == task.id, SEOFix.status == "draft").all()
    }
    current_page = _latest_page_audit_for_url(db, task.page_url)
    candidates = _seo_fix_candidate_specs(task, current_page)
    new_fixes = [
        SEOFix(
            task_id=task.id,
            page_url=task.page_url,
            fix_type=str(candidate["fix_type"]),
            current_value=candidate.get("current_value"),
            proposed_value=str(candidate["proposed_value"]),
            status="draft",
            confidence_score=float(candidate["confidence_score"]),
            source=str(candidate["source"]),
            notes_json=json.dumps(
                {
                    "task_status": task.status,
                    "article_status": task.article_status,
                    "safe_pipeline": "manual_review_required",
                }
            ),
        )
        for candidate in candidates
        if candidate["fix_type"] not in existing_draft_types
    ]
    db.add_all(new_fixes)
    db.commit()
    for fix in new_fixes:
        db.refresh(fix)

    return {"created_count": len(new_fixes), "fixes": [fix.to_dict() for fix in new_fixes]}


@router.get("/seo/fixes")
def list_seo_fixes(
    db: DatabaseSession,
    status: str | None = None,
    fix_type: str | None = None,
    task_id: int | None = None,
) -> dict[str, object]:
    """Return SEO fixes with optional status, type, and task filters."""
    query = db.query(SEOFix)
    if status:
        query = query.filter(SEOFix.status == status)
    if fix_type:
        query = query.filter(SEOFix.fix_type == fix_type)
    if task_id is not None:
        query = query.filter(SEOFix.task_id == task_id)
    fixes = query.order_by(SEOFix.created_at.desc(), SEOFix.id.desc()).all()
    return {"fixes": [fix.to_dict() for fix in fixes]}


@router.post("/seo/fixes/{fix_id}/approve")
def approve_seo_fix(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Approve a reviewable SEO fix without publishing it."""
    fix = db.get(SEOFix, fix_id)
    if not fix:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO fix not found")
    fix.status = "approved"
    db.add(fix)
    db.commit()
    db.refresh(fix)
    return {"success": True, "fix": fix.to_dict()}


@router.post("/seo/fixes/{fix_id}/reject")
def reject_seo_fix(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Reject a reviewable SEO fix without publishing it."""
    fix = db.get(SEOFix, fix_id)
    if not fix:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO fix not found")
    fix.status = "rejected"
    db.add(fix)
    db.commit()
    db.refresh(fix)
    return {"success": True, "fix": fix.to_dict()}


@router.get("/seo/fixes/{fix_id}/export")
def export_seo_fix(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Return a copy-friendly manual publishing payload for one SEO fix."""
    fix = db.get(SEOFix, fix_id)
    if not fix:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO fix not found")
    return {
        "page_url": fix.page_url,
        "fix_type": fix.fix_type,
        "current_value": fix.current_value or "",
        "proposed_value": fix.proposed_value or "",
        "publishing_instructions": _fix_publishing_instructions(fix),
    }


@router.post("/seo/fixes/{fix_id}/create-publishing-package", status_code=status.HTTP_201_CREATED)
def create_publishing_package_for_fix(fix_id: int, response: Response, db: DatabaseSession) -> dict[str, object]:
    """Create a manual ISTORE publishing package from an approved SEO fix."""
    fix = db.get(SEOFix, fix_id)
    if not fix:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO fix not found")
    if fix.status != "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only approved SEO fixes can be packaged")

    existing_package = (
        db.query(PublishingPackage)
        .filter(PublishingPackage.fix_id == fix.id, PublishingPackage.status.in_(["draft", "ready"]))
        .order_by(PublishingPackage.created_at.desc(), PublishingPackage.id.desc())
        .first()
    )
    if existing_package:
        response.status_code = status.HTTP_200_OK
        return {"success": True, "duplicate": True, "package": existing_package.to_dict()}

    package = PublishingPackage(
        fix_id=fix.id,
        page_url=fix.page_url,
        cms_type="istore",
        payload_json=json.dumps(_publishing_package_payload(fix), ensure_ascii=False),
        status="ready",
        notes="Prepared for manual ISTORE/CMS publishing. Auto-publishing is disabled.",
    )
    db.add(package)
    db.commit()
    db.refresh(package)
    return {"success": True, "duplicate": False, "package": package.to_dict()}


@router.get("/seo/publishing-packages")
def list_publishing_packages(
    db: DatabaseSession,
    status: str | None = None,
    cms_type: str | None = None,
) -> dict[str, object]:
    """Return publishing packages with optional status and CMS filters."""
    query = db.query(PublishingPackage)
    if status:
        if status not in PUBLISHING_PACKAGE_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid publishing package status")
        query = query.filter(PublishingPackage.status == status)
    if cms_type:
        query = query.filter(PublishingPackage.cms_type == cms_type)
    packages = query.order_by(PublishingPackage.created_at.desc(), PublishingPackage.id.desc()).all()
    return {"packages": [package.to_dict() for package in packages]}


@router.get("/seo/publishing-packages/{package_id}/export")
def export_publishing_package(package_id: int, db: DatabaseSession) -> dict[str, object]:
    """Return a copy-friendly manual ISTORE application payload for one package."""
    package = db.get(PublishingPackage, package_id)
    if not package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Publishing package not found")

    package.status = "exported"
    db.add(package)
    db.commit()
    db.refresh(package)
    payload = package.to_dict()["payload_json"]
    return {
        "success": True,
        "package": package.to_dict(),
        "copy_payload": payload,
        "copy_payload_json": _pretty_json(payload),
        "instructions": [
            "Copy the payload into the matching ISTORE fields manually.",
            "Verify the rendered page before marking this package applied.",
        ],
    }


@router.post("/seo/publishing-packages/{package_id}/mark-applied")
def mark_publishing_package_applied(package_id: int, db: DatabaseSession) -> dict[str, object]:
    """Mark a publishing package as manually applied in ISTORE/CMS."""
    package = db.get(PublishingPackage, package_id)
    if not package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Publishing package not found")
    package.status = "applied_manually"
    db.add(package)
    db.commit()
    db.refresh(package)
    return {"success": True, "package": package.to_dict()}


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


@router.get("/seo/topical-clusters")
def topical_clusters(db: DatabaseSession) -> dict[str, object]:
    """Return topical SEO clusters from the latest crawl and saved SEO task context."""
    pages = _latest_crawl_pages(db)
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in pages])
    page_payloads = [_page_cluster_payload(page, tasks_by_url.get(page.url)) for page in pages]
    clusters = build_cluster_summary(page_payloads)

    if page_payloads and settings.openai_api_key:
        try:
            generated = OpenAIClient().generate_topical_clusters(pages=page_payloads)
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            generated = {}
        if _valid_topical_clusters(generated):
            clusters = generated["clusters"]

    return {"success": True, "total_pages_analyzed": len(page_payloads), "clusters": clusters}


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


@router.get("/seo/tasks/{task_id}/export")
def export_seo_task_article(task_id: int, db: DatabaseSession) -> dict[str, object]:
    """Return a CMS-copyable JSON export for a generated SEO task article."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")
    if not _task_has_generated_article(task):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generated article is required")

    return _task_export_payload(task)


@router.get("/seo/tasks/{task_id}/export-view", response_class=HTMLResponse)
def export_seo_task_article_view(task_id: int, db: DatabaseSession) -> HTMLResponse:
    """Return a copy-friendly HTML export view for a generated SEO task article."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")
    if not _task_has_generated_article(task):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generated article is required")

    return HTMLResponse(content=_task_export_view_html(_task_export_payload(task)))


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
