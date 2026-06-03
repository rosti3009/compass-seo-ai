import json
import logging
import re
from datetime import UTC, date, datetime
from html import escape
from typing import Annotated
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_istore_token, settings
from app.db.database import get_db
from app.db.models import (
    ContentArticleDraft,
    CrawlRun,
    GoogleOAuthToken,
    GSCKeywordMetric,
    IStoreProduct,
    IStoreProductMapping,
    IStoreSEOApproval,
    PageAudit,
    PublishingPackage,
    SEOAutomationRun,
    SEOFix,
    SEOScheduleConfig,
    SEOStrategyRecommendation,
    SEOTask,
)
from app.integrations.ga4 import GA4Client
from app.integrations.ga4 import MissingGoogleCredentialsError as MissingGA4CredentialsError
from app.integrations.google_auth import GOOGLE_OAUTH_SCOPES, oauth_status, utc_expiry_from_seconds
from app.integrations.gsc import GSCAPIError, GSCClient
from app.integrations.gsc import MissingGoogleCredentialsError as MissingGSCCredentialsError
from app.integrations.istore import IStoreAPIError, IStoreClient, MissingIStoreSettingsError
from app.integrations.openai_client import OpenAIClient
from app.services.content_articles import (
    GENERIC_FILLER_PHRASES,
    _classify_topic as classify_topic,
    build_topic_seo_metadata,
    validate_article_relevance,
    generate_daily_article_draft,
    generate_topic_article_draft,
    refresh_internal_link_index,
)
from app.services.crawler import SEOCrawler
from app.services.hebrew_seo import analyze_page_hebrew_seo, israeli_seasonality, summarize_hebrew_insights
from app.services.image_generation import build_realistic_hero_prompt, get_image_provider
from app.services.internal_links import authority_score, best_anchor_text, opportunity_score
from app.services.istore_approval import (
    approve_fix as approve_istore_approval_fix,
)
from app.services.istore_approval import (
    export_content_draft_for_manual_publish,
    mark_english_fallback_drafts_stale,
    preview_generated_content,
    publish_approved_fix,
    rollback_preview,
    rollback_published_fix,
    scan_istore_seo_opportunities,
    validate_istore_payload,
)
from app.services.istore_approval import (
    reject_fix as reject_istore_approval_fix,
)
from app.services.istore_blog_publisher import IStoreBlogPublisher, IStoreBlogPublishError
from app.services.istore_browser_automation import check_istore_browser_status, create_shop_information_page
from app.services.istore_mapping import (
    PUBLISHABLE_CONFIDENCE_THRESHOLD,
    assign_product_mapping,
    enrich_istore_seo_fields,
    list_products_missing_seo,
    list_synced_products,
    publishable_mapping,
    sync_istore_products,
    verify_pending_istore_mappings,
)
from app.services.istore_product_seo import analyze_istore_product_seo
from app.services.seo_auto_fixes import (
    AutoFixOptions,
    fix_to_review_dict,
    generate_fixes_from_latest_crawl,
    pending_fixes_review,
)
from app.services.seo_automation import run_seo_automation
from app.services.seo_draft_lifecycle import (
    fresh_drafts,
    invalidate_stale_drafts,
    is_stale_draft,
    regenerate_stale_drafts,
    stale_drafts,
)
from app.services.seo_scheduler import (
    create_schedule_config,
    ensure_default_schedule_config,
    run_due_schedules,
    set_schedule_enabled,
)
from app.services.seo_strategy_engine import generate_strategy_recommendations, summarize_site_strategy
from app.services.seo_url_filters import get_url_exclusion_reason, is_seo_eligible_url
from app.services.sitemap import discover_sitemap_urls
from app.services.topical_clusters import build_cluster_summary

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
DatabaseSession = Annotated[Session, Depends(get_db)]


class IStoreApprovalAction(BaseModel):
    approved_by: str | None = None
    metadata: dict[str, object] | None = None


class IStorePublishRequest(BaseModel):
    approval: bool = False
    dry_run: bool = False


class IStoreDraftEditRequest(BaseModel):
    proposed_value: str


class SimpleBulkApprovalRequest(BaseModel):
    fix_ids: list[int] = []
    confirmed: bool = False


class ContentArticleEditRequest(BaseModel):
    title: str | None = None
    slug: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    article_body: str | None = None


class ManualTopicArticleRequest(BaseModel):
    topic_title: str | list[str]
    focus_keyword: str
    target_intent: str = "commercial_informational"
    preferred_slug: str | None = None


class IStorePayloadValidationRequest(BaseModel):
    payload: dict[str, object]


class IStoreAssignProductRequest(BaseModel):
    istore_product_id: str


class SEOAutoFixGenerationRequest(BaseModel):
    limit: int = 50
    min_risk_level: str | None = None
    page_type: str | None = None
    dry_run: bool = True


class SEOScheduleConfigCreate(BaseModel):
    name: str = "Daily SEO Automation"
    frequency: str = "daily"
    hour_utc: int = 5
    max_tasks: int = 10
    generate_articles: bool = False
    sync_gsc: bool = True
    enabled: bool = False


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


SIMPLE_ISSUE_LABELS = {
    "title_too_long": "הכותרת ארוכה מדי וגוגל עלול לחתוך אותה.",
    "generic_ai_meta": "התיאור נשמע גנרי מדי ולא מספיק משכנע ללקוחות.",
    "duplicate_meta_similarity": "התיאור דומה מדי לעמודים אחרים באתר.",
    "duplicate_meta_description": "התיאור דומה מדי לעמודים אחרים באתר.",
    "thin_content": "חסר מידע שיעזור ללקוחות להבין את המוצר.",
    "system_page_indexable": "זה עמוד מערכת שלא צריך לקדם בגוגל.",
    "missing_h1": "חסרה כותרת ראשית ברורה בעמוד.",
    "non_descriptive_slug": "כתובת העמוד לא מספיק ברורה.",
    "invalid_slug": "כתובת העמוד לא מספיק ברורה.",
}

SIMPLE_IMPORTANCE_BY_FIELD = {
    "meta_title": "כותרת טובה יותר יכולה לגרום ליותר אנשים ללחוץ על התוצאה בגוגל.",
    "meta_description": "תיאור ייחודי עוזר לגוגל להבין במה העמוד שונה מעמודים אחרים.",
    "content_draft": "טקסט ברור יותר עוזר ללקוח להבין אם המוצר מתאים לו.",
    "h1_recommendation": "כותרת ברורה עוזרת ללקוח להבין מיד לאן הגיע ומה יש בעמוד.",
    "keyword": "כתובת ברורה עוזרת ללקוחות ולגוגל להבין את נושא העמוד.",
    "noindex_recommendation": "הסתרת עמודי מערכת מגוגל עוזרת להתמקד בעמודים שבאמת חשובים ללקוחות.",
}

SIMPLE_STATUS_LABELS = {
    "PENDING_APPROVAL": "ממתין לבדיקה",
    "APPROVED": "אושר ומוכן לבדיקה",
    "DRY_RUN_PASSED": "בדיקת פרסום עברה",
    "REJECTED": "נדחה",
    "PUBLISHED": "פורסם באתר",
    "VERIFIED": "אומת באתר",
    "FAILED": "נכשל",
    "FAILED_VERIFICATION": "הפרסום נשלח אך לא אומת",
    "ROLLED_BACK": "שוחזר",
    "INVALIDATED": "טיוטה ישנה / בוטלה",
}

SIMPLE_SAFE_FIELDS = {"meta_title", "meta_description", "keyword", "product_description"}

SEO_STRATEGY_STATUSES = {"pending", "accepted", "ignored", "completed"}
SEO_STRATEGY_RECOMMENDATION_TYPES = {
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


LOW_CTR_THRESHOLD = 0.03
HIGH_IMPRESSIONS_THRESHOLD = 50
WEAK_RANKING_MIN = 4
WEAK_RANKING_MAX = 20


def _raise_if_url_excluded(page_url: str) -> None:
    reason = get_url_exclusion_reason(page_url)
    if reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "URL is excluded from SEO content workflows", "excluded_reason": reason},
        )


def _get_schedule_config_or_404(db: Session, config_id: int) -> SEOScheduleConfig:
    config = db.get(SEOScheduleConfig, config_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO schedule config not found")
    return config


def _scheduled_automation_runs(db: Session, limit: int = 25) -> list[SEOAutomationRun]:
    return (
        db.query(SEOAutomationRun)
        .filter(SEOAutomationRun.summary_json.contains('"scheduler"'))
        .order_by(SEOAutomationRun.started_at.desc(), SEOAutomationRun.id.desc())
        .limit(limit)
        .all()
    )


def _get_istore_approval_or_404(db: Session, fix_id: int) -> IStoreSEOApproval:
    approval = db.get(IStoreSEOApproval, fix_id)
    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ISTORE SEO approval fix not found")
    return approval


def _require_google_oauth_settings() -> tuple[str, str, str]:
    """Return configured Google OAuth client settings or raise a clear API error."""
    missing = [
        name
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.google_oauth_client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.google_oauth_client_secret),
            ("GOOGLE_OAUTH_REDIRECT_URI", settings.google_oauth_redirect_uri),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Google OAuth is not configured. Set {', '.join(missing)}.",
        )
    return (
        settings.google_oauth_client_id or "",
        settings.google_oauth_client_secret or "",
        settings.google_oauth_redirect_uri or "",
    )


def _store_google_oauth_token(db: Session, token_payload: dict[str, object]) -> GoogleOAuthToken:
    """Persist a Google OAuth token response, replacing any previous Google token."""
    client_id, client_secret, _redirect_uri = _require_google_oauth_settings()
    scopes = str(token_payload.get("scope") or " ".join(GOOGLE_OAUTH_SCOPES)).split()
    token = (
        db.query(GoogleOAuthToken)
        .filter(GoogleOAuthToken.provider == "google")
        .order_by(GoogleOAuthToken.updated_at.desc(), GoogleOAuthToken.id.desc())
        .first()
    )
    if token is None:
        token = GoogleOAuthToken(provider="google", client_id=client_id, client_secret=client_secret, access_token="")
        db.add(token)
    token.access_token = str(token_payload.get("access_token") or "")
    token.refresh_token = str(token_payload.get("refresh_token") or token.refresh_token or "") or None
    token.token_uri = str(token_payload.get("token_uri") or "https://oauth2.googleapis.com/token")
    token.client_id = client_id
    token.client_secret = client_secret
    token.scopes_json = json.dumps(scopes)
    token.expiry = utc_expiry_from_seconds(token_payload.get("expires_in"))
    db.commit()
    db.refresh(token)
    return token


