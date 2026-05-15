
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
from app.integrations.gsc import GSCClient
from app.integrations.openai_client import OpenAIClient
from app.services.crawler import SEOCrawler
from app.services.image_generator import ImageGenerator
from app.services.page_analyzer import PageAnalyzer
from app.services.sitemap import discover_sitemap_urls

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
        {
            "request": request,
            "target_domain": settings.target_domain,
            "latest_run": latest_run,
            "pages": latest_pages,
        },
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
        "status": crawl_run.status,
        "error_message": crawl_run.error_message,
        "results": [page.to_dict() for page in pages],
    }


@router.get("/crawler/latest")
def latest_crawler_results(db: DatabaseSession) -> dict[str, object]:
    """Return the most recent crawl run and its page audit results."""
    crawl_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()

    if not crawl_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No crawler runs found",
        )

    pages = (
        db.query(PageAudit)
        .filter(PageAudit.crawl_run_id == crawl_run.id)
        .order_by(PageAudit.seo_score.asc(), PageAudit.url.asc())
        .all()
    )

    return {
        "crawl_run": crawl_run.to_dict(),
        "results": [page.to_dict() for page in pages],
    }


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
    """Validate Google Search Console OAuth connection."""
    try:
        return GSCClient.from_settings().status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/integrations/gsc/performance")
def gsc_performance() -> dict[str, object]:
    """Return Search Console performance data."""
    try:
        return GSCClient.from_settings().search_performance()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/integrations/gsc/opportunities")
def gsc_opportunities() -> dict[str, object]:
    """Return automatic SEO opportunities from Search Console."""
    try:
        return GSCClient.from_settings().seo_opportunities()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/integrations/gsc/recommendations")
def gsc_recommendations() -> dict[str, object]:
    """Return SEO recommendations based on Search Console opportunities."""
    try:
        return GSCClient.from_settings().seo_recommendations()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/integrations/ga4/status")
def ga4_status() -> dict[str, object]:
    """Validate GA4 OAuth connection."""
    try:
        return GA4Client.from_settings().status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/sitemap/discover")
async def sitemap_discover() -> dict[str, object]:
    """Discover URLs from the Compass sitemap."""
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

