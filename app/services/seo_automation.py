import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    CrawlRun,
    GSCKeywordMetric,
    PageAudit,
    PublishingPackage,
    SEOAutomationRun,
    SEOFix,
    SEOTask,
)
from app.integrations.gsc import GSCAPIError, GSCClient
from app.integrations.gsc import MissingGoogleCredentialsError as MissingGSCCredentialsError
from app.integrations.openai_client import OpenAIClient
from app.services.crawler import SEOCrawler
from app.services.seo_strategy_engine import generate_strategy_recommendations, summarize_site_strategy

LOW_CTR_THRESHOLD = 0.03
HIGH_IMPRESSIONS_THRESHOLD = 50
WEAK_RANKING_MIN = 4
WEAK_RANKING_MAX = 20


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pretty_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _metric_payload(metric: GSCKeywordMetric) -> dict[str, object]:
    payload = metric.to_dict()
    payload["keyword_opportunity_score"] = _keyword_opportunity_score(metric)
    return payload


def _keyword_opportunity_score(metric: GSCKeywordMetric | None) -> int:
    if metric is None:
        return 0
    impression_score = min(metric.impressions / 1000, 1) * 45
    ctr_score = (
        max(0.0, LOW_CTR_THRESHOLD - metric.ctr) / LOW_CTR_THRESHOLD * 35 if metric.ctr < LOW_CTR_THRESHOLD else 0
    )
    position_score = (
        20
        if WEAK_RANKING_MIN <= metric.average_position <= WEAK_RANKING_MAX
        else 8
        if metric.average_position <= 30
        else 0
    )
    return round(impression_score + ctr_score + position_score)


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


def _upsert_gsc_metric(db: Session, row: dict[str, object]) -> bool:
    page_url = str(row.get("page_url") or "").strip()
    query = str(row.get("query") or "").strip()
    raw_date = row.get("date")
    if isinstance(raw_date, date):
        metric_date = raw_date
    elif isinstance(raw_date, str):
        try:
            metric_date = date.fromisoformat(raw_date)
        except ValueError:
            return False
    else:
        return False
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


def _build_task_from_page(page: PageAudit, gsc_metric: GSCKeywordMetric | None = None) -> SEOTask:
    missing_fields = [field for field in page.missing_fields.split(",") if field]
    keyword_opportunity_score = _keyword_opportunity_score(gsc_metric)
    recommendations = [f"Add or improve {field.replace('_', ' ')}." for field in missing_fields] or [
        "Improve on-page SEO signals for this low-scoring page."
    ]
    if gsc_metric:
        recommendations.insert(
            0,
            f"Prioritize GSC query '{gsc_metric.query}' with "
            f"{gsc_metric.impressions} impressions and {gsc_metric.ctr:.2%} CTR.",
        )
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
        recommendation_json=json.dumps(
            {
                "source": "automation_latest_crawl_gsc_enriched" if gsc_metric else "automation_latest_crawl",
                "page_audit_id": page.id,
                "seo_score": page.seo_score,
                "missing_fields": missing_fields,
                "primary_query": gsc_metric.query if gsc_metric else None,
                "keyword_opportunity_score": keyword_opportunity_score,
                "gsc_metric": _metric_payload(gsc_metric) if gsc_metric else None,
                "recommendations": recommendations,
            }
        ),
    )


def _create_tasks_from_latest_crawl(db: Session, max_tasks: int) -> list[SEOTask]:
    crawl_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
    if not crawl_run:
        return []
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
        if _seo_task_candidate(page) or _keyword_opportunity_score(gsc_by_url.get(page.url)) >= 55
    ]
    candidates = sorted(
        candidates,
        key=lambda page: (-_keyword_opportunity_score(gsc_by_url.get(page.url)), page.seo_score, page.url),
    )[:max_tasks]
    existing_urls = {
        page_url
        for (page_url,) in db.query(SEOTask.page_url).filter(SEOTask.page_url.in_([p.url for p in candidates])).all()
    }
    new_tasks = [
        _build_task_from_page(page, gsc_by_url.get(page.url)) for page in candidates if page.url not in existing_urls
    ]
    db.add_all(new_tasks)
    db.commit()
    for task in new_tasks:
        db.refresh(task)
    return new_tasks


def _task_recommendation_payload(task: SEOTask, db: Session) -> dict[str, object]:
    gsc_metric = _top_gsc_metric_for_url(db, task.page_url)
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
        "related_gsc_queries": _related_gsc_queries(db, task.page_url),
        "existing_recommendation": _json_object(task.recommendation_json),
    }


def _apply_recommendation_to_task(task: SEOTask, recommendation: dict[str, object]) -> None:
    task.recommendation_json = json.dumps(recommendation, ensure_ascii=False)
    for field_name in ("suggested_title", "suggested_h1", "meta_description"):
        value = recommendation.get(field_name)
        if isinstance(value, str) and value:
            setattr(task, field_name, value)
    task.status = "recommended"