def _parse_metric_date(value: object) -> date:
    """Parse a Search Console date, falling back to today for malformed provider data."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return date.today()


def _keyword_opportunity_score(metric: GSCKeywordMetric | dict[str, object] | None) -> int:
    """Score GSC opportunity from impressions, CTR weakness, and page-one/two ranking distance."""
    if metric is None:
        return 0
    if isinstance(metric, dict):
        impressions = int(metric.get("impressions") or 0)
        ctr = float(metric.get("ctr") or 0.0)
        position = float(metric.get("average_position") or 0.0)
    else:
        impressions = int(getattr(metric, "impressions", 0))
        ctr = float(getattr(metric, "ctr", 0.0))
        position = float(getattr(metric, "average_position", 0.0))
    impression_score = min(impressions / 1000, 1) * 45
    ctr_score = max(0.0, LOW_CTR_THRESHOLD - ctr) / LOW_CTR_THRESHOLD * 35 if ctr < LOW_CTR_THRESHOLD else 0
    position_score = 20 if WEAK_RANKING_MIN <= position <= WEAK_RANKING_MAX else 8 if position <= 30 else 0
    return round(impression_score + ctr_score + position_score)


def _gsc_opportunity_reason(metric: GSCKeywordMetric) -> str:
    reasons = []
    if metric.impressions >= HIGH_IMPRESSIONS_THRESHOLD and metric.ctr < LOW_CTR_THRESHOLD:
        reasons.append("high impressions with low CTR")
    if WEAK_RANKING_MIN <= metric.average_position <= WEAK_RANKING_MAX:
        reasons.append("ranking within positions 4-20")
    if metric.clicks == 0 and metric.impressions > 0:
        reasons.append("missing click coverage")
    return ", ".join(reasons) or "keyword has measurable GSC demand"


def _gsc_recommended_action(metric: GSCKeywordMetric) -> str:
    if metric.ctr < LOW_CTR_THRESHOLD and metric.average_position <= 10:
        return "Rewrite title/meta description around the query to improve SERP CTR."
    if WEAK_RANKING_MIN <= metric.average_position <= WEAK_RANKING_MAX:
        return "Strengthen on-page coverage and add internal links using this query as anchor text."
    return "Review content coverage and align the page with this query's intent."


def _metric_payload(metric: GSCKeywordMetric) -> dict[str, object]:
    payload = metric.to_dict()
    payload["keyword_opportunity_score"] = _keyword_opportunity_score(metric)
    return payload


def _top_gsc_metric_for_url(db: Session, page_url: str) -> GSCKeywordMetric | None:
    return (
        db.query(GSCKeywordMetric)
        .filter(GSCKeywordMetric.page_url == page_url)
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc())
        .first()
    )


def _gsc_metrics_by_url(db: Session, page_urls: list[str]) -> dict[str, GSCKeywordMetric]:
    if not page_urls:
        return {}
    rows = (
        db.query(GSCKeywordMetric)
        .filter(GSCKeywordMetric.page_url.in_(page_urls))
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc())
        .all()
    )
    metrics: dict[str, GSCKeywordMetric] = {}
    for row in rows:
        metrics.setdefault(row.page_url, row)
    return metrics


def _related_gsc_queries(db: Session, page_url: str, limit: int = 10) -> list[str]:
    rows = (
        db.query(GSCKeywordMetric)
        .filter(GSCKeywordMetric.page_url == page_url)
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc())
        .limit(limit)
        .all()
    )
    return [row.query for row in rows if row.query]


def _gsc_keyword_query(
    db: Session,
    page_url: str | None = None,
    query: str | None = None,
    min_impressions: int | None = None,
    max_position: float | None = None,
    low_ctr_only: bool = False,
):
    metrics_query = db.query(GSCKeywordMetric)
    if page_url:
        metrics_query = metrics_query.filter(GSCKeywordMetric.page_url == page_url)
    if query:
        metrics_query = metrics_query.filter(GSCKeywordMetric.query.ilike(f"%{query}%"))
    if min_impressions is not None:
        metrics_query = metrics_query.filter(GSCKeywordMetric.impressions >= min_impressions)
    if max_position is not None:
        metrics_query = metrics_query.filter(GSCKeywordMetric.average_position <= max_position)
    if low_ctr_only:
        metrics_query = metrics_query.filter(GSCKeywordMetric.ctr < LOW_CTR_THRESHOLD)
    return metrics_query


def _gsc_opportunities(db: Session, limit: int = 100) -> list[dict[str, object]]:
    rows = (
        db.query(GSCKeywordMetric)
        .filter(
            GSCKeywordMetric.impressions >= HIGH_IMPRESSIONS_THRESHOLD,
            GSCKeywordMetric.ctr < LOW_CTR_THRESHOLD,
            GSCKeywordMetric.average_position >= WEAK_RANKING_MIN,
            GSCKeywordMetric.average_position <= WEAK_RANKING_MAX,
        )
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.average_position.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "page_url": row.page_url,
            "query": row.query,
            "clicks": row.clicks,
            "impressions": row.impressions,
            "ctr": row.ctr,
            "average_position": row.average_position,
            "opportunity_reason": _gsc_opportunity_reason(row),
            "recommended_action": _gsc_recommended_action(row),
            "keyword_opportunity_score": _keyword_opportunity_score(row),
        }
        for row in rows
    ]


def _upsert_gsc_metric(db: Session, row: dict[str, object]) -> bool:
    page_url = str(row.get("page_url") or "").strip()
    query = str(row.get("query") or "").strip()
    metric_date = _parse_metric_date(row.get("date"))
    source = str(row.get("source") or "gsc")
    if not page_url or not query:
        return False
    metric = (
        db.query(GSCKeywordMetric)
        .filter(
            GSCKeywordMetric.page_url == page_url,
            GSCKeywordMetric.query == query,
            GSCKeywordMetric.date == metric_date,
            GSCKeywordMetric.source == source,
        )
        .first()
    )
    if metric is None:
        metric = GSCKeywordMetric(page_url=page_url, query=query, date=metric_date, source=source)
        db.add(metric)
    metric.clicks = int(row.get("clicks") or 0)
    metric.impressions = int(row.get("impressions") or 0)
    metric.ctr = float(row.get("ctr") or 0.0)
    metric.average_position = float(row.get("average_position") or 0.0)
    return True


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


def _task_recommendation_payload(task: SEOTask, db: Session | None = None) -> dict[str, object]:
    """Build a compact page/task payload for OpenAI recommendation generation."""
    try:
        existing_recommendation = json.loads(task.recommendation_json or "{}")
    except json.JSONDecodeError:
        existing_recommendation = task.recommendation_json

    gsc_metric = _top_gsc_metric_for_url(db, task.page_url) if db is not None else None
    related_queries = _related_gsc_queries(db, task.page_url) if db is not None else []
    return {
        "task_id": task.id,
        "page_url": task.page_url,
        "keyword": task.keyword,
        "priority": task.priority,
        "status": task.status,
        "suggested_title": task.suggested_title,
        "suggested_h1": task.suggested_h1,
        "meta_description": task.meta_description,
        "gsc_primary_query": gsc_metric.query if gsc_metric else task.keyword,
        "gsc_keyword_opportunity_score": _keyword_opportunity_score(gsc_metric),
        "gsc_metric": _metric_payload(gsc_metric) if gsc_metric else None,
        "related_gsc_queries": related_queries,
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


def _task_article_payload(task: SEOTask, db: Session | None = None) -> dict[str, object]:
    """Build the complete payload used for full article generation."""
    gsc_metric = _top_gsc_metric_for_url(db, task.page_url) if db is not None else None
    related_queries = _related_gsc_queries(db, task.page_url) if db is not None else []
    gsc_keyword_rows = (
        db.query(GSCKeywordMetric)
        .filter(GSCKeywordMetric.page_url == task.page_url)
        .order_by(GSCKeywordMetric.impressions.desc())
        .limit(10)
        .all()
        if db is not None
        else []
    )
    return {
        "task_id": task.id,
        "page_url": task.page_url,
        "keyword": task.keyword or (gsc_metric.query if gsc_metric else None),
        "priority": task.priority,
        "status": task.status,
        "suggested_title": task.suggested_title,
        "suggested_h1": task.suggested_h1,
        "meta_description": task.meta_description,
        "gsc_primary_query": gsc_metric.query if gsc_metric else task.keyword,
        "secondary_keywords": related_queries,
        "gsc_keywords": [_metric_payload(row) for row in gsc_keyword_rows],
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
    pages: list[PageAudit], tasks_by_url: dict[str, SEOTask], gsc_by_url: dict[str, GSCKeywordMetric] | None = None
) -> list[dict[str, object]]:
    """Pair strong source pages with weak target pages and produce deterministic suggestions."""
    scored_pages = [
        {
            "page": page,
            "task": tasks_by_url.get(page.url),
            "authority_score": authority_score(page),
            "opportunity_score": max(
                opportunity_score(page, tasks_by_url.get(page.url)),
                _keyword_opportunity_score((gsc_by_url or {}).get(page.url)),
            ),
            "gsc_metric": (gsc_by_url or {}).get(page.url),
        }
        for page in pages
        if page.status_code < 400 and is_seo_eligible_url(page.url)
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
            target_metric = target.get("gsc_metric")
            anchor_text = (
                target_metric.query
                if isinstance(target_metric, GSCKeywordMetric)
                else best_anchor_text(target["page"], target["task"])
            )
            opportunity = {
                "source_url": source["page"].url,
                "target_url": target["page"].url,
                "anchor_text": anchor_text,
                "reason": (
                    "High-authority source page can pass relevance to a GSC keyword opportunity."
                    if isinstance(target_metric, GSCKeywordMetric)
                    else "High-authority source page can pass relevance to a weaker page that needs more internal "
                    "link support."
                ),
                "authority_score": source["authority_score"],
                "opportunity_score": target["opportunity_score"],
            }
            if isinstance(target_metric, GSCKeywordMetric):
                opportunity.update(
                    {
                        "gsc_query": target_metric.query,
                        "gsc_impressions": target_metric.impressions,
                        "gsc_ctr": target_metric.ctr,
                    }
                )
            opportunities.append(opportunity)
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
        "excluded_reason": get_url_exclusion_reason(page.url),
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


def _build_task_from_page(page: PageAudit, gsc_metric: GSCKeywordMetric | None = None) -> SEOTask:
    missing_fields = [field for field in page.missing_fields.split(",") if field]
    keyword_opportunity_score = _keyword_opportunity_score(gsc_metric)
    recommendations = [f"Add or improve {field.replace('_', ' ')}." for field in missing_fields] or [
        "Improve on-page SEO signals for this low-scoring page."
    ]
    if gsc_metric:
        recommendations.insert(
            0,
            f"Prioritize GSC query '{gsc_metric.query}' with {gsc_metric.impressions} impressions and "
            f"{gsc_metric.ctr:.2%} CTR.",
        )
    recommendation = {
        "source": "latest_crawl_gsc_enriched" if gsc_metric else "latest_crawl",
        "page_audit_id": page.id,
        "seo_score": page.seo_score,
        "missing_fields": missing_fields,
        "primary_query": gsc_metric.query if gsc_metric else None,
        "keyword_opportunity_score": keyword_opportunity_score,
        "gsc_metric": _metric_payload(gsc_metric) if gsc_metric else None,
        "recommendations": recommendations,
    }
    priority = _priority_for_page(page)
    if keyword_opportunity_score >= 55:
        priority = "high"
    return SEOTask(
        page_url=page.url,
        keyword=gsc_metric.query if gsc_metric else None,
        priority=priority,
        status="open",
        suggested_title=page.title or None,
        suggested_h1=page.h1 or None,
        meta_description=page.meta_description or None,
        recommendation_json=json.dumps(recommendation),
    )


def _field_has_issue(value: str | None, issue_names: set[str]) -> bool:
    """Return whether a comma-delimited issue field contains one of the supplied issues."""
    return bool({field.strip() for field in (value or "").split(",") if field.strip()} & issue_names)


def _dashboard_metrics(db: Session, latest_pages: list[PageAudit]) -> dict[str, int]:
    """Return SEO workflow counts for dashboard cards."""
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in latest_pages])
    gsc_by_url = _gsc_metrics_by_url(db, [page.url for page in latest_pages])
    internal_link_opportunities_count = len(_build_internal_link_opportunities(latest_pages, tasks_by_url, gsc_by_url))
    page_payloads = [_page_cluster_payload(page, tasks_by_url.get(page.url)) for page in latest_pages]
    generic_ai_issues = {"generic_ai_meta", "generic_ai_title", "repetitive_ai_content"}
    duplicate_meta_issues = {"duplicate_meta_description", "duplicate_meta_similarity", "duplicate_title_similarity"}
    return {
        "total_tasks": db.query(SEOTask).count(),
        "recommended_tasks": db.query(SEOTask).filter(SEOTask.status == "recommended").count(),
        "generated_articles": db.query(SEOTask).filter(SEOTask.article_status == "generated").count(),
        "internal_link_opportunities": internal_link_opportunities_count,
        "topical_clusters": len(build_cluster_summary(page_payloads)),
        "strategy_recommendations": db.query(SEOStrategyRecommendation)
        .filter(SEOStrategyRecommendation.status == "pending")
        .count(),
        "critical_issues": sum(1 for page in latest_pages if (page.seo_risk_level or "").lower() == "critical"),
        "high_risk_issues": sum(1 for page in latest_pages if (page.seo_risk_level or "").lower() == "high"),
        "pending_fixes": db.query(IStoreSEOApproval).filter(IStoreSEOApproval.status == "PENDING_APPROVAL").count(),
        "approved_fixes": db.query(IStoreSEOApproval).filter(IStoreSEOApproval.status == "APPROVED").count(),
        "published_fixes": db.query(IStoreSEOApproval).filter(IStoreSEOApproval.status == "PUBLISHED").count(),
        "verified_fixes": db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.publish_mapping_verified.is_(True))
        .count(),
        "synced_istore_products": db.query(IStoreProduct).count(),
        "verified_mappings": db.query(IStoreProductMapping)
        .filter(IStoreProductMapping.active.is_(True))
        .filter(IStoreProductMapping.mapping_confidence >= PUBLISHABLE_CONFIDENCE_THRESHOLD)
        .count(),
        "unmapped_fixes": db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status.in_(["PENDING_APPROVAL", "APPROVED"]))
        .filter(IStoreSEOApproval.publish_mapping_verified.is_(False))
        .filter(IStoreSEOApproval.mapping_conflict.is_(False))
        .count(),
        "ambiguous_mappings": db.query(IStoreSEOApproval).filter(IStoreSEOApproval.mapping_conflict.is_(True)).count(),
        "generic_ai_meta": sum(1 for page in latest_pages if _field_has_issue(page.missing_fields, generic_ai_issues)),
        "duplicate_meta": sum(
            1 for page in latest_pages if _field_has_issue(page.missing_fields, duplicate_meta_issues)
        ),
        "brand_pages": sum(1 for page in latest_pages if page.page_type == "brand"),
        "system_pages_excluded": sum(
            1 for page in latest_pages if page.page_type == "system" or bool(get_url_exclusion_reason(page.url))
        ),
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


def _operations_view_context(db: Session, *, legacy_root_markers: bool = False) -> dict[str, object]:
    """Build the shared context for the SEO operations dashboard and result views."""
    latest_run, latest_pages = _latest_crawl_context(db, limit=25)
    metrics_pages = _latest_crawl_pages(db)
    return {
        "target_domain": settings.target_domain,
        "latest_run": latest_run,
        "pages": latest_pages,
        "metrics": _dashboard_metrics(db, metrics_pages),
        "legacy_root_markers": legacy_root_markers,
    }


def _simple_page_name(fix: IStoreSEOApproval) -> str:
    source = fix.target_url or fix.source_url or fix.target_id or "עמוד ללא שם"
    cleaned = source.rstrip("/").split("/")[-1] if source.startswith("http") else source
    return cleaned.replace("-", " ").replace("_", " ") or source


def _simple_issue_label(issue_type: str | None) -> str:
    return SIMPLE_ISSUE_LABELS.get(issue_type or "", "נדרש שיפור קטן כדי שהעמוד יהיה ברור ומשכנע יותר.")


def _simple_importance(fix: IStoreSEOApproval) -> str:
    if fix.issue_type in {"duplicate_meta_similarity", "duplicate_meta_description"}:
        return "תיאור ייחודי עוזר לגוגל להבין במה העמוד שונה מעמודים אחרים."
    if fix.issue_type == "thin_content":
        return "טקסט ברור יותר עוזר ללקוח להבין אם המוצר מתאים לו."
    return SIMPLE_IMPORTANCE_BY_FIELD.get(
        fix.field_path,
        "שיפור ברור ומדויק יותר יכול לעזור ללקוחות להבין את העמוד ולקבל החלטה מהר יותר.",
    )


def _simple_suggestion(fix: IStoreSEOApproval) -> str:
    suggestions = {
        "meta_title": "להחליף לכותרת קצרה וברורה יותר.",
        "meta_description": "להחליף לתיאור ייחודי ומשכנע יותר.",
        "content_draft": "להוסיף טקסט הסבר קצר וברור יותר בעמוד.",
        "h1_recommendation": "להוסיף כותרת ראשית ברורה בעמוד.",
        "keyword": "לבדוק כתובת עמוד ברורה יותר לפני שינוי ידני.",
        "noindex_recommendation": "לבדוק אם זה באמת עמוד מערכת שלא צריך להופיע בגוגל.",
    }
    return suggestions.get(fix.field_path, "לבדוק את הטקסט המוצע ולאשר רק אם הוא מתאים לעמוד.")


def _simple_review_state(fix: IStoreSEOApproval) -> tuple[bool, str]:
    if fix.mapping_conflict:
        return False, "נמצאו כמה מוצרים דומים — צריך לבחור ידנית"
    if not fix.publish_mapping_verified:
        return False, "צריך לחבר למוצר בחנות"
    if not publishable_mapping(fix):
        return False, "עדיין לא ניתן לפרסם"
    return True, "כן, אפשר לאשר בבטחה"


def _is_simple_bulk_safe_fix(fix: IStoreSEOApproval) -> bool:
    metadata = fix.to_dict().get("approval_metadata", {})
    page_type = metadata.get("page_type") if isinstance(metadata, dict) else None
    return bool(
        fix.status == "PENDING_APPROVAL"
        and fix.field_path in SIMPLE_SAFE_FIELDS
        and page_type != "system"
        and publishable_mapping(fix)
    )


def _simple_publish_block_reason(fix: IStoreSEOApproval) -> str | None:
    if fix.mapping_conflict or not fix.publish_mapping_verified:
        return "אי אפשר לפרסם עדיין כי המוצר לא חובר בוודאות לחנות."
    if not settings.istore_publish_enabled:
        return "פרסום לא פעיל בסביבת Render. צריך להפעיל ISTORE_PUBLISH_ENABLED."
    if settings.istore_safe_mode:
        return "מצב בטוח פעיל כרגע ולכן אי אפשר לפרסם אוטומטית."
    if fix.target_type != "product":
        return "פרסום זמין רק לדפי מוצר בטוחים."
    if fix.field_path not in SIMPLE_SAFE_FIELDS:
        return "השדה הזה אינו שדה SEO בטוח לפרסום."
    return None


def _simple_next_action(status: str) -> str:
    return {
        "PENDING_APPROVAL": "אשר שינוי",
        "APPROVED": "בדיקת פרסום יבשה",
        "DRY_RUN_PASSED": "פרסם באתר",
        "PUBLISHED": "בדוק שהשינוי הופיע באתר",
        "VERIFIED": "הושלם",
        "FAILED": "צריך בדיקה",
        "FAILED_VERIFICATION": "צריך בדיקת מיפוי title",
        "ROLLED_BACK": "שוחזר",
    }.get(status, "צריך בדיקה")


def _simple_fix_card(fix: IStoreSEOApproval) -> dict[str, object]:
    can_approve, approval_label = _simple_review_state(fix)
    status_label = SIMPLE_STATUS_LABELS.get(fix.status, "ממתין לבדיקה")
    if fix.mapping_conflict:
        safety_label = "נמצאו כמה מוצרים דומים — צריך לבחור ידנית"
    elif not fix.publish_mapping_verified:
        safety_label = "צריך לחבר למוצר בחנות"
    elif not publishable_mapping(fix):
        safety_label = "עדיין לא ניתן לפרסם"
    else:
        safety_label = "מוכן לאישור בטוח"
    stale = is_stale_draft(fix)
    publish_block_reason = _simple_publish_block_reason(fix)
    can_publish = (
        fix.status in {"APPROVED", "DRY_RUN_PASSED"} and publish_block_reason is None and publishable_mapping(fix)
    )
    freshness_label = "טיוטה ישנה" if stale else "טיוטה חדשה"
    engine_label = "נוצר עם מנוע ישן" if stale else "מנוע עדכני"
    regen_label = "נוצר מחדש" if fix.regenerated_from_id else ""
    verification_message = None
    if fix.status == "FAILED_VERIFICATION":
        verification_message = "הפרסום נשלח לחנות, אבל הכותרת באתר עדיין לא השתנתה. ייתכן שהשדה לא נכון או שיש cache."
    metadata = fix.to_dict().get("approval_metadata", {}) or {}
    decision_payload = metadata.get("decision", {}) if isinstance(metadata, dict) else {}
    decision_label = decision_payload.get("decision", "REWRITE") if isinstance(decision_payload, dict) else "REWRITE"
    return {
        "id": fix.id,
        "verification_message": verification_message,
        "page_name": _simple_page_name(fix),
        "page_url": fix.target_url or fix.source_url or "",
        "problem": _simple_issue_label(fix.issue_type),
        "importance": _simple_importance(fix),
        "suggestion": _simple_suggestion(fix),
        "before": fix.current_value or "—",
        "after": fix.proposed_value or "—",
        "can_approve": can_approve,
        "approval_label": approval_label,
        "status_label": status_label,
        "safety_label": safety_label,
        "show_google_preview": fix.field_path in {"meta_title", "meta_description"},
        "preview_title": (
            fix.proposed_value if fix.field_path == "meta_title" else (_simple_page_name(fix) or "שם העמוד")
        ),
        "preview_description": (
            fix.proposed_value
            if fix.field_path == "meta_description"
            else (fix.current_value or "תיאור העמוד יופיע כאן.")
        ),
        "bulk_safe": _is_simple_bulk_safe_fix(fix),
        "freshness_label": freshness_label,
        "engine_label": engine_label,
        "regen_label": regen_label,
        "stale": stale,
        "can_publish": can_publish,
        "publish_block_reason": publish_block_reason,
        "next_action": _simple_next_action(fix.status),
        "is_system": (fix.to_dict().get("approval_metadata", {}) or {}).get("page_type") == "system",
        "publish_timestamp": fix.publish_timestamp,
        "field_path": fix.field_path,
        "technical_details": (fix.to_dict().get("publish_response", {}) or {}).get("technical_details", {}),
        "decision": decision_label,
        "recommendation_only": decision_label == "KEEP_EXISTING",
        "group_key": (fix.target_url or fix.source_url or "") + "::" + (fix.field_path or ""),
    }


def _simple_workspace_context(db: Session) -> dict[str, object]:
    fixes = (
        db.query(IStoreSEOApproval)
        .filter(
            IStoreSEOApproval.status.in_(
                [
                    "PENDING_APPROVAL",
                    "APPROVED",
                    "DRY_RUN_PASSED",
                    "REJECTED",
                    "PUBLISHED",
                    "VERIFIED",
                    "FAILED",
                    "FAILED_VERIFICATION",
                    "ROLLED_BACK",
                    "INVALIDATED",
                ]
            )
        )
        .order_by(IStoreSEOApproval.priority_score.desc(), IStoreSEOApproval.id.desc())
        .limit(50)
        .all()
    )
    show_stale = False
    visible_fixes = [fix for fix in fixes if fix.status != "INVALIDATED"]
    visible_fixes = [fix for fix in visible_fixes if (show_stale or not is_stale_draft(fix))]
    cards = [_simple_fix_card(fix) for fix in visible_fixes]
    cards = [c for c in cards if c["field_path"] != "keyword" and not c["is_system"] and not c["recommendation_only"]]
    grouped: dict[str, dict[str, object]] = {}
    for card in cards:
        key = card["page_url"] or str(card["id"])
        if key not in grouped:
            grouped[key] = {**card, "additional_suggestions": []}
            continue
        grouped[key]["additional_suggestions"].append(card)
    cards = list(grouped.values())
    pending = [card for card in cards if card["status_label"] == "ממתין לבדיקה"]
    safe = [card for card in cards if card["bulk_safe"]]
    needs_product = [
        card
        for card in cards
        if card["safety_label"] in {"צריך לחבר למוצר בחנות", "נמצאו כמה מוצרים דומים — צריך לבחור ידנית"}
    ]
    approved_count = sum(1 for fix in visible_fixes if fix.status in {"APPROVED", "PUBLISHED", "VERIFIED"})
    if pending:
        if safe:
            next_message = f"יש {len(pending)} תיקונים מוכנים לבדיקה. מומלץ להתחיל מהתיקונים הבטוחים."
            primary_label = "התחל בדיקה"
            primary_href = "#simple-review-list"
        elif needs_product:
            next_message = f"יש {len(needs_product)} מוצרים שצריך לחבר למערכת החנות לפני שאפשר לפרסם."
            primary_label = "חבר מוצרים"
            primary_href = "/seo/fixes/pending-view"
        else:
            next_message = f"יש {len(pending)} תיקונים מוכנים לבדיקה."
            primary_label = "התחל בדיקה"
            primary_href = "#simple-review-list"
    else:
        next_message = "אין תיקונים מוכנים. מומלץ להריץ סריקה חדשה."
        primary_label = "הרץ סריקה"
        primary_href = "/seo/operations-view"
    invalidated_count = db.query(IStoreSEOApproval).filter(IStoreSEOApproval.status == "INVALIDATED").count()
    regenerated_count = db.query(IStoreSEOApproval).filter(IStoreSEOApproval.regenerated_from_id.is_not(None)).count()
    all_article_drafts = db.query(ContentArticleDraft).order_by(ContentArticleDraft.created_at.desc()).limit(20).all()
    eligible_manual = [d for d in all_article_drafts if d.status in {"CONTENT_DRAFT", "READY_FOR_REVIEW"}]
    active_article = next((d for d in eligible_manual if d.is_active_manual_article), None)
    if active_article is None and eligible_manual:
        active_article = eligible_manual[0]
        _set_active_manual_article(db, active_article)
        db.commit()
    active_id = active_article.id if active_article else None
    return {
        "cards": cards,
        "safe_cards": safe,
        "summary": {
            "needs_today": len(pending) + len(needs_product),
            "ready_review": len(pending),
            "safe_approval": len(safe),
            "needs_product_check": len(needs_product),
            "approved": approved_count,
        },
        "next_message": next_message,
        "draft_stats": {
            "stale": len(stale_drafts(db)),
            "fresh": len(fresh_drafts(db)),
            "invalidated": invalidated_count,
            "regenerated": regenerated_count,
        },
        "primary_label": primary_label,
        "primary_href": primary_href,
        "active_article": ({**active_article.to_dict(), **_article_quality_summary(active_article), "debug": _draft_debug(active_article, "title")} if active_article else None),
        "archived_articles": [
            {**d.to_dict(), **_article_quality_summary(d), "debug": _draft_debug(d, "title")}
            for d in all_article_drafts
            if d.id != active_id
        ],
    }



def _set_active_manual_article(db: Session, active_draft: ContentArticleDraft) -> None:
    db.query(ContentArticleDraft).update({ContentArticleDraft.is_active_manual_article: False}, synchronize_session=False)
    active_draft.is_active_manual_article = True
    db.add(active_draft)


def _latest_active_candidate(db: Session) -> ContentArticleDraft | None:
    return (
        db.query(ContentArticleDraft)
        .filter(ContentArticleDraft.status.in_(["CONTENT_DRAFT", "READY_FOR_REVIEW"]))
        .order_by(ContentArticleDraft.created_at.desc(), ContentArticleDraft.id.desc())
        .first()
    )


def _article_generation_response(draft: ContentArticleDraft, endpoint_used: str) -> dict[str, object]:
    quality = _article_quality_summary(draft)
    debug = _draft_debug(draft, "title")
    seo_metadata = build_topic_seo_metadata(
        draft.focus_keyword or "",
        draft.title or draft.topic_title or "",
        classify_topic(draft.topic_title or "", draft.focus_keyword or "", draft.target_intent or ""),
    )
    diagnostics = {
        "article_id": draft.id,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "generator_version": debug.get("generator_version"),
        "selected_generator": debug.get("selected_generator"),
        "generator_source": debug.get("generator_source"),
        "article_brief": debug.get("article_brief"),
        "main_entity": debug.get("main_entity"),
        "entity_type": debug.get("entity_type"),
        "topic_type": debug.get("topic_type"),
        "content_format": debug.get("content_format"),
        "detected_topic_type": debug.get("detected_topic_type"),
        "selected_contract": debug.get("selected_contract"),
        "search_intent": debug.get("search_intent"),
        "primary_keyword": seo_metadata.get("primary_keyword"),
        "secondary_keywords": seo_metadata.get("secondary_keywords"),
        "long_tail_keywords": seo_metadata.get("long_tail_keywords"),
        "question_keywords": seo_metadata.get("question_keywords"),
        "commercial_keywords": seo_metadata.get("commercial_keywords") or seo_metadata.get("usage_keywords"),
        "seo_keywords": seo_metadata.get("seo_keywords"),
        "seo_score": seo_metadata.get("seo_score"),
        "meta_title_score": seo_metadata.get("meta_title_score"),
        "meta_description_score": seo_metadata.get("meta_description_score"),
        "selected_internal_links": debug.get("selected_internal_links"),
        "selected_products": debug.get("selected_products"),
        "excluded_low_relevance_links": debug.get("excluded_low_relevance_links"),
        "link_relevance_score": debug.get("link_relevance_score"),
        "sitemap_loaded_count": debug.get("sitemap_loaded_count"),
        "products_loaded_count": debug.get("products_loaded_count"),
        "categories_loaded_count": debug.get("categories_loaded_count"),
        "final_word_count": debug.get("final_word_count"),
        "title_body_relevance_score": debug.get("title_body_relevance_score"),
        "validation_passed": debug.get("validation_passed"),
        "missing_required_terms": debug.get("missing_required_terms"),
        "forbidden_terms_found": debug.get("forbidden_terms_found"),
        "regenerated_due_to_validation": debug.get("regenerated_due_to_validation"),
        "regeneration_count": debug.get("regeneration_count"),
        "final_body_source": debug.get("final_body_source"),
        "endpoint_used": endpoint_used,
    }
    return {"success": True, "draft": {**draft.to_dict(), "debug": {**debug, "endpoint_used": endpoint_used}, "quality": quality}, "diagnostics": diagnostics}
def _strip_h1_tags(html: str) -> tuple[str, bool]:
    cleaned = re.sub(r"<h1[^>]*>.*?</h1>", "", html or "", flags=re.IGNORECASE | re.DOTALL)
    return cleaned, cleaned != (html or "")


def _topic_keywords_detected(body: str) -> list[str]:
    checks = [
        "hickory", "oak", "apple", "mesquite", "cherry",
        "thin blue smoke", "bitter smoke", "soak", "smoker", "wood chips", "טמפרט"
    ]
    lowered = (body or "").lower()
    return [k for k in checks if k in lowered]


def _draft_debug(draft: ContentArticleDraft, slug_source: str = "title") -> dict[str, object]:
    body = draft.article_body or ""
    _, removed = _strip_h1_tags(body)
    link_debug = getattr(draft, "link_match_debug", {})
    topic_profile = classify_topic(draft.topic_title or "", draft.focus_keyword or "", draft.target_intent or "")
    internal_links = json.loads(draft.internal_links_json or "[]") if draft.internal_links_json else []
    selected_products = json.loads(draft.suggested_related_products_json or "[]") if draft.suggested_related_products_json else []
    link_scores = [float(item.get("relevance_score") or item.get("semantic_topic_match_score") or 0) for item in selected_products if isinstance(item, dict)]
    validation_debug = validate_article_relevance(
        draft.title or draft.topic_title or "",
        draft.focus_keyword or "",
        body,
        topic_profile,
        image_prompt=draft.featured_image_prompt or "",
        internal_links=internal_links,
    )
    if isinstance(link_debug, dict):
        for key in ("regenerated_due_to_validation", "regeneration_count", "final_body_source"):
            if key in link_debug:
                validation_debug[key] = link_debug[key]
    forbidden_terms = topic_profile.get("forbidden_terms", [])
    removed_terms = [term for term in forbidden_terms if term and term not in body]
    return {
        "generator_version": "v3-topic-contract-engine-2026-06-02",
        "slug_source": slug_source,
        "article_brief": topic_profile.get("article_brief"),
        "main_entity": topic_profile.get("main_entity"),
        "entity_type": topic_profile.get("entity_type"),
        "topic_type": topic_profile.get("topic_type"),
        "content_format": topic_profile.get("content_format"),
        "detected_topic_type": topic_profile.get("topic_type"),
        "selected_contract": topic_profile.get("selected_contract"),
        "selected_generator": topic_profile.get("selected_generator"),
        "search_intent": topic_profile.get("search_intent"),
        "generator_source": topic_profile.get("generator_source"),
        "fallback_reason": topic_profile.get("fallback_reason"),
        "forbidden_terms_removed": removed_terms,
        "h1_removed": "<h1" not in body.lower(),
        "topic_keywords_detected": _topic_keywords_detected(body),
        "selected_internal_links": internal_links,
        "selected_products": selected_products,
        "link_relevance_score": round(sum(link_scores) / len(link_scores), 1) if link_scores else 0.0,
        "final_word_count": len(re.findall(r"[\w\u0590-\u05FF]+", re.sub(r"<[^>]+>", " ", body or ""))),
        **{
            key: value
            for key, value in build_topic_seo_metadata(
                draft.focus_keyword or "", draft.title or draft.topic_title or "", topic_profile
            ).items()
            if key
            in {
                "primary_keyword",
                "secondary_keywords",
                "long_tail_keywords",
                "question_keywords",
                "usage_keywords",
                "seo_keywords",
                "seo_score",
                "meta_title_score",
                "meta_description_score",
            }
        },
        "article_template_used": "fallback_generic" if topic_profile.get("topic_type") == "fallback_generic" else "topic_type_contract",
        "h1_cleanup_was_needed": removed,
        "final_body_source": validation_debug.get("final_body_source", "contract_engine" if topic_profile.get("topic_type") != "fallback_generic" else "fallback_generic"),
        "regeneration_count": validation_debug.get("regeneration_count", 0),
        **validation_debug,
        **(link_debug if isinstance(link_debug, dict) else {}),
    }

def _article_quality_summary(draft: ContentArticleDraft) -> dict[str, float | str]:
    links = json.loads(draft.internal_links_json or "[]") if draft.internal_links_json else []
    products = json.loads(draft.suggested_related_products_json or "[]") if draft.suggested_related_products_json else []
    body = draft.article_body or ""
    semantic = round(sum(float(item.get("semantic_topic_match_score", 0)) for item in links) / len(links), 1) if links else 80.0
    suggestion = round(sum(float(item.get("relatedness_score", 0)) for item in products) / len(products), 1) if products else 80.0
    seo = 90.0 if len(draft.meta_title) <= 65 and 70 <= len(draft.meta_description) <= 160 else 72.0
    structure = 100.0
    if "<h1" in body.lower():
        structure -= 30
    h2_count = len(re.findall(r"<h2[\s>]", body, flags=re.IGNORECASE))
    h3_count = len(re.findall(r"<h3[\s>]", body, flags=re.IGNORECASE))
    if h2_count < 5:
        structure -= 25
    if h3_count < 3:
        structure -= 20
    if "compass-grill-article" in (draft.slug or ""):
        structure -= 15
    repeated = ["במדריך הזה נסביר", "למה זה חשוב", "שלבים מעשיים"]
    repeated_penalty = 8 * sum(1 for t in repeated if t in body)
    structure = max(0.0, structure - repeated_penalty)
    generic_slug_penalty = 18 if (draft.slug or "") in {"compass-grill-article", "bbq-hebrew-guide", "grill-smoking-guide"} else 0
    prompt_blob = ((draft.featured_image_prompt or "") + " " + (draft.section_image_prompts_json or "")).lower()
    wing_topic = "כנפיים" in ((draft.topic_title or "") + " " + (draft.focus_keyword or ""))
    wrong_prompt_penalty = 0
    if wing_topic and any(t in prompt_blob for t in ["wood chips", "smoker box", "hickory"]):
        wrong_prompt_penalty = 25
    generic_prompt_penalty = 15 if len(prompt_blob.strip()) < 25 else 0
    topic_profile = classify_topic(draft.topic_title or "", draft.focus_keyword or "", draft.target_intent or "")
    relevance_validation = validate_article_relevance(draft.title or draft.topic_title or "", draft.focus_keyword or "", body, topic_profile)
    technical_bonus = 26 if len(_topic_keywords_detected(body)) >= 7 else 0
    if wing_topic and all(t in body for t in ["74", "גלייז", "קריספ"]):
        technical_bonus += 18
    structure = max(0.0, structure - generic_slug_penalty - generic_prompt_penalty - wrong_prompt_penalty)
    filler_penalty = 12 * sum(1 for phrase in GENERIC_FILLER_PHRASES if phrase in body)
    forbidden_penalty = 15 * sum(1 for term in topic_profile.get("forbidden_terms", []) if term and term in body)
    required_miss_penalty = 7 * sum(1 for term in topic_profile.get("required_terms", []) if term and term not in body)
    faq_penalty = 12 if "שאלות נפוצות" not in body and "FAQ" not in body else 0
    keyword_bonus = 10 if (draft.focus_keyword or "") in (draft.title or "") and (draft.focus_keyword or "") in (draft.meta_title or "") and (draft.focus_keyword or "") in body[:280] else 0
    relevance_penalty = max(0.0, (80.0 - float(relevance_validation["title_body_relevance_score"])) * 1.3)
    if not relevance_validation["validation_passed"]:
        relevance_penalty += 20
    article_quality = min(100.0, round((seo * 0.18) + (semantic * 0.22) + (suggestion * 0.22) + (structure * 0.38) + technical_bonus + keyword_bonus - filler_penalty - forbidden_penalty - required_miss_penalty - faq_penalty - relevance_penalty, 1))
    if not relevance_validation["validation_passed"]:
        article_quality = min(article_quality, 60.0)
    readiness = "READY_FOR_REVIEW" if article_quality >= 75 and relevance_validation["validation_passed"] else ("NEEDS_REWRITE" if not relevance_validation["validation_passed"] else "NEEDS_IMPROVEMENT")
    return {
        "seo_quality_score": seo,
        "semantic_relevance_score": semantic,
        "suggested_link_relevance": suggestion,
        "article_quality_score": article_quality,
        "publish_readiness": readiness,
        "title_body_relevance_score": relevance_validation["title_body_relevance_score"],
        "validation_passed": relevance_validation["validation_passed"],
        "missing_required_terms": relevance_validation["missing_required_terms"],
        "forbidden_terms_found": relevance_validation["forbidden_terms_found"],
    }


def _content_quality_gate_passed(draft: ContentArticleDraft) -> bool:
    summary = _article_quality_summary(draft)
    return (
        summary["publish_readiness"] == "READY_FOR_REVIEW"
        and float(summary["semantic_relevance_score"]) >= 70
        and float(summary["article_quality_score"]) >= 75
        and float(summary["suggested_link_relevance"]) >= 70
    )


def _article_publish_validation(draft: ContentArticleDraft, adapter_ready: bool) -> tuple[bool, str | None]:
    def _blog_target_allowed() -> bool:
        return bool(
            draft.target_path in {"/blog", "/blog/"}
            or (draft.target_path or "").startswith("/blog/")
            and (
                draft.target_url == "https://compassgrill.co.il/blog/"
                or (draft.target_url or "").startswith("https://compassgrill.co.il/blog/")
            )
        )

    if draft.status != "APPROVED":
        logger.info("[BLOG VALIDATION] allowed=false exclusion_reason=missing_approval target=%s", draft.target_url)
        return False, "אי אפשר לפרסם כי חסר אישור"
    if not all([draft.target_site_section, draft.target_publish_type, draft.target_path, draft.target_url]):
        logger.info("[BLOG VALIDATION] allowed=false exclusion_reason=missing_target target=%s", draft.target_url)
        return False, "אי אפשר לפרסם כי חסרים פרטי יעד"
    if draft.target_site_section != "blog" or draft.target_publish_type != "article" or not _blog_target_allowed():
        logger.info(
            "[BLOG VALIDATION] allowed=false exclusion_reason=target_not_under_blog target_path=%s target_url=%s",
            draft.target_path,
            draft.target_url,
        )
        return False, "אי אפשר לפרסם כי היעד אינו תחת /blog/"
    if not adapter_ready:
        logger.info("[BLOG VALIDATION] allowed=false exclusion_reason=adapter_not_ready target=%s", draft.target_url)
        return False, "פרסום לבלוג עדיין לא מוגדר במערכת"
    logger.info("[BLOG VALIDATION] allowed=true exclusion_reason=None target=%s", draft.target_url)
    return True, None


def _blog_publish_adapter_ready() -> bool:
    token = get_istore_token()
    project_or_company_id = getattr(settings, "istore_project_id", None) or getattr(settings, "istore_company_id", None)
    return bool(getattr(settings, "istore_base_url", None) and token and project_or_company_id)

def _get_content_draft_or_404(db: Session, draft_id: int) -> ContentArticleDraft:
    draft = db.get(ContentArticleDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Content draft not found")
    return draft


def _bulk_approve_simple_safe_fixes(db: Session, fix_ids: list[int]) -> dict[str, object]:
    fixes = db.query(IStoreSEOApproval).filter(IStoreSEOApproval.id.in_(fix_ids)).all() if fix_ids else []
    approved: list[int] = []
    skipped: list[int] = []
    for fix in fixes:
        if not _is_simple_bulk_safe_fix(fix):
            skipped.append(fix.id)
            continue
        approve_istore_approval_fix(db, fix, approved_by="simple-workspace")
        approved.append(fix.id)
    return {
        "approved_count": len(approved),
        "skipped_count": len(skipped),
        "approved_fix_ids": approved,
        "skipped_fix_ids": skipped,
    }


@router.get("/auth/google/start")
def google_oauth_start() -> RedirectResponse:
    """Redirect the user to Google's OAuth consent screen for GSC and GA4 read access."""
    client_id, _client_secret, redirect_uri = _require_google_oauth_settings()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")