@router.get("/ai/seo-rewrite")
def ai_seo_rewrite(query: str, page_url: str) -> dict[str, object]:
    """Generate AI SEO rewrite for a keyword and page."""
    try:
        return OpenAIClient().generate_seo_rewrite(
            query=query,
            page_url=page_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.get("/seo/analyze-page")
def analyze_page(url: str) -> dict[str, object]:
    """Analyze a single page and return SEO issues."""
    try:
        return PageAnalyzer().analyze(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.get("/integrations/gsc/seo-plan")
def gsc_seo_plan() -> dict[str, object]:
    """Generate AI SEO plan from Search Console opportunities."""
    try:
        gsc_data = GSCClient.from_settings().search_performance()

        rows = gsc_data.get("rows", [])

        recommendations = []

        for row in rows[:15]:
            keys = row.get("keys", [])

            if len(keys) < 2:
                continue

            recommendations.append(
                {
                    "query": keys[0],
                    "page": keys[1],
                    "clicks": row.get("clicks", 0),
                    "impressions": row.get("impressions", 0),
                    "ctr": row.get("ctr", 0),
                    "position": row.get("position", 0),
                }
            )

        return OpenAIClient().generate_seo_plan(
            recommendations=recommendations,
        )

    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.post("/integrations/gsc/create-seo-tasks")
def create_seo_tasks(
    db: DatabaseSession,
) -> dict[str, object]:
    """Generate and save SEO tasks from GSC AI recommendations."""

    try:
        gsc_data = GSCClient.from_settings().search_performance()

        rows = gsc_data.get("rows", [])

        recommendations = []

        for row in rows[:10]:
            keys = row.get("keys", [])

            if len(keys) < 2:
                continue

            recommendations.append(
                {
                    "query": keys[0],
                    "page": keys[1],
                    "clicks": row.get("clicks", 0),
                    "impressions": row.get("impressions", 0),
                    "ctr": row.get("ctr", 0),
                    "position": row.get("position", 0),
                }
            )

        ai_result = OpenAIClient().generate_seo_plan(
            recommendations=recommendations,
        )

        seo_plan = ai_result.get("seo_plan", {})
        pages = seo_plan.get("pages", [])

        created_tasks = []

        for page in pages:
            task = SEOTask(
                page_url=page.get("page_url", ""),
                keyword=page.get("main_keyword", ""),
                task_type="seo_optimization",
                priority=page.get("priority", "medium"),
                status="pending",
                suggested_title=page.get("recommended_title", ""),
                suggested_meta=page.get(
                    "recommended_meta_description",
                    "",
                ),
                suggested_h1=page.get("recommended_h1", ""),
                ai_recommendation=str(page),
            )

            db.add(task)

            created_tasks.append(
                {
                    "keyword": task.keyword,
                    "page_url": task.page_url,
                    "priority": task.priority,
                }
            )

        db.commit()

        return {
            "success": True,
            "tasks_created": len(created_tasks),
            "tasks": created_tasks,
        }

    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.get("/seo/tasks")
def list_seo_tasks(db: DatabaseSession) -> dict[str, object]:
    """Return saved SEO tasks."""
    tasks = db.query(SEOTask).order_by(SEOTask.created_at.desc()).all()

    return {
        "total_tasks": len(tasks),
        "tasks": [task.to_dict() for task in tasks],
    }

@router.post("/seo/tasks/{task_id}/generate-article")
def generate_article_from_task(
    task_id: int,
    db: DatabaseSession,
) -> dict[str, object]:
    """Generate and save a full SEO article from a saved SEO task."""

    task = db.query(SEOTask).filter(SEOTask.id == task_id).first()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SEO task not found",
        )

    try:
        article = OpenAIClient().generate_full_article(
            keyword=task.keyword,
            page_url=task.page_url,
        )

        task.article_html = article.get("html_article")
        task.faq_schema = article.get("faq_schema")
        task.article_schema = article.get("article_schema")
        task.image_prompts = json.dumps(
            article.get("image_prompts", []),
            ensure_ascii=False,
        )
        task.cta_text = article.get("cta")

        task.status = "article_generated"

        db.commit()
        db.refresh(task)

        return {
            "success": True,
            "task": task.to_dict(),
            "article": article,
        }

    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.get("/seo/tasks/{task_id}/preview", response_class=HTMLResponse)
def preview_seo_article(
    task_id: int,
    db: DatabaseSession,
) -> HTMLResponse:
    """Render saved SEO article visually."""
    task = db.query(SEOTask).filter(SEOTask.id == task_id).first()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SEO task not found",
        )

    if not task.article_html:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Article was not generated for this task yet",
        )

    html = f"""
<!doctype html>
<html lang="he" dir="rtl">
<head>
    <meta charset="utf-8">
    <title>{task.suggested_title or task.keyword}</title>
    <meta name="description" content="{task.suggested_meta or ''}">
    <style>
        body {{
            font-family: Arial, sans-serif;
            background: #f6f6f6;
            color: #222;
            line-height: 1.8;
            margin: 0;
            padding: 0;
        }}
        .container {{
            max-width: 980px;
            margin: 0 auto;
            background: white;
            padding: 40px;
        }}
        h1 {{
            font-size: 34px;
            margin-bottom: 16px;
        }}
        h2 {{
            margin-top: 38px;
            font-size: 26px;
        }}
        h3 {{
            margin-top: 24px;
            font-size: 21px;
        }}
        p, li {{
            font-size: 18px;
        }}
        .meta-box, .cta-box, .images-box {{
            background: #f1f5f9;
            border: 1px solid #d9e2ec;
            padding: 18px;
            border-radius: 12px;
            margin: 24px 0;
        }}
        .image-card {{
            background: #fff;
            border: 1px solid #ddd;
            padding: 14px;
            border-radius: 10px;
            margin-bottom: 14px;
        }}
        code {{
            direction: ltr;
            display: block;
            white-space: pre-wrap;
            background: #111827;
            color: #f9fafb;
            padding: 12px;
            border-radius: 8px;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <main class="container">
        <div class="meta-box">
            <strong>Keyword:</strong> {task.keyword}<br>
            <strong>URL:</strong> {task.page_url}<br>
            <strong>Status:</strong> {task.status}<br>
            <strong>Priority:</strong> {task.priority}
        </div>

        <h1>{task.suggested_h1 or task.keyword}</h1>

        <div class="meta-box">
            <strong>SEO Title:</strong><br>
            {task.suggested_title or ''}<br><br>
            <strong>Meta Description:</strong><br>
            {task.suggested_meta or ''}
        </div>

        {task.article_html}

        <div class="cta-box">
            <h2>CTA</h2>
            <p>{task.cta_text or ''}</p>
        </div>

        <div class="images-box">
    <h2>Generated SEO Images</h2>

    {
        "".join(
            [
                f'''
                <div class="image-card">
                    <img
                        src="{img.get("url")}"
                        alt="{img.get("alt_text")}"
                        style="width:100%; border-radius:12px; margin-bottom:10px;"
                    >

                    <strong>ALT:</strong>
                    <p>{img.get("alt_text")}</p>

                    <strong>Filename:</strong>
                    <p>{img.get("filename")}</p>
                </div>
                '''
                for img in json.loads(task.image_prompts or "[]")
            ]
        )
    }
</div>

        <div class="meta-box">
            <h2>FAQ Schema</h2>
            <code>{task.faq_schema or ''}</code>
        </div>

        <div class="meta-box">
            <h2>Article Schema</h2>
            <code>{task.article_schema or ''}</code>
        </div>
    </main>
</body>
</html>
"""

    return HTMLResponse(content=html)