def _task_article_payload(task: SEOTask, db: Session) -> dict[str, object]:
    gsc_metric = _top_gsc_metric_for_url(db, task.page_url)
    return {
        "task_id": task.id,
        "page_url": task.page_url,
        "keyword": task.keyword or (gsc_metric.query if gsc_metric else None),
        "priority": task.priority,
        "status": task.status,
        "suggested_title": task.suggested_title,
        "suggested_h1": task.suggested_h1,
        "meta_description": task.meta_description,
        "secondary_keywords": _related_gsc_queries(db, task.page_url),
        "recommendation": _json_object(task.recommendation_json),
    }


def _apply_article_to_task(task: SEOTask, article: dict[str, object]) -> None:
    task.article_html = article.get("article_html") if isinstance(article.get("article_html"), str) else ""
    task.article_schema_json = json.dumps(article.get("article_schema_json") or {}, ensure_ascii=False)
    task.faq_schema_json = json.dumps(article.get("faq_schema_json") or {}, ensure_ascii=False)
    if isinstance(article.get("meta_title"), str) and article.get("meta_title"):
        task.suggested_title = str(article["meta_title"])
    if isinstance(article.get("meta_description"), str) and article.get("meta_description"):
        task.meta_description = str(article["meta_description"])
    if isinstance(article.get("article_title"), str) and article.get("article_title"):
        task.suggested_h1 = str(article["article_title"])
    task.article_status = "generated"


def _latest_page_audit_for_url(db: Session, page_url: str) -> PageAudit | None:
    return (
        db.query(PageAudit)
        .filter(PageAudit.url == page_url)
        .order_by(PageAudit.crawled_at.desc(), PageAudit.id.desc())
        .first()
    )


def _task_has_generated_article(task: SEOTask) -> bool:
    return task.article_status == "generated" and bool((task.article_html or "").strip())


def _seo_fix_candidate_specs(task: SEOTask, current_page: PageAudit | None) -> list[dict[str, object]]:
    recommendation = _json_object(task.recommendation_json)
    has_generated_article = _task_has_generated_article(task)
    confidence_score = float(recommendation.get("confidence_score", 0.8) or 0.8) if recommendation else 0.9
    primary_source = "generated_article" if has_generated_article else "recommendation"
    specs = [
        {
            "fix_type": "meta_title",
            "current_value": current_page.title if current_page else None,
            "proposed_value": task.suggested_title,
            "source": primary_source,
        },
        {
            "fix_type": "meta_description",
            "current_value": current_page.meta_description if current_page else None,
            "proposed_value": task.meta_description,
            "source": primary_source,
        },
        {
            "fix_type": "h1",
            "current_value": current_page.h1 if current_page else None,
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
                    "proposed_value": _pretty_json(_json_object(task.faq_schema_json)),
                    "source": "generated_article",
                },
                {
                    "fix_type": "article_schema",
                    "current_value": None,
                    "proposed_value": _pretty_json(_json_object(task.article_schema_json)),
                    "source": "generated_article",
                },
            ]
        )
    return [
        {**spec, "confidence_score": confidence_score}
        for spec in specs
        if isinstance(spec.get("proposed_value"), str) and str(spec["proposed_value"]).strip()
    ]


def _create_fixes_for_tasks(db: Session, tasks: list[SEOTask]) -> list[SEOFix]:
    new_fixes: list[SEOFix] = []
    for task in tasks:
        if not _json_object(task.recommendation_json) and not _task_has_generated_article(task):
            continue
        existing_draft_types = {
            fix_type
            for (fix_type,) in db.query(SEOFix.fix_type)
            .filter(SEOFix.task_id == task.id, SEOFix.status == "draft")
            .all()
        }
        current_page = _latest_page_audit_for_url(db, task.page_url)
        for candidate in _seo_fix_candidate_specs(task, current_page):
            if candidate["fix_type"] in existing_draft_types:
                continue
            new_fixes.append(
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
                            "automation": True,
                            "safe_pipeline": "manual_review_required",
                            "auto_approved": False,
                            "auto_published": False,
                        }
                    ),
                )
            )
    db.add_all(new_fixes)
    db.commit()
    for fix in new_fixes:
        db.refresh(fix)
    return new_fixes


def _payload_field_for_fix_type(fix_type: str) -> str:
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
        "manual_instructions": ["Do not auto-publish; apply this fix manually only after approval."],
        "safety": {"auto_publish": False, "requires_manual_istore_application": True},
    }