@router.get("/auth/google/callback")
def google_oauth_callback(db: DatabaseSession, code: str | None = None, error: str | None = None) -> dict[str, object]:
    """Exchange a Google OAuth code and store the returned user token."""
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google OAuth failed: {error}")
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Google OAuth code.")
    client_id, client_secret, redirect_uri = _require_google_oauth_settings()
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google OAuth token exchange failed.")
    payload = response.json()
    if not payload.get("access_token"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google OAuth response did not include an access token.",
        )
    token = _store_google_oauth_token(db, payload)
    return {"connected": True, "provider": token.provider, "scopes": token.scopes}


@router.get("/auth/google/status")
def google_oauth_status(db: DatabaseSession) -> dict[str, object]:
    """Return whether a Google user OAuth connection has been stored."""
    return oauth_status(db)


@router.get("/health")
def health() -> dict[str, str]:
    """Return a lightweight health check response."""
    return {"status": "ok", "service": settings.app_name}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the SEO operations dashboard as the app home page."""
    return templates.TemplateResponse(request, "dashboard.html", _operations_view_context(db, legacy_root_markers=True))


@router.get("/seo/operations-view", response_class=HTMLResponse)
def seo_operations_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the dashboard-safe SEO operations view with in-place actions."""
    return templates.TemplateResponse(request, "dashboard.html", _operations_view_context(db))