@router.post("/seo/tasks/{task_id}/generate-images")
def generate_images_for_task(
    task_id: int,
    db: DatabaseSession,
) -> dict[str, object]:
    """Generate images for a saved SEO task from image prompts."""

    task = db.query(SEOTask).filter(SEOTask.id == task_id).first()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SEO task not found",
        )

    if not task.image_prompts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No image prompts found for this task",
        )

    try:
        generated_images = ImageGenerator().generate_images_for_task(
            task_id=task.id,
            image_prompts_json=task.image_prompts,
        )

        task.image_prompts = json.dumps(
            generated_images,
            ensure_ascii=False,
        )
        task.status = "images_generated"

        db.commit()
        db.refresh(task)

        return {
            "success": True,
            "task_id": task.id,
            "images_created": len(generated_images),
            "images": generated_images,
        }

    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.get("/seo/tasks/{task_id}/export")
def export_seo_task(
    task_id: int,
    db: DatabaseSession,
) -> dict[str, object]:
    """Export a clean SEO package ready for ISTORE."""

    task = db.query(SEOTask).filter(SEOTask.id == task_id).first()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SEO task not found",
        )

    return {
        "task_id": task.id,
        "status": task.status,
        "page_url": task.page_url,
        "keyword": task.keyword,
        "priority": task.priority,
        "seo_title": task.suggested_title,
        "meta_description": task.suggested_meta,
        "h1": task.suggested_h1,
        "html_article": task.article_html,
        "faq_schema": task.faq_schema,
        "article_schema": task.article_schema,
        "images": json.loads(task.image_prompts or "[]"),
        "cta": task.cta_text,
    }

@router.get(
    "/seo/tasks/{task_id}/export-view",
    response_class=HTMLResponse,
)
def export_view(
    request: Request,
    task_id: int,
    db: DatabaseSession,
) -> HTMLResponse:
    task = db.query(SEOTask).filter(SEOTask.id == task_id).first()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SEO task not found",
        )

    images = json.loads(task.image_prompts or "[]")

    images_html = "".join(
        [
            f"""
            <div style="margin-bottom:40px;">
                <img
                    src="{img.get('url')}"
                    style="width:100%;max-width:600px;border-radius:16px;"
                >

                <p><strong>ALT:</strong> {img.get('alt_text')}</p>
                <p><strong>Filename:</strong> {img.get('filename')}</p>
            </div>
            """
            for img in images
        ]
    )

    html = f"""
    <html>
    <head>
        <title>SEO Export</title>

        <style>
            body {{
                font-family: Arial;
                background: #111;
                color: white;
                padding: 40px;
                max-width: 1200px;
                margin: auto;
            }}

            .box {{
                background: #1c1c1c;
                padding: 25px;
                border-radius: 18px;
                margin-bottom: 30px;
            }}

            textarea {{
                width: 100%;
                min-height: 200px;
                background: #000;
                color: #00ff88;
                border: 1px solid #333;
                padding: 15px;
                border-radius: 12px;
            }}

            h1,h2 {{
                color: #00ff88;
            }}
        </style>
    </head>

    <body>

        <h1>Compass SEO Export</h1>

        <div class="box">
            <h2>SEO Title</h2>
            <textarea>{task.suggested_title or ""}</textarea>
        </div>

        <div class="box">
            <h2>Meta Description</h2>
            <textarea>{task.suggested_meta or ""}</textarea>
        </div>

        <div class="box">
            <h2>H1</h2>
            <textarea>{task.suggested_h1 or ""}</textarea>
        </div>

        <div class="box">
            <h2>Generated Images</h2>
            {images_html}
        </div>

        <div class="box">
            <h2>HTML Article</h2>
            <textarea>{task.article_html or ""}</textarea>
        </div>

        <div class="box">
            <h2>FAQ Schema</h2>
            <textarea>{task.faq_schema or ""}</textarea>
        </div>

        <div class="box">
            <h2>Article Schema</h2>
            <textarea>{task.article_schema or ""}</textarea>
        </div>

    </body>
    </html>
    """

    return HTMLResponse(content=html)