def _create_publishing_packages_for_approved_fixes(db: Session) -> list[PublishingPackage]:
    approved_fixes = db.query(SEOFix).filter(SEOFix.status == "approved").order_by(SEOFix.id.asc()).all()
    packages: list[PublishingPackage] = []
    for fix in approved_fixes:
        existing_package = (
            db.query(PublishingPackage)
            .filter(PublishingPackage.fix_id == fix.id, PublishingPackage.status.in_(["draft", "ready"]))
            .first()
        )
        if existing_package:
            continue
        packages.append(
            PublishingPackage(
                fix_id=fix.id,
                page_url=fix.page_url,
                cms_type="istore",
                payload_json=json.dumps(_publishing_package_payload(fix), ensure_ascii=False),
                status="ready",
                notes="Prepared for manual ISTORE/CMS publishing. Auto-publishing is disabled.",
            )
        )
    db.add_all(packages)
    db.commit()
    for package in packages:
        db.refresh(package)
    return packages


def run_seo_automation(
    db: Session, max_tasks: int = 10, generate_articles: bool = False, sync_gsc: bool = True
) -> SEOAutomationRun:
    max_tasks = max(1, min(max_tasks, 100))
    run = SEOAutomationRun(
        status="running",
        summary_json=json.dumps(
            {
                "safety": {
                    "auto_approve_fixes": False,
                    "auto_publish": False,
                    "auto_mark_publishing_packages_applied": False,
                },
                "requested": {"max_tasks": max_tasks, "generate_articles": generate_articles, "sync_gsc": sync_gsc},
            }
        ),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    processed_tasks: list[SEOTask] = []

    def warn(step: str, exc: Exception | str) -> None:
        warnings.append({"step": step, "message": str(exc)})

    try:
        crawl_run, _pages = SEOCrawler(settings.target_domain, max_pages=settings.crawler_max_pages).run(db)
        run.crawl_run_id = crawl_run.id
        if crawl_run.status != "completed":
            warn("crawler", crawl_run.error_message or "Crawler did not complete successfully.")
        db.add(run)
        db.commit()

        if sync_gsc:
            try:
                gsc_client = GSCClient.from_settings(db)
                rows = gsc_client.fetch_top_queries(gsc_client.site_url, limit=250)
                for row in rows:
                    if _upsert_gsc_metric(db, row):
                        run.gsc_synced_rows += 1
                db.commit()
            except (MissingGSCCredentialsError, GSCAPIError, RuntimeError, ValueError) as exc:
                warn("gsc_sync", exc)

        new_tasks = _create_tasks_from_latest_crawl(db, max_tasks)
        run.seo_tasks_created = len(new_tasks)
        all_tasks = db.query(SEOTask).order_by(SEOTask.updated_at.desc(), SEOTask.id.desc()).all()
        priority_order = {"high": 0, "medium": 1, "low": 2}
        processed_tasks = sorted(all_tasks, key=lambda task: (priority_order.get(task.priority, 3), -task.id))[
            :max_tasks
        ]
        high_priority_tasks = [task for task in processed_tasks if task.priority == "high"]

        if high_priority_tasks:
            try:
                openai_client = OpenAIClient()
            except RuntimeError as exc:
                openai_client = None
                warn("openai", exc)
            if openai_client is not None:
                for task in high_priority_tasks:
                    try:
                        recommendation = openai_client.generate_seo_recommendation(
                            _task_recommendation_payload(task, db)
                        )
                        _apply_recommendation_to_task(task, recommendation)
                        db.add(task)
                        db.commit()
                        db.refresh(task)
                        run.recommendations_generated += 1
                    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        db.rollback()
                        warn("generate_recommendation", exc)

                if generate_articles:
                    for task in high_priority_tasks:
                        if not _json_object(task.recommendation_json):
                            continue
                        try:
                            article = openai_client.generate_full_article(_task_article_payload(task, db))
                            _apply_article_to_task(task, article)
                            db.add(task)
                            db.commit()
                            db.refresh(task)
                            run.articles_generated += 1
                        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                            db.rollback()
                            warn("generate_article", exc)

        fixes = _create_fixes_for_tasks(db, processed_tasks)
        run.fixes_created = len(fixes)
        packages = _create_publishing_packages_for_approved_fixes(db)
        run.publishing_packages_created = len(packages)

        try:
            strategy = generate_strategy_recommendations(db)
            run.strategy_recommendations_created = int(strategy.get("created_count") or 0)
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            db.rollback()
            warn("strategy_engine", exc)

        run.status = "completed_with_warnings" if warnings else "completed"
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        errors.append({"step": "automation", "message": str(exc)})
        run.status = "failed"
    finally:
        run.completed_at = datetime.now(UTC)
        run.errors_json = json.dumps([*errors, *warnings], ensure_ascii=False)
        run.summary_json = json.dumps(
            {
                "warnings_count": len(warnings),
                "errors_count": len(errors),
                "processed_task_ids": [task.id for task in processed_tasks],
                "safety": {
                    "auto_approve_fixes": False,
                    "auto_publish": False,
                    "auto_mark_publishing_packages_applied": False,
                    "publishing_packages_require_approved_fixes": True,
                },
                "strategy_summary": summarize_site_strategy(db),
            },
            ensure_ascii=False,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
    return run