@router.get("/seo/simple-workspace", response_class=HTMLResponse)
def seo_simple_workspace(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render a jargon-free employee SEO review workspace."""
    return templates.TemplateResponse(request, "seo_simple_workspace.html", _simple_workspace_context(db))


@router.post("/seo/simple-workspace/{fix_id}/verify-live")
def verify_simple_workspace_fix_live(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    fix = _get_istore_approval_or_404(db, fix_id)
    if not (fix.target_url and fix.proposed_value):
        return {"success": False, "message": "לא נמצאו נתונים מספיקים לאימות"}
    html = requests.get(fix.target_url, timeout=20).text
    title = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    meta = re.search(
        r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'](.*?)[\"']",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    og = re.search(
        r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"'](.*?)[\"']",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    candidate = " ".join(
        [
            title.group(1).strip() if title else "",
            meta.group(1).strip() if meta else "",
            og.group(1).strip() if og else "",
        ]
    )
    ok = fix.proposed_value.strip() in candidate
    if ok:
        fix.status = "VERIFIED"
        db.add(fix)
        db.commit()
        return {"success": True, "message": "השינוי מופיע באתר"}
    return {"success": False, "message": "השינוי עדיין לא מופיע באתר — ייתכן שיש cache."}


@router.post("/seo/simple-workspace/bulk-approve")
def seo_simple_bulk_approve(payload: SimpleBulkApprovalRequest, db: DatabaseSession) -> dict[str, object]:
    """Approve only fixes that pass the existing mapping and safe-field gates; never publish automatically."""
    if not payload.confirmed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Approval confirmation is required.")
    return _bulk_approve_simple_safe_fixes(db, payload.fix_ids)




@router.post("/content/articles/internal-links/refresh-index")
def refresh_article_internal_link_index() -> dict[str, object]:
    stats = refresh_internal_link_index()
    return {"success": True, "message": "אינדקס קישורים רוענן", **stats}

@router.post("/content/articles/generate-daily-draft")
def generate_daily_content_article(db: DatabaseSession) -> dict[str, object]:
    draft, reused, last_generated_at = generate_daily_article_draft(db)
    _set_active_manual_article(db, draft)
    db.commit()
    db.refresh(draft)
    response = _article_generation_response(draft, "/content/articles/generate-daily-draft")
    response.update({"reused": reused, "last_generated_at": last_generated_at.isoformat() if last_generated_at else None, "auto_publish": False})
    return response


@router.post("/content/articles/generate-random-daily-draft")
def generate_random_daily_content_article(db: DatabaseSession) -> dict[str, object]:
    draft, reused, _ = generate_daily_article_draft(db, randomize=True)
    _set_active_manual_article(db, draft)
    db.commit()
    db.refresh(draft)
    response = _article_generation_response(draft, "/content/articles/generate-random-daily-draft")
    response.update({"selected_topic": draft.topic_title, "reused": reused, "draft_id": draft.id, "title": draft.title, "slug": draft.slug, "quality_score": response["draft"]["quality"].get("article_quality_score")})
    return response


@router.post("/content/articles/generate-topic-draft")
def generate_topic_content_article(payload: ManualTopicArticleRequest, db: DatabaseSession) -> dict[str, object]:
    topic = payload.topic_title[0] if isinstance(payload.topic_title, list) else payload.topic_title
    draft = generate_topic_article_draft(
        db,
        topic_title=topic,
        focus_keyword=payload.focus_keyword,
        target_intent=payload.target_intent,
        preferred_slug=payload.preferred_slug,
    )
    _set_active_manual_article(db, draft)
    db.commit()
    db.refresh(draft)
    response = _article_generation_response(draft, "/content/articles/generate-topic-draft")
    quality = response["draft"]["quality"]
    manual_upload_url = f"/seo/simple-workspace#article-{draft.id}"
    logger.info(
        "[MANUAL_SINGLE_ARTICLE_GENERATION] topic=%s keyword=%s generated_slug=%s draft_id=%s",
        topic,
        payload.focus_keyword,
        draft.slug,
        draft.id,
    )
    full_draft = {
        **response["draft"],
        "draft_id": draft.id,
        "quality": quality,
        "manual_upload_url": manual_upload_url,
        "debug": {**response["draft"]["debug"], "endpoint_used": "/content/articles/generate-topic-draft"},
    }
    return {
        **response,
        "auto_publish": False,
        "draft": full_draft,
    }


@router.get("/seo/content-articles/latest-debug")
def latest_content_article_debug(db: DatabaseSession) -> dict[str, object]:
    latest = db.query(ContentArticleDraft).order_by(ContentArticleDraft.created_at.desc(), ContentArticleDraft.id.desc()).first()
    active = db.query(ContentArticleDraft).filter(ContentArticleDraft.is_active_manual_article.is_(True)).order_by(ContentArticleDraft.created_at.desc(), ContentArticleDraft.id.desc()).first()
    if latest is None:
        return {"latest_article_id": None, "active_article_id": None}
    debug = _draft_debug(latest, "title")
    return {
        "latest_article_id": latest.id,
        "active_article_id": active.id if active else None,
        "title": latest.title,
        "slug": latest.slug,
        "generator_version": debug.get("generator_version"),
        "generator_source": debug.get("generator_source"),
        "selected_generator": debug.get("selected_generator"),
        "created_at": latest.created_at.isoformat() if latest.created_at else None,
    }


@router.post("/content/articles/{draft_id}/set-active")
def set_active_content_draft(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    _set_active_manual_article(db, draft)
    db.commit()
    db.refresh(draft)
    return {"success": True, "draft": draft.to_dict()}


@router.post("/content/articles/{draft_id}/archive-manual-work")
def archive_manual_content_draft(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    draft.is_active_manual_article = False
    if draft.status in {"CONTENT_DRAFT", "READY_FOR_REVIEW"}:
        draft.status = "REJECTED"
    db.add(draft)
    candidate = _latest_active_candidate(db)
    if candidate and candidate.id != draft.id:
        _set_active_manual_article(db, candidate)
    db.commit()
    return {"success": True, "draft": draft.to_dict()}


@router.get("/content/articles/drafts")
def list_content_drafts(db: DatabaseSession) -> dict[str, object]:
    drafts = db.query(ContentArticleDraft).order_by(ContentArticleDraft.created_at.desc()).all()
    return {"drafts": [d.to_dict() for d in drafts]}


@router.get("/content/articles/{draft_id}")
def get_content_draft(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    return {"draft": {**draft.to_dict(), "debug": _draft_debug(draft, "title"), "quality": _article_quality_summary(draft)}}


@router.post("/content/articles/{draft_id}/edit")
def edit_content_draft(draft_id: int, payload: ContentArticleEditRequest, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    for field in ("title", "slug", "meta_title", "meta_description", "article_body"):
        value = getattr(payload, field)
        if value is not None:
            setattr(draft, field, value)
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {"success": True, "draft": draft.to_dict()}


@router.post("/content/articles/{draft_id}/approve")
def approve_content_draft(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    if not _content_quality_gate_passed(draft):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="אי אפשר לאשר מאמר כי איכות/רלוונטיות נמוכה מדי",
        )
    draft.status = "APPROVED"
    db.add(draft)
    db.commit()
    return {"success": True, "draft": draft.to_dict(), "publish_allowed": True}


@router.post("/content/articles/{draft_id}/reject")
def reject_content_draft(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    draft.status = "REJECTED"
    db.add(draft)
    db.commit()
    return {"success": True, "draft": draft.to_dict()}


@router.post("/content/articles/{draft_id}/publish")
def publish_content_draft(draft_id: int, db: DatabaseSession, dry_run: bool = False) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    if not _content_quality_gate_passed(draft):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="אי אפשר לאשר מאמר כי איכות/רלוונטיות נמוכה מדי",
        )
    adapter_ready = _blog_publish_adapter_ready()
    allowed, blocked_reason = _article_publish_validation(draft, adapter_ready)
    dry_payload = {
        "target_url": draft.target_url,
        "target_path": draft.target_path,
        "title": draft.title,
        "slug": draft.slug,
        "meta_title": draft.meta_title,
        "meta_description": draft.meta_description,
        "body_length": len(draft.article_body or ""),
        "internal_links_count": len(draft.to_dict().get("internal_links", [])),
        "suggested_products_count": len(draft.to_dict().get("suggested_related_products", [])),
        "image_metadata_count": len(
            [
                x
                for x in [
                    draft.featured_image_prompt,
                    draft.image_alt_text,
                    draft.image_title,
                    draft.image_caption,
                    draft.image_filename_slug,
                ]
                if x
            ]
        ),
        "approved": draft.status == "APPROVED",
        "allowed": allowed,
        "blocked_reason": blocked_reason,
        "publish_adapter": "istore_blog_content_adapter",
        "destination_under_blog": bool(draft.target_url and draft.target_url.startswith("https://compassgrill.co.il/blog/")),
    }
    if dry_run:
        contract: dict[str, object] | None = None
        try:
            contract = IStoreBlogPublisher.from_settings().publish(draft, dry_run=True).get("request_contract")
        except IStoreBlogPublishError:
            contract = None
        return {
            "success": True,
            "dry_run": True,
            "result_he": "בדיקת פרסום יבשה בוצעה",
            "request_contract": contract,
            **dry_payload,
        }
    if not allowed:
        raise HTTPException(status_code=400, detail=blocked_reason)
    if not adapter_ready:
        raise HTTPException(status_code=400, detail="פרסום לבלוג עדיין לא פעיל — ניתן לבצע בדיקת פרסום יבשה בלבד")
    try:
        result = IStoreBlogPublisher.from_settings().publish(draft)
    except IStoreBlogPublishError as exc:
        raise HTTPException(status_code=400, detail=f"פרסום נכשל: {exc}") from exc

    external_content_id = str(result.get("external_content_id") or "").strip()
    if bool(result.get("minimal_payload_test")):
        return {
            "success": True,
            "published": False,
            "publish_status": draft.status,
            "verification_status": draft.verification_status,
            "external_content_id": external_content_id,
            "result_he": "ISTORE minimal create test succeeded; full article payload still needs investigation.",
            "publish_adapter": "IStoreBlogPublisher",
            "publish_result": result,
            "draft": draft.to_dict(),
        }

    live_url = str(result.get("live_url") or "").strip()
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    verified_ok = bool(verification.get("title_found")) and int(verification.get("status_code", 0)) == 200
    if not external_content_id or not live_url or not verified_ok:
        raise HTTPException(
            status_code=400,
            detail="פרסום נכשל: חסר external_content_id או אימות URL ציבורי נכשל",
        )

    draft.status = "PUBLISHED"
    draft.published_at = datetime.now(UTC)
    draft.published_url = live_url
    draft.verification_status = "VERIFIED"
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {
        "success": True,
        "published": True,
        "publish_status": draft.status,
        "published_url": draft.published_url,
        "published_at": draft.published_at.isoformat(),
        "verification_status": draft.verification_status,
        "result_he": "פורסם בהצלחה",
        "publish_adapter": "IStoreBlogPublisher",
        "publish_result": result,
        "draft": draft.to_dict(),
    }






@router.get("/debug/internal-link-match")
def debug_internal_link_match(query: str, db: DatabaseSession) -> dict[str, object]:
    from app.services.content_articles import _discover_related_links

    matches, debug = _discover_related_links(db, query, limit=10)
    return {"query": query, "debug": debug, "matches": matches}
@router.get("/debug/istore/browser-status")
def debug_istore_browser_status() -> dict[str, object]:
    """Run ISTORE admin browser session check without submitting forms."""
    return check_istore_browser_status().to_dict()

@router.get("/debug/istore/create-dry-run")
def debug_istore_create_dry_run(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    dry_run = IStoreBlogPublisher.from_settings().publish(draft, dry_run=True)
    contract = dry_run.get("request_contract") if isinstance(dry_run, dict) else {}
    if not isinstance(contract, dict):
        contract = {}
    return {
        "endpoint": contract.get("endpoint"),
        "method": contract.get("method"),
        "headers": contract.get("headers", {}),
        "payload": contract.get("payload", {}),
        "minimal_payload": contract.get("minimal_payload", False),
        "payload_description_length": contract.get("payload_description_length", 0),
        "payload_title_length": contract.get("payload_title_length", 0),
        "estimated_json_length": contract.get("estimated_json_length", 0),
        "cookie_names": contract.get("cookie_names", []),
        "xsrf_length": contract.get("xsrf_length", 0),
    }


@router.post("/debug/istore/browser-create-test")
def debug_istore_browser_create_test(draft_id: int, db: DatabaseSession, dry_run: bool = True) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    payload = {
        "title": draft.title,
        "description": draft.article_body,
        "meta_title": draft.meta_title,
        "meta_description": draft.meta_description,
        "slug": draft.slug,
        "status": None,
        "is_blog": None,
    }
    result = create_shop_information_page(payload=payload, dry_run=dry_run).to_dict()
    return {
        "success": bool(result.get("success")),
        "current_url": result.get("current_url", ""),
        "external_content_id": result.get("external_content_id"),
        "otp_required": bool(result.get("otp_required", False)),
        "error": result.get("error"),
        "screenshot_path": result.get("screenshot_path"),
        "selector_availability": result.get("selector_availability"),
        "planned_fields": result.get("planned_fields"),
        "dom_diagnostics": result.get("dom_diagnostics"),
        "dry_run": dry_run,
    }


@router.get("/content/articles/calendar")
def content_calendar(db: DatabaseSession) -> dict[str, object]:
    drafts = db.query(ContentArticleDraft).order_by(ContentArticleDraft.created_at.desc()).limit(60).all()
    history = [
        {"date": d.created_at.date().isoformat(), "topic": d.topic_title, "keyword": d.focus_keyword}
        for d in drafts
    ]
    return {"history": history}


def _build_hebrew_insights_payload(db: Session, enrich: bool = False) -> dict[str, object]:
    """Build Hebrew-native SEO intelligence for the latest compassgrill.co.il ecommerce crawl."""
    crawled_pages = _latest_crawl_pages(db)
    excluded_pages = [
        {"url": page.url, "excluded_reason": get_url_exclusion_reason(page.url)}
        for page in crawled_pages
        if get_url_exclusion_reason(page.url)
    ]
    pages = [page for page in crawled_pages if is_seo_eligible_url(page.url)]
    metrics_by_url = _gsc_metrics_by_url(db, [page.url for page in pages]) if pages else {}
    insights = [analyze_page_hebrew_seo(page, metrics_by_url.get(page.url)) for page in pages]
    ai_enrichment: dict[str, object] = {"enabled": False, "error": None, "recommendations": []}
    if enrich and insights:
        try:
            ai_enrichment = OpenAIClient().generate_hebrew_seo_enrichment(insights=insights[:10])
            ai_enrichment["enabled"] = True
        except RuntimeError as exc:
            ai_enrichment = {"enabled": False, "error": str(exc), "recommendations": []}
    return {
        "success": True,
        "target_domain": settings.target_domain,
        "supported_site": "compassgrill.co.il",
        "summary": summarize_hebrew_insights(insights),
        "seasonality": israeli_seasonality(),
        "insights": insights,
        "excluded_pages": excluded_pages,
        "openai_enrichment": ai_enrichment,
    }


@router.get("/seo/hebrew-insights")
def hebrew_seo_insights(db: DatabaseSession, enrich: bool = False) -> dict[str, object]:
    """Return Hebrew SEO intelligence for Israeli ecommerce and compassgrill.co.il structures."""
    return _build_hebrew_insights_payload(db, enrich=enrich)


@router.get("/seo/hebrew-insights-view", response_class=HTMLResponse)
def hebrew_seo_insights_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render Hebrew SEO intelligence for Israeli ecommerce."""
    payload = _build_hebrew_insights_payload(db, enrich=False)
    return templates.TemplateResponse(request, "hebrew_insights.html", payload)


@router.get("/seo/scheduler/configs")
def list_seo_scheduler_configs(db: DatabaseSession) -> dict[str, object]:
    """Return safe SEO scheduler configs, creating the default disabled config if needed."""
    ensure_default_schedule_config(db)
    configs = db.query(SEOScheduleConfig).order_by(SEOScheduleConfig.enabled.desc(), SEOScheduleConfig.id.asc()).all()
    return {"configs": [config.to_dict() for config in configs]}


@router.post("/seo/scheduler/configs", status_code=status.HTTP_201_CREATED)
def create_seo_scheduler_config(
    db: DatabaseSession,
    payload: Annotated[SEOScheduleConfigCreate | None, Body()] = None,
    name: str = "Daily SEO Automation",
    frequency: str = "daily",
    hour_utc: int = 5,
    max_tasks: int = 10,
    generate_articles: bool = False,
    sync_gsc: bool = True,
    enabled: bool = False,
) -> dict[str, object]:
    """Create a disabled-by-default scheduler config for safe SEO preparation work."""
    values = (
        payload.model_dump()
        if payload is not None
        else {
            "name": name,
            "frequency": frequency,
            "hour_utc": hour_utc,
            "max_tasks": max_tasks,
            "generate_articles": generate_articles,
            "sync_gsc": sync_gsc,
            "enabled": enabled,
        }
    )
    try:
        config = create_schedule_config(
            db,
            name=values["name"],
            frequency=values["frequency"],
            hour_utc=values["hour_utc"],
            max_tasks=values["max_tasks"],
            generate_articles=values["generate_articles"],
            sync_gsc=values["sync_gsc"],
            enabled=values["enabled"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"config": config.to_dict()}


@router.post("/seo/scheduler/configs/{config_id}/enable")
def enable_seo_scheduler_config(config_id: int, db: DatabaseSession) -> dict[str, object]:
    """Enable a scheduler config without running it immediately."""
    config = set_schedule_enabled(db, _get_schedule_config_or_404(db, config_id), True)
    return {"config": config.to_dict()}


@router.post("/seo/scheduler/configs/{config_id}/disable")
def disable_seo_scheduler_config(config_id: int, db: DatabaseSession) -> dict[str, object]:
    """Disable a scheduler config without changing previous automation runs."""
    config = set_schedule_enabled(db, _get_schedule_config_or_404(db, config_id), False)
    return {"config": config.to_dict()}


@router.post("/seo/scheduler/run-due")
def run_due_seo_scheduler_configs(db: DatabaseSession) -> dict[str, object]:
    """Run due schedules through the safe automation pipeline only; no publishing is performed."""
    return run_due_schedules(db)


@router.get("/seo/scheduler-view", response_class=HTMLResponse)
def seo_scheduler_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render enabled and disabled scheduler configs plus related automation runs."""
    ensure_default_schedule_config(db)
    configs = db.query(SEOScheduleConfig).order_by(SEOScheduleConfig.enabled.desc(), SEOScheduleConfig.id.asc()).all()
    return templates.TemplateResponse(
        request,
        "seo_scheduler.html",
        {
            "enabled_configs": [config for config in configs if config.enabled],
            "disabled_configs": [config for config in configs if not config.enabled],
            "runs": _scheduled_automation_runs(db),
        },
    )


@router.post("/seo/automation/run", status_code=status.HTTP_201_CREATED)
def run_seo_automation_endpoint(
    db: DatabaseSession,
    max_tasks: int = 10,
    generate_articles: bool = False,
    sync_gsc: bool = True,
) -> dict[str, object]:
    """Run the safe SEO automation workflow without approving, applying, or publishing changes."""
    automation_run = run_seo_automation(db, max_tasks=max_tasks, generate_articles=generate_articles, sync_gsc=sync_gsc)
    return {"success": automation_run.status != "failed", "run": automation_run.to_dict()}


@router.get("/seo/automation/runs")
def list_seo_automation_runs(db: DatabaseSession) -> dict[str, object]:
    """Return previous safe SEO automation workflow runs."""
    runs = db.query(SEOAutomationRun).order_by(SEOAutomationRun.started_at.desc(), SEOAutomationRun.id.desc()).all()
    return {"runs": [run.to_dict() for run in runs]}


@router.get("/seo/automation/runs/{run_id}")
def get_seo_automation_run(run_id: int, db: DatabaseSession) -> dict[str, object]:
    """Return full details for one safe SEO automation workflow run."""
    automation_run = db.get(SEOAutomationRun, run_id)
    if not automation_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO automation run not found")
    return {"run": automation_run.to_dict()}


@router.get("/seo/automation-view", response_class=HTMLResponse)
def seo_automation_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the safe one-click SEO automation dashboard."""
    runs = (
        db.query(SEOAutomationRun)
        .order_by(SEOAutomationRun.started_at.desc(), SEOAutomationRun.id.desc())
        .limit(25)
        .all()
    )
    return templates.TemplateResponse(request, "seo_automation.html", {"runs": runs})


@router.post("/gsc/sync")
def sync_gsc_keywords(db: DatabaseSession) -> dict[str, object]:
    """Fetch Search Console keyword rows and upsert them into the local keyword intelligence table."""
    try:
        client = GSCClient.from_settings(db)
        rows = client.fetch_top_queries(client.site_url, limit=250)
    except (MissingGSCCredentialsError, GSCAPIError, RuntimeError, ValueError) as exc:
        return {"success": False, "rows_synced": 0, "top_queries": [], "error": str(exc)}

    rows_synced = 0
    for row in rows:
        if _upsert_gsc_metric(db, row):
            rows_synced += 1
    db.commit()
    top_queries = (
        db.query(GSCKeywordMetric)
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc())
        .limit(10)
        .all()
    )
    return {"success": True, "rows_synced": rows_synced, "top_queries": [_metric_payload(row) for row in top_queries]}


@router.get("/gsc/keywords")
def list_gsc_keywords(
    db: DatabaseSession,
    page_url: str | None = None,
    query: str | None = None,
    min_impressions: int | None = None,
    max_position: float | None = None,
    low_ctr_only: bool = False,
) -> dict[str, object]:
    """Return stored GSC keyword metrics with optional page, query, impression, position, and CTR filters."""
    metrics = (
        _gsc_keyword_query(db, page_url, query, min_impressions, max_position, low_ctr_only)
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc())
        .limit(500)
        .all()
    )
    return {"success": True, "keywords": [_metric_payload(metric) for metric in metrics]}