@router.get("/seo/tasks/{task_id}/patch-plan")
def generate_task_patch_plan(
    task_id: int,
    db: DatabaseSession,
) -> dict[str, object]:
    """Generate SEO patch plan for an existing task."""

    task = db.query(SEOTask).filter(SEOTask.id == task_id).first()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SEO task not found",
        )

    seo_data = {
        "task_id": task.id,
        "page_url": task.page_url,
        "keyword": task.keyword,
        "priority": task.priority,
        "status": task.status,
        "suggested_title": task.suggested_title,
        "suggested_meta": task.suggested_meta,
        "suggested_h1": task.suggested_h1,
        "ai_recommendation": task.ai_recommendation,
        "clicks_before": task.clicks_before,
        "impressions_before": task.impressions_before,
        "ctr_before": task.ctr_before,
        "position_before": task.position_before,
        "has_article": bool(task.article_html),
        "has_images": bool(task.image_prompts),
        "has_faq_schema": bool(task.faq_schema),
        "has_article_schema": bool(task.article_schema),
    }

    try:
        patch_plan = OpenAIClient().generate_seo_patch_plan(seo_data=seo_data)

        return {
            "success": True,
            "task_id": task.id,
            "keyword": task.keyword,
            "page_url": task.page_url,
            "patch_plan": patch_plan,
        }

    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.get("/seo/internal-link-opportunities")
def internal_link_opportunities(
    db: DatabaseSession,
) -> dict[str, object]:
    """Generate internal link opportunities from saved SEO tasks."""

    tasks = db.query(SEOTask).all()

    pages = []

    for task in tasks:
        strength_score = (
            (task.clicks_before * 2)
            + (task.impressions_before * 0.2)
            + ((10 - min(task.position_before, 10)) * 10)
            + (task.ctr_before * 100)
        )

        opportunity_score = (
            (task.impressions_before * 0.5)
            + ((15 - min(task.position_before, 15)) * 15)
            + ((0.1 - min(task.ctr_before, 0.1)) * 200)
        )

        pages.append(
            {
                "page_url": task.page_url,
                "keyword": task.keyword,
                "priority": task.priority,
                "status": task.status,
                "suggested_title": task.suggested_title,
                "suggested_h1": task.suggested_h1,
                "clicks_before": task.clicks_before,
                "impressions_before": task.impressions_before,
                "ctr_before": task.ctr_before,
                "position_before": task.position_before,
                "strength_score": round(strength_score, 2),
                "opportunity_score": round(opportunity_score, 2),
                "has_article": bool(task.article_html),
                "has_images": bool(task.image_prompts),
            }
        )

    try:
        result = OpenAIClient().generate_internal_link_opportunities(
            pages=pages,
        )

        return {
            "success": True,
            "total_pages_analyzed": len(pages),
            "internal_links": result.get("links", []),
        }

    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

@router.post("/seo/tasks/sync-gsc-metrics")
def sync_seo_tasks_gsc_metrics(
    db: DatabaseSession,
) -> dict[str, object]:
    """Sync saved SEO tasks with current Search Console metrics."""

    tasks = db.query(SEOTask).all()
    gsc_client = GSCClient.from_settings()

    updated_tasks = []

    for task in tasks:
        metrics = gsc_client.get_page_metrics(task.page_url)

        task.clicks_before = metrics.get("clicks", 0)
        task.impressions_before = metrics.get("impressions", 0)
        task.ctr_before = metrics.get("ctr", 0)
        task.position_before = metrics.get("position", 0)

        updated_tasks.append(
            {
                "task_id": task.id,
                "keyword": task.keyword,
                "page_url": task.page_url,
                "clicks": task.clicks_before,
                "impressions": task.impressions_before,
                "ctr": task.ctr_before,
                "position": task.position_before,
            }
        )

    db.commit()

    return {
        "success": True,
        "tasks_synced": len(updated_tasks),
        "tasks": updated_tasks,
    }