@router.get("/gsc/opportunities")
def gsc_opportunities(db: DatabaseSession) -> dict[str, object]:
    """Return keyword opportunities from high-impression, low-CTR, mid-ranking GSC rows."""
    return {"success": True, "opportunities": _gsc_opportunities(db)}


@router.get("/gsc/keywords-view", response_class=HTMLResponse)
def gsc_keywords_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render GSC keyword dashboard tables."""
    top_keywords = (
        db.query(GSCKeywordMetric)
        .order_by(GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc())
        .limit(50)
        .all()
    )
    low_ctr = (
        db.query(GSCKeywordMetric)
        .filter(GSCKeywordMetric.ctr < LOW_CTR_THRESHOLD, GSCKeywordMetric.impressions >= HIGH_IMPRESSIONS_THRESHOLD)
        .order_by(GSCKeywordMetric.impressions.desc())
        .limit(25)
        .all()
    )
    rising_pages = (
        db.query(GSCKeywordMetric)
        .filter(GSCKeywordMetric.average_position <= 10)
        .order_by(GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.impressions.desc())
        .limit(25)
        .all()
    )
    weak_ranking_pages = (
        db.query(GSCKeywordMetric)
        .filter(
            GSCKeywordMetric.average_position >= WEAK_RANKING_MIN,
            GSCKeywordMetric.average_position <= WEAK_RANKING_MAX,
        )
        .order_by(GSCKeywordMetric.impressions.desc())
        .limit(25)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "gsc_keywords.html",
        {
            "top_keywords": top_keywords,
            "low_ctr": low_ctr,
            "rising_pages": rising_pages,
            "weak_ranking_pages": weak_ranking_pages,
        },
    )


@router.get("/gsc/opportunities-view", response_class=HTMLResponse)
def gsc_opportunities_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render GSC opportunity recommendations for human review."""
    return templates.TemplateResponse(
        request,
        "gsc_opportunities.html",
        {"opportunities": _gsc_opportunities(db), "low_ctr_threshold": LOW_CTR_THRESHOLD},
    )


@router.post("/seo/strategy/run")
def run_seo_strategy_engine(db: DatabaseSession) -> dict[str, object]:
    """Analyze the current SEO state and generate/update prioritized strategy recommendations."""
    return generate_strategy_recommendations(db)


@router.get("/seo/strategy/recommendations")
def list_seo_strategy_recommendations(
    db: DatabaseSession,
    status: str | None = None,
    recommendation_type: str | None = None,
    min_priority: float | None = None,
) -> dict[str, object]:
    """Return SEO strategy recommendations with status, type, and minimum-priority filters."""
    query = db.query(SEOStrategyRecommendation)
    if status:
        if status not in SEO_STRATEGY_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid SEO strategy recommendation status")
        query = query.filter(SEOStrategyRecommendation.status == status)
    if recommendation_type:
        if recommendation_type not in SEO_STRATEGY_RECOMMENDATION_TYPES:
            raise HTTPException(status_code=400, detail="Invalid SEO strategy recommendation type")
        query = query.filter(SEOStrategyRecommendation.recommendation_type == recommendation_type)
    if min_priority is not None:
        query = query.filter(SEOStrategyRecommendation.priority_score >= min_priority)
    recommendations = query.order_by(
        SEOStrategyRecommendation.priority_score.desc(), SEOStrategyRecommendation.id.desc()
    ).all()
    return {"success": True, "recommendations": [recommendation.to_dict() for recommendation in recommendations]}


@router.get("/seo/strategy/summary")
def seo_strategy_summary(db: DatabaseSession) -> dict[str, object]:
    """Return the summarized site-level SEO strategy from pending recommendations."""
    return {"success": True, "summary": summarize_site_strategy(db)}


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


@router.get("/seo/strategy-view", response_class=HTMLResponse)
def seo_strategy_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the prioritized SEO strategy recommendations dashboard."""
    recommendations = (
        db.query(SEOStrategyRecommendation)
        .order_by(SEOStrategyRecommendation.priority_score.desc(), SEOStrategyRecommendation.id.desc())
        .limit(100)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "seo_strategy.html",
        {"recommendations": recommendations, "summary": summarize_site_strategy(db)},
    )


@router.get("/seo/strategy-summary-view", response_class=HTMLResponse)
def seo_strategy_summary_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the site-level SEO strategy summary dashboard."""
    return templates.TemplateResponse(request, "seo_strategy_summary.html", {"summary": summarize_site_strategy(db)})


@router.get("/seo/internal-link-opportunities-view", response_class=HTMLResponse)
def internal_link_opportunities_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render deterministic internal link opportunities from the latest crawl."""
    pages = [page for page in _latest_crawl_pages(db) if is_seo_eligible_url(page.url)]
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in pages])
    gsc_by_url = _gsc_metrics_by_url(db, [page.url for page in pages])
    return templates.TemplateResponse(
        request,
        "internal_link_opportunities.html",
        {
            "opportunities": _build_internal_link_opportunities(pages, tasks_by_url, gsc_by_url),
            "pages_analyzed": len(pages),
        },
    )


@router.get("/seo/topical-clusters-view", response_class=HTMLResponse)
def topical_clusters_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render topical cluster summaries from the latest crawl."""
    pages = [page for page in _latest_crawl_pages(db) if is_seo_eligible_url(page.url)]
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


@router.get("/crawler/results-view/latest", response_class=HTMLResponse)
def latest_crawler_results_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the latest crawl results as readable HTML instead of a raw API payload."""
    latest_run, pages = _latest_crawl_context(db)
    return templates.TemplateResponse(
        request,
        "crawler_results_latest.html",
        {"crawl_run": latest_run, "pages": pages, "target_domain": settings.target_domain},
    )


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
    gsc_by_url = _gsc_metrics_by_url(db, [page.url for page in pages])
    candidates = [
        page
        for page in pages
        if is_seo_eligible_url(page.url)
        and (_seo_task_candidate(page) or _keyword_opportunity_score(gsc_by_url.get(page.url)) >= 55)
    ]
    candidates = sorted(
        candidates,
        key=lambda page: (-_keyword_opportunity_score(gsc_by_url.get(page.url)), page.seo_score, page.url),
    )
    existing_urls = {
        page_url
        for (page_url,) in db.query(SEOTask.page_url)
        .filter(SEOTask.page_url.in_([page.url for page in candidates]))
        .all()
    }
    new_tasks = [
        _build_task_from_page(page, gsc_by_url.get(page.url)) for page in candidates if page.url not in existing_urls
    ]
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
    _raise_if_url_excluded(task.page_url)
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


@router.post("/seo/fixes/generate-from-latest-crawl", status_code=status.HTTP_201_CREATED)
def generate_seo_fixes_from_latest_crawl(
    db: DatabaseSession, payload: Annotated[SEOAutoFixGenerationRequest | None, Body()] = None
) -> dict[str, object]:
    """Generate human-reviewable SEO fixes from the latest crawl without publishing anything."""
    request_payload = payload or SEOAutoFixGenerationRequest()
    return generate_fixes_from_latest_crawl(
        db,
        AutoFixOptions(
            limit=request_payload.limit,
            min_risk_level=request_payload.min_risk_level,
            page_type=request_payload.page_type,
            dry_run=request_payload.dry_run,
        ),
    )


@router.post("/seo/fixes/verify-istore-mappings")
def verify_istore_fix_mappings(db: DatabaseSession) -> dict[str, object]:
    """Verify crawler SEO fixes against real ISTORE product IDs before any publish can occur."""
    try:
        return verify_pending_istore_mappings(db)
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/seo/fixes/pending")
def pending_seo_fixes(db: DatabaseSession, limit: int = 250) -> dict[str, object]:
    """Return pending auto-fix drafts grouped and sorted for human review."""
    return pending_fixes_review(db, limit=limit)


@router.get("/seo/fixes/pending-view", response_class=HTMLResponse)
def pending_seo_fixes_view(request: Request, db: DatabaseSession, limit: int = 250) -> HTMLResponse:
    """Render pending fixes in a review table with dashboard-safe action controls."""
    review = pending_fixes_review(db, limit=limit)
    return templates.TemplateResponse(request, "seo_pending_fixes.html", {"review": review, "fixes": review["fixes"]})


@router.post("/seo/fixes/verify-against-latest-crawl")
def verify_seo_fixes_against_latest_crawl(db: DatabaseSession) -> dict[str, object]:
    """Compare pending/approved fixes with the latest crawl without publishing or changing safety gates."""
    latest_run, pages = _latest_crawl_context(db)
    latest_urls = {page.url for page in pages}
    fixes = (
        db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status.in_(["PENDING_APPROVAL", "APPROVED", "READY_FOR_MANUAL_PUBLISH"]))
        .order_by(IStoreSEOApproval.priority_score.desc(), IStoreSEOApproval.id.desc())
        .all()
    )
    verified = [fix for fix in fixes if (fix.target_url or fix.target_id) in latest_urls]
    warnings = [] if latest_run else ["No crawler runs found; verification used no crawl data."]
    return {
        "success": True,
        "crawl_run_id": latest_run.id if latest_run else None,
        "pages_crawled": latest_run.pages_crawled if latest_run else 0,
        "average_score": latest_run.average_score if latest_run else 0,
        "pending_fixes_count": db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status == "PENDING_APPROVAL")
        .count(),
        "verified_count": len(verified),
        "warnings": warnings,
        "safety": {"auto_publish": False, "changed_publish_gates": False},
        "fixes": [fix_to_review_dict(fix) for fix in verified[:10]],
    }


@router.post("/seo/fixes/invalidate-stale")
def invalidate_stale_seo_fixes(db: DatabaseSession) -> dict[str, object]:
    return {"success": True, **invalidate_stale_drafts(db, reason="manual_stale_cleanup")}


@router.post("/seo/fixes/regenerate-stale")
def regenerate_stale_seo_fixes(db: DatabaseSession) -> dict[str, object]:
    return {"success": True, **regenerate_stale_drafts(db)}


@router.get("/seo/fixes/stale")
def list_stale_seo_fixes(db: DatabaseSession) -> dict[str, object]:
    fixes = stale_drafts(db)
    return {"count": len(fixes), "fixes": [fix_to_review_dict(fix) for fix in fixes]}


@router.get("/seo/fixes/fresh")
def list_fresh_seo_fixes(db: DatabaseSession) -> dict[str, object]:
    fixes = fresh_drafts(db)
    return {"count": len(fixes), "fixes": [fix_to_review_dict(fix) for fix in fixes]}


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
    _raise_if_url_excluded(fix.page_url)

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


@router.post("/istore/sync-products")
def sync_istore_product_catalog(db: DatabaseSession) -> dict[str, object]:
    """Synchronize the local ISTORE product catalog without publishing changes."""
    try:
        return sync_istore_products(db)
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/istore/enrich-seo-fields")
def enrich_istore_product_seo_fields(db: DatabaseSession) -> dict[str, object]:
    """Fetch full ISTORE product records and enrich local SEO catalog fields."""
    try:
        return enrich_istore_seo_fields(db)
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/istore/products/missing-seo")
def missing_istore_product_seo_fields(db: DatabaseSession, limit: int = 100) -> dict[str, object]:
    """Return synced ISTORE products missing core SEO fields."""
    products = list_products_missing_seo(db, limit=limit)
    return {"products": [product.to_dict() for product in products], "count": len(products)}


@router.get("/istore/products")
def synced_istore_products(db: DatabaseSession, q: str | None = None, limit: int = 100) -> dict[str, object]:
    """Return synchronized ISTORE products for mapping review/manual assignment."""
    products = list_synced_products(db, q=q, limit=limit)
    return {
        "products": [product.to_dict() for product in products],
        "count": len(products),
        "publishable_threshold": PUBLISHABLE_CONFIDENCE_THRESHOLD,
    }


@router.post("/seo/fixes/{fix_id}/assign-product")
def assign_seo_fix_product(
    fix_id: int, db: DatabaseSession, payload: Annotated[IStoreAssignProductRequest, Body()]
) -> dict[str, object]:
    """Manually assign a synchronized ISTORE product to a fix without auto-publishing."""
    fix = _get_istore_approval_or_404(db, fix_id)
    try:
        candidate = assign_product_mapping(db, fix, payload.istore_product_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "success": True,
        "auto_publish": False,
        "mapping": {
            "istore_product_id": candidate.product_id,
            "mapping_confidence": candidate.confidence,
            "mapping_source": candidate.source,
        },
        "fix": fix_to_review_dict(fix),
    }


@router.post("/integrations/istore/seo-approvals/scan", status_code=status.HTTP_201_CREATED)
def scan_istore_seo_approvals(db: DatabaseSession, limit: int = 50) -> dict[str, object]:
    """Scan ISTORE products and latest crawled pages, storing proposed fixes as pending drafts only."""
    try:
        return scan_istore_seo_opportunities(db, limit=limit)
    except (MissingIStoreSettingsError, IStoreAPIError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/integrations/istore/seo-approvals/cleanup-english-fallbacks")
def cleanup_istore_english_fallback_drafts(db: DatabaseSession) -> dict[str, object]:
    """Mark pending English fallback drafts stale without deleting or publishing records."""
    return mark_english_fallback_drafts_stale(db)


@router.get("/integrations/istore/seo-approvals")
def list_istore_seo_approvals(db: DatabaseSession, status_filter: str | None = None) -> dict[str, object]:
    """Return semi-automatic ISTORE SEO fixes awaiting review or publication."""
    query = db.query(IStoreSEOApproval)
    if status_filter:
        query = query.filter(IStoreSEOApproval.status == status_filter)
    fixes = query.order_by(IStoreSEOApproval.created_at.desc(), IStoreSEOApproval.id.desc()).all()
    return {"fixes": [fix.to_dict() for fix in fixes]}


@router.get("/integrations/istore/seo-approvals-view", response_class=HTMLResponse)
def istore_seo_approvals_view(request: Request, db: DatabaseSession) -> HTMLResponse:
    """Render the human approval dashboard for ISTORE SEO changes."""
    fixes = db.query(IStoreSEOApproval).order_by(IStoreSEOApproval.created_at.desc(), IStoreSEOApproval.id.desc()).all()
    return templates.TemplateResponse(request, "istore_seo_approvals.html", {"fixes": fixes})


@router.get("/integrations/istore/seo-approvals/{fix_id}/preview")
def preview_istore_seo_approval(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Preview one proposed ISTORE SEO change without publishing."""
    fix = _get_istore_approval_or_404(db, fix_id)
    return {
        "fix": fix.to_dict(),
        "preview": preview_generated_content(fix, db),
        "rollback_preview": rollback_preview(fix),
    }


@router.post("/integrations/istore/seo-approvals/{fix_id}/draft")
def edit_istore_seo_approval_draft(
    fix_id: int, db: DatabaseSession, payload: Annotated[IStoreDraftEditRequest, Body()]
) -> dict[str, object]:
    """Update the proposed SEO draft text inline without approving or publishing it."""
    approval = _get_istore_approval_or_404(db, fix_id)
    if approval.status not in {"PENDING_APPROVAL", "APPROVED", "READY_FOR_MANUAL_PUBLISH"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only reviewable drafts can be edited")
    proposed_value = payload.proposed_value.strip()
    if not proposed_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="proposed_value cannot be empty")
    approval.proposed_value = proposed_value
    approval.approval_action = "draft_edited"
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return {"success": True, "updated": True, "auto_publish": False, "fix": fix_to_review_dict(approval)}


@router.post("/integrations/istore/seo-approvals/{fix_id}/verify-mapping")
def verify_single_istore_seo_mapping(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Run ISTORE mapping verification and return the selected fix state for inline review."""
    _get_istore_approval_or_404(db, fix_id)
    try:
        result = verify_pending_istore_mappings(db)
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    approval = _get_istore_approval_or_404(db, fix_id)
    return {**result, "fix": fix_to_review_dict(approval), "auto_publish": False}


@router.post("/integrations/istore/seo-approvals/{fix_id}/approve")
def approve_istore_seo_approval(
    fix_id: int, db: DatabaseSession, payload: Annotated[IStoreApprovalAction | None, Body()] = None
) -> dict[str, object]:
    """Approve a pending ISTORE SEO fix without publishing it."""
    action = payload or IStoreApprovalAction()
    fix = approve_istore_approval_fix(
        db, _get_istore_approval_or_404(db, fix_id), approved_by=action.approved_by, metadata=action.metadata
    )
    return {"success": True, "fix": fix.to_dict()}


@router.post("/integrations/istore/seo-approvals/{fix_id}/reject")
def reject_istore_seo_approval(
    fix_id: int, db: DatabaseSession, payload: Annotated[IStoreApprovalAction | None, Body()] = None
) -> dict[str, object]:
    """Reject a pending ISTORE SEO fix without publishing it."""
    action = payload or IStoreApprovalAction()
    fix = reject_istore_approval_fix(
        db, _get_istore_approval_or_404(db, fix_id), approved_by=action.approved_by, metadata=action.metadata
    )
    return {"success": True, "fix": fix.to_dict()}


@router.post("/integrations/istore/seo-approvals/{fix_id}/export")
def export_istore_content_draft(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Export one approved article/content draft for manual publishing; no ISTORE publish is sent."""
    try:
        return export_content_draft_for_manual_publish(db, _get_istore_approval_or_404(db, fix_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/integrations/istore/seo-approvals/{fix_id}/publish")
def publish_istore_seo_approval(
    fix_id: int, db: DatabaseSession, payload: Annotated[IStorePublishRequest | None, Body()] = None
) -> dict[str, object]:
    """Publish exactly one approved ISTORE SEO fix after explicit approval and safety gates."""
    request_payload = payload or IStorePublishRequest()
    try:
        return publish_approved_fix(
            db,
            _get_istore_approval_or_404(db, fix_id),
            approval_confirmed=request_payload.approval,
            dry_run=request_payload.dry_run,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/integrations/istore/seo-approvals/{fix_id}/rollback")
def rollback_istore_seo_approval(
    fix_id: int, db: DatabaseSession, payload: Annotated[IStorePublishRequest | None, Body()] = None
) -> dict[str, object]:
    """Rollback exactly one published ISTORE SEO fix after explicit approval and safety gates."""
    request_payload = payload or IStorePublishRequest()
    try:
        return rollback_published_fix(
            db,
            _get_istore_approval_or_404(db, fix_id),
            approval_confirmed=request_payload.approval,
            dry_run=request_payload.dry_run,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/integrations/istore/seo-approvals/{fix_id}/rollback-preview")
def istore_seo_rollback_preview(fix_id: int, db: DatabaseSession) -> dict[str, object]:
    """Return a rollback payload preview; rollback is never executed automatically."""
    return rollback_preview(_get_istore_approval_or_404(db, fix_id))


@router.post("/integrations/istore/seo-approvals/validate-payload")
def validate_istore_seo_payload(payload: IStorePayloadValidationRequest) -> dict[str, object]:
    """Validate that an ISTORE update payload contains only allowed SEO fields."""
    try:
        validate_istore_payload(payload.payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"valid": True}


@router.get("/seo/internal-link-opportunities")
def internal_link_opportunities(db: DatabaseSession) -> dict[str, object]:
    """Return internal link opportunities from the latest crawl and saved SEO task context."""
    pages = [page for page in _latest_crawl_pages(db) if is_seo_eligible_url(page.url)]
    tasks_by_url = _tasks_by_page_url(db, [page.url for page in pages])
    strong_pages_count = sum(1 for page in pages if page.status_code < 400 and authority_score(page) >= 60)
    weak_pages_count = sum(
        1 for page in pages if page.status_code < 400 and opportunity_score(page, tasks_by_url.get(page.url)) >= 45
    )
    gsc_by_url = _gsc_metrics_by_url(db, [page.url for page in pages])
    opportunities = _build_internal_link_opportunities(pages, tasks_by_url, gsc_by_url)

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
    pages = [page for page in _latest_crawl_pages(db) if is_seo_eligible_url(page.url)]
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
    _raise_if_url_excluded(task.page_url)

    payload = _task_recommendation_payload(task, db)
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
    _raise_if_url_excluded(task.page_url)

    if not _parse_task_recommendation(task):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SEO task recommendation is required")

    try:
        article = OpenAIClient().generate_full_article(task=_task_article_payload(task, db))
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
    _raise_if_url_excluded(task.page_url)
    if not _task_has_generated_article(task):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generated article is required")

    return _task_export_payload(task)


@router.get("/seo/tasks/{task_id}/export-view", response_class=HTMLResponse)
def export_seo_task_article_view(task_id: int, db: DatabaseSession) -> HTMLResponse:
    """Return a copy-friendly HTML export view for a generated SEO task article."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")
    _raise_if_url_excluded(task.page_url)
    if not _task_has_generated_article(task):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Generated article is required")

    return HTMLResponse(content=_task_export_view_html(_task_export_payload(task)))


@router.get("/seo/tasks/{task_id}/preview", response_class=HTMLResponse)
def preview_seo_task_article(task_id: int, db: DatabaseSession) -> HTMLResponse:
    """Return a simple HTML preview for a generated SEO task article."""
    task = db.get(SEOTask, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SEO task not found")
    _raise_if_url_excluded(task.page_url)
    return HTMLResponse(content=_task_article_preview_html(task))


@router.get("/integrations/gsc/status")
def gsc_status(db: DatabaseSession) -> dict[str, object]:
    """Validate Google Search Console credentials configuration."""
    try:
        return GSCClient.from_settings(db).status()
    except MissingGSCCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/integrations/ga4/status")
def ga4_status(db: DatabaseSession) -> dict[str, object]:
    """Validate GA4 credentials configuration."""
    try:
        return GA4Client.from_settings(db).status()
    except MissingGA4CredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/integrations/istore/status")
def istore_status() -> dict[str, object]:
    """Validate read-only ISTORE integration configuration without exposing secrets."""
    try:
        return IStoreClient.from_settings().status()
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/integrations/istore/products")
def istore_products() -> dict[str, object]:
    """Return live ISTORE products through the read-only client."""
    try:
        return {"products": IStoreClient.from_settings().list_products()}
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/integrations/istore/products/{product_id}/seo-analysis")
def istore_product_seo_analysis_view(product_id: str, request: Request) -> HTMLResponse:
    """Render read-only SEO recommendations for one ISTORE product."""
    try:
        product = IStoreClient.from_settings().get_product(product_id)
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if not isinstance(product, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ISTORE product response must be an object.",
        )
    analysis = analyze_istore_product_seo(product)
    return templates.TemplateResponse(
        request,
        "istore_product_seo_analysis.html",
        {"analysis": analysis.as_dict(), "product": product},
    )


@router.get("/integrations/istore/products/{product_id}/seo-analysis.json")
def istore_product_seo_analysis(product_id: str) -> dict[str, object]:
    """Return read-only SEO recommendations for one ISTORE product."""
    try:
        product = IStoreClient.from_settings().get_product(product_id)
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if not isinstance(product, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ISTORE product response must be an object.",
        )
    return {"product": product, "analysis": analyze_istore_product_seo(product).as_dict()}


@router.get("/integrations/istore/products/{product_id}")
def istore_product(product_id: str) -> dict[str, object]:
    """Return one ISTORE product through the read-only client."""
    try:
        return {"product": IStoreClient.from_settings().get_product(product_id)}
    except MissingIStoreSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except IStoreAPIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


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


@router.post("/content/articles/{draft_id}/generate-image-plan")
def generate_article_image_plan(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    draft.featured_image_prompt = build_realistic_hero_prompt(draft.featured_image_prompt)
    draft.featured_image_status = "planned"
    draft.image_publish_status = "NOT_PUBLISHED"
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {
        "success": True,
        "message_he": "תכנון התמונה עודכן בהצלחה",
        "draft": draft.to_dict(),
    }


@router.post("/content/articles/{draft_id}/generate-image")
def generate_article_image(draft_id: int, db: DatabaseSession) -> dict[str, object]:
    draft = _get_content_draft_or_404(db, draft_id)
    provider = get_image_provider()
    draft.featured_image_prompt = build_realistic_hero_prompt(draft.featured_image_prompt)
    public_base_url = "https://compass-seo-ai-1.onrender.com"
    metadata = json.loads(draft.image_generation_metadata_json or "{}") if draft.image_generation_metadata_json else {}
    assets = metadata.get("assets", {})
    regeneration_count = int(metadata.get("regeneration_count", 0)) + 1

    hero_prompt = build_realistic_hero_prompt(draft.featured_image_prompt)
    banner_prompt = (
        f"wide premium BBQ banner, {draft.focus_keyword} on grill, dark elegant BBQ background, "
        "empty space for Hebrew text overlay, cinematic lighting, no text, no logos"
    )
    hero_result = provider.generate_hero_image(hero_prompt, draft_slug=f"{draft.slug}-hero")
    banner_result = provider.generate_hero_image(banner_prompt, draft_slug=f"{draft.slug}-banner")
    result = hero_result
    image_file_path = None
    image_public_url = f"{public_base_url}{result.image_url}" if result.image_url and result.image_url.startswith("/") else result.image_url
    image_file_saved = False
    diagnostics = {
        "provider_name": result.provider,
        "provider_response_received": True,
        "raw_provider_url_present": bool(result.image_url),
        "generated_image_url": result.image_url,
        "featured_image_url": result.image_url,
        "image_url_present": bool(result.image_url),
        "image_storage_success": False,
        "image_file_saved": image_file_saved,
        "image_public_url": image_public_url,
        "image_file_path": image_file_path,
        "image_generation_metadata": {
            "width": result.width,
            "height": result.height,
            "provider": result.provider,
            "generated_at": result.generated_at,
        },
    }
    logger.info(
        "Image provider response for draft_id=%s slug=%s provider=%s status=%s image_url=%s diagnostics=%s",
        draft.id,
        draft.slug,
        result.provider,
        result.status,
        result.image_url,
        diagnostics,
    )
    if result.status == "generated" and not result.image_url:
        logger.warning(
            "Image provider returned empty URL for generated image draft_id=%s provider=%s diagnostics=%s",
            draft.id,
            result.provider,
            diagnostics,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "success": False,
                "error": "Image provider returned no URL",
                "diagnostics": diagnostics,
            },
        )
    if result.status == "failed":
        logger.warning(
            "Image provider failed for draft_id=%s provider=%s error=%s diagnostics=%s",
            draft.id,
            result.provider,
            result.error,
            diagnostics,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "success": False,
                "error": result.error or "Image generation failed",
                "diagnostics": diagnostics,
            },
        )
    draft.featured_image_status = result.status
    draft.image_publish_status = "NOT_PUBLISHED"
    draft.generated_image_url = image_public_url
    draft.featured_image_url = image_public_url
    assets["article_hero_image"] = {
        "image_type": "article_hero_image",
        "public_url": image_public_url,
        "local_path": (f"app{hero_result.image_url}" if hero_result.image_url and hero_result.image_url.startswith("/static/") else None),
        "prompt": hero_prompt,
        "alt_text": draft.image_alt_text,
        "width": hero_result.width,
        "height": hero_result.height,
        "created_at": hero_result.generated_at,
        "generation_status": hero_result.status,
    }
    banner_public_url = f"{public_base_url}{banner_result.image_url}" if banner_result.image_url and banner_result.image_url.startswith("/") else banner_result.image_url
    assets["general_banner_image"] = {
        "image_type": "general_banner_image",
        "public_url": banner_public_url,
        "local_path": (f"app{banner_result.image_url}" if banner_result.image_url and banner_result.image_url.startswith("/static/") else None),
        "prompt": banner_prompt,
        "alt_text": f"באנר כללי - {draft.focus_keyword}",
        "width": banner_result.width,
        "height": banner_result.height,
        "created_at": banner_result.generated_at,
        "generation_status": banner_result.status,
    }
    draft.image_generation_metadata_json = json.dumps({
        "width": result.width,
        "height": result.height,
        "provider": result.provider,
        "generated_at": result.generated_at,
        "assets": assets,
        "generated_image_count": len([a for a in assets.values() if a.get("public_url")]),
        "article_hero_image_status": hero_result.status,
        "general_banner_image_status": banner_result.status,
        "image_prompt_version": "v2-separate-assets",
        "regeneration_count": regeneration_count,
    }, ensure_ascii=False)
    diagnostics["image_storage_success"] = bool(result.image_url)
    diagnostics["image_file_saved"] = bool(result.image_url and str(result.image_url).startswith("/static/generated-images/"))
    diagnostics["image_public_url"] = image_public_url
    diagnostics["image_file_path"] = (f"app{result.image_url}" if result.image_url and result.image_url.startswith("/static/") else None)
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {
        "success": True,
        "image_generation_enabled": result.enabled,
        "image_provider": result.provider,
        "image_status": result.status,
        "status": result.status,
        "generated_image_url": image_public_url,
        "featured_image_url": image_public_url,
        "open_image_url": image_public_url,
        "download_image_url": image_public_url,
        "copy_image_url": image_public_url,
        "article_hero_image": assets.get("article_hero_image"),
        "general_banner_image": assets.get("general_banner_image"),
        "image_metadata": {
            "width": result.width,
            "height": result.height,
            "provider": result.provider,
            "generated_at": result.generated_at,
        },
        "diagnostics": diagnostics,
        "message_he": result.message_he,
        "draft": draft.to_dict(),
    }
