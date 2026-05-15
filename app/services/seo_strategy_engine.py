from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    CrawlRun,
    GSCKeywordMetric,
    PageAudit,
    PublishingPackage,
    SEOFix,
    SEOStrategyRecommendation,
    SEOTask,
)
from app.integrations.openai_client import OpenAIClient
from app.services.internal_links import opportunity_score
from app.services.seo_url_filters import get_url_exclusion_reason, is_seo_eligible_url
from app.services.topical_clusters import build_cluster_summary

LOW_CTR_THRESHOLD = 0.03
WEAK_RANKING_MIN = 4
WEAK_RANKING_MAX = 20


@dataclass(frozen=True)
class StrategyCandidate:
    page_url: str
    recommendation_type: str
    scores: dict[str, float]
    ai_summary: str
    recommended_action: str
    reasoning: str
    source_payload: dict[str, Any]


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return round(max(minimum, min(maximum, value)), 2)


def _missing_fields(page: PageAudit | None) -> set[str]:
    if page is None:
        return set()
    missing = {field.strip() for field in (page.missing_fields or "").split(",") if field.strip()}
    if not page.title:
        missing.add("title")
    if not page.meta_description:
        missing.add("meta_description")
    if not page.h1:
        missing.add("h1")
    return missing


def _latest_crawl_pages(db: Session) -> list[PageAudit]:
    crawl_run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc()).first()
    if not crawl_run:
        return []
    pages = db.query(PageAudit).filter(PageAudit.crawl_run_id == crawl_run.id).all()
    return [page for page in pages if is_seo_eligible_url(page.url)]


def _best_gsc_by_url(db: Session, page_urls: list[str]) -> dict[str, GSCKeywordMetric]:
    query = db.query(GSCKeywordMetric)
    if page_urls:
        query = query.filter(GSCKeywordMetric.page_url.in_(page_urls))
    rows = query.order_by(
        GSCKeywordMetric.impressions.desc(), GSCKeywordMetric.clicks.desc(), GSCKeywordMetric.id.desc()
    ).all()
    best: dict[str, GSCKeywordMetric] = {}
    for row in rows:
        best.setdefault(row.page_url, row)
    return best


def _page_cluster_payload(page: PageAudit, task: SEOTask | None) -> dict[str, Any]:
    return {
        "url": page.url,
        "title": page.title or "",
        "h1": page.h1 or "",
        "meta": page.meta_description or "",
        "word_count": page.word_count,
        "seo_score": page.seo_score,
        "keyword": task.keyword if task else None,
        "priority": task.priority if task else None,
        "article_status": task.article_status if task else "not_generated",
        "excluded_reason": get_url_exclusion_reason(page.url),
    }


def _task_payload(task: SEOTask | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {
        "id": task.id,
        "keyword": task.keyword,
        "priority": task.priority,
        "status": task.status,
        "article_status": task.article_status,
        "has_article": bool(task.article_html),
    }


def calculate_priority_scores(
    page: PageAudit | None = None,
    gsc_metric: GSCKeywordMetric | None = None,
    internal_link_opportunity: float = 0.0,
    topical_gap: float = 0.0,
    task: SEOTask | None = None,
    fix: SEOFix | None = None,
    publishing_package: PublishingPackage | None = None,
) -> dict[str, float]:
    """Calculate normalized SEO strategy impact scores from all available signals."""
    impressions = gsc_metric.impressions if gsc_metric else 0
    ctr = gsc_metric.ctr if gsc_metric else 0.0
    position = gsc_metric.average_position if gsc_metric else 0.0
    traffic_potential_score = _clamp((min(impressions, 5000) / 5000) * 100)
    ctr_opportunity_score = (
        _clamp(((LOW_CTR_THRESHOLD - ctr) / LOW_CTR_THRESHOLD) * 100)
        if impressions and ctr < LOW_CTR_THRESHOLD
        else 0.0
    )
    if WEAK_RANKING_MIN <= position <= WEAK_RANKING_MAX:
        ranking_opportunity_score = _clamp(
            100 - ((position - WEAK_RANKING_MIN) / (WEAK_RANKING_MAX - WEAK_RANKING_MIN)) * 45
        )
    elif position and position <= 3:
        ranking_opportunity_score = 45.0
    elif position and position <= 30:
        ranking_opportunity_score = 35.0
    else:
        ranking_opportunity_score = 0.0

    internal_link_score = _clamp(internal_link_opportunity)
    topical_authority_score = _clamp(topical_gap)

    if page is None:
        content_gap_score = 70.0 if task and task.article_status != "generated" else 0.0
    else:
        missing_component = len(_missing_fields(page)) / 3 * 55
        thin_component = (1 - min(max(page.word_count, 0), 1000) / 1000) * 30
        low_score_component = max(0.0, 70 - page.seo_score) / 70 * 15
        content_gap_score = _clamp(missing_component + thin_component + low_score_component)

    publishing_readiness_score = 0.0
    if publishing_package is not None:
        publishing_readiness_score = 100.0 if publishing_package.status in {"draft", "ready"} else 45.0
    elif fix is not None:
        publishing_readiness_score = (
            85.0 if fix.status == "approved" else 55.0 if fix.status == "ready_for_review" else 25.0
        )
    elif task and task.article_status == "generated":
        publishing_readiness_score = 70.0

    priority_score = _clamp(
        traffic_potential_score * 0.22
        + ctr_opportunity_score * 0.15
        + ranking_opportunity_score * 0.18
        + internal_link_score * 0.12
        + topical_authority_score * 0.12
        + content_gap_score * 0.14
        + publishing_readiness_score * 0.07
    )
    return {
        "priority_score": priority_score,
        "traffic_potential_score": traffic_potential_score,
        "ctr_opportunity_score": ctr_opportunity_score,
        "ranking_opportunity_score": ranking_opportunity_score,
        "internal_link_score": internal_link_score,
        "topical_authority_score": topical_authority_score,
        "content_gap_score": content_gap_score,
        "publishing_readiness_score": publishing_readiness_score,
    }


def _recommendation_type(
    page: PageAudit | None,
    gsc_metric: GSCKeywordMetric | None,
    task: SEOTask | None,
    fix: SEOFix | None,
    package: PublishingPackage | None,
    topical_gap: float,
    internal_link_score: float,
) -> str:
    if package is not None or (fix is not None and fix.status == "approved"):
        return "publish_fix_package"
    if task and task.article_status == "generated":
        return "publish_fix_package"
    missing = _missing_fields(page)
    if "title" in missing:
        return "rewrite_title"
    if "meta_description" in missing:
        return "rewrite_meta"
    if gsc_metric and gsc_metric.impressions >= 50 and gsc_metric.ctr < LOW_CTR_THRESHOLD:
        return "improve_ctr"
    if internal_link_score >= 55:
        return "improve_internal_links"
    if topical_gap >= 60:
        return "create_cluster_content"
    if page and page.word_count < 700:
        return "expand_content"
    if task and task.article_status != "generated":
        return "generate_article"
    if page and page.status_code >= 400:
        return "noindex_page"
    return "expand_content"


def _default_enrichment(candidate: StrategyCandidate) -> dict[str, str]:
    payload = candidate.source_payload
    query = payload.get("gsc_query") or payload.get("keyword") or "primary topic"
    page_url = candidate.page_url
    summary = f"{candidate.recommendation_type.replace('_', ' ').title()} opportunity for {page_url}."
    action = {
        "rewrite_title": f"Rewrite the title around {query} and match search intent.",
        "rewrite_meta": f"Rewrite the meta description to improve CTR for {query}.",
        "generate_article": f"Generate a supporting article targeting {query}.",
        "improve_internal_links": "Add contextual links from high-authority pages to this URL.",
        "create_cluster_content": f"Create supporting cluster content for {query}.",
        "improve_ctr": f"Test title and meta variants to lift CTR for {query}.",
        "expand_content": f"Expand the page with missing subtopics and FAQs for {query}.",
        "publish_fix_package": "Review and publish the prepared SEO fix package manually.",
        "merge_content": "Merge overlapping content into the stronger canonical page.",
        "noindex_page": "Review indexability and consider noindex if the page has no search value.",
    }[candidate.recommendation_type]
    reasoning = (
        "Priority combines traffic potential, CTR/ranking opportunity, internal links, topical gaps, "
        "content gaps, and publishing readiness."
    )
    return {"ai_summary": summary, "recommended_action": action, "reasoning": reasoning}


def _openai_enrich(candidates: list[StrategyCandidate]) -> dict[tuple[str, str], dict[str, str]]:
    if not candidates or not settings.openai_api_key:
        return {}
    try:
        payload = [
            {
                "page_url": candidate.page_url,
                "recommendation_type": candidate.recommendation_type,
                **candidate.scores,
                "source_payload": candidate.source_payload,
            }
            for candidate in candidates[:20]
        ]
        enriched = OpenAIClient().generate_seo_strategy_enrichment(payload)
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}
    items = enriched.get("recommendations", [])
    if not isinstance(items, list):
        return {}
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        page_url = str(item.get("page_url") or "")
        recommendation_type = str(item.get("recommendation_type") or "")
        if not page_url or not recommendation_type:
            continue
        by_key[(page_url, recommendation_type)] = {
            "ai_summary": str(item.get("ai_summary") or ""),
            "recommended_action": str(item.get("recommended_action") or ""),
            "reasoning": str(item.get("reasoning") or ""),
        }
    return by_key


def _candidate_from_context(
    page_url: str,
    recommendation_type: str,
    scores: dict[str, float],
    source_payload: dict[str, Any],
) -> StrategyCandidate:
    candidate = StrategyCandidate(
        page_url=page_url,
        recommendation_type=recommendation_type,
        scores=scores,
        ai_summary="",
        recommended_action="",
        reasoning="",
        source_payload=source_payload,
    )
    enrichment = _default_enrichment(candidate)
    return StrategyCandidate(
        page_url=page_url,
        recommendation_type=recommendation_type,
        scores=scores,
        ai_summary=enrichment["ai_summary"],
        recommended_action=enrichment["recommended_action"],
        reasoning=enrichment["reasoning"],
        source_payload=source_payload,
    )


def rank_recommendations(recommendations: list[SEOStrategyRecommendation | StrategyCandidate]) -> list[Any]:
    """Rank recommendations by business SEO impact, then stable page/type ordering."""
    return sorted(
        recommendations,
        key=lambda item: (
            -float(getattr(item, "priority_score", getattr(item, "scores", {}).get("priority_score", 0.0))),
            str(getattr(item, "page_url", "")),
            str(getattr(item, "recommendation_type", "")),
        ),
    )


def generate_strategy_recommendations(db: Session) -> dict[str, Any]:
    """Generate or update pending strategy recommendations while avoiding duplicate pending rows."""
    pages = _latest_crawl_pages(db)
    page_urls = [page.url for page in pages]
    tasks = [task for task in db.query(SEOTask).all() if is_seo_eligible_url(task.page_url)]
    tasks_by_url = {task.page_url: task for task in tasks}
    gsc_by_url = _best_gsc_by_url(db, list(set(page_urls + [task.page_url for task in tasks])))
    fixes_by_url: dict[str, SEOFix] = {}
    for fix in db.query(SEOFix).order_by(SEOFix.confidence_score.desc(), SEOFix.id.desc()).all():
        if is_seo_eligible_url(fix.page_url):
            fixes_by_url.setdefault(fix.page_url, fix)
    packages_by_url: dict[str, PublishingPackage] = {}
    for package in db.query(PublishingPackage).order_by(PublishingPackage.id.desc()).all():
        if is_seo_eligible_url(package.page_url):
            packages_by_url.setdefault(package.page_url, package)

    page_payloads = [_page_cluster_payload(page, tasks_by_url.get(page.url)) for page in pages]
    clusters = build_cluster_summary(page_payloads)
    weak_cluster_urls = {
        str(url)
        for cluster in clusters
        if len(cluster.get("supporting_pages", [])) < 2 or cluster.get("missing_articles")
        for url in [cluster.get("pillar_page"), *cluster.get("supporting_pages", [])]
        if url
    }

    candidates: list[StrategyCandidate] = []
    for page in pages:
        task = tasks_by_url.get(page.url)
        gsc_metric = gsc_by_url.get(page.url)
        internal_link_score = opportunity_score(page, task)
        topical_gap = 75.0 if page.url in weak_cluster_urls else 25.0
        fix = fixes_by_url.get(page.url)
        package = packages_by_url.get(page.url)
        scores = calculate_priority_scores(page, gsc_metric, internal_link_score, topical_gap, task, fix, package)
        recommendation_type = _recommendation_type(
            page, gsc_metric, task, fix, package, topical_gap, internal_link_score
        )
        payload = {
            "page": page.to_dict(),
            "excluded_reason": get_url_exclusion_reason(page.url),
            "task": _task_payload(task),
            "gsc_query": gsc_metric.query if gsc_metric else None,
            "gsc_impressions": gsc_metric.impressions if gsc_metric else 0,
            "gsc_ctr": gsc_metric.ctr if gsc_metric else 0,
            "gsc_position": gsc_metric.average_position if gsc_metric else 0,
            "has_fix": fix is not None,
            "has_publishing_package": package is not None,
        }
        if scores["priority_score"] >= 15 or recommendation_type in {
            "publish_fix_package",
            "rewrite_title",
            "rewrite_meta",
        }:
            candidates.append(_candidate_from_context(page.url, recommendation_type, scores, payload))

    crawled_urls = set(page_urls)
    for task in tasks:
        if task.page_url in crawled_urls:
            continue
        gsc_metric = gsc_by_url.get(task.page_url)
        fix = fixes_by_url.get(task.page_url)
        package = packages_by_url.get(task.page_url)
        scores = calculate_priority_scores(None, gsc_metric, 30.0, 45.0, task, fix, package)
        recommendation_type = _recommendation_type(None, gsc_metric, task, fix, package, 45.0, 30.0)
        payload = {
            "task": _task_payload(task),
            "excluded_reason": get_url_exclusion_reason(task.page_url),
            "keyword": task.keyword,
            "gsc_query": gsc_metric.query if gsc_metric else None,
        }
        candidates.append(_candidate_from_context(task.page_url, recommendation_type, scores, payload))

    enrichments = _openai_enrich(candidates)
    created = 0
    updated = 0
    recommendations: list[SEOStrategyRecommendation] = []
    for candidate in rank_recommendations(candidates):
        enrichment = enrichments.get((candidate.page_url, candidate.recommendation_type)) or {}
        existing = (
            db.query(SEOStrategyRecommendation)
            .filter(
                SEOStrategyRecommendation.page_url == candidate.page_url,
                SEOStrategyRecommendation.recommendation_type == candidate.recommendation_type,
                SEOStrategyRecommendation.status == "pending",
            )
            .first()
        )
        recommendation = existing or SEOStrategyRecommendation(
            page_url=candidate.page_url,
            recommendation_type=candidate.recommendation_type,
            status="pending",
        )
        if existing is None:
            db.add(recommendation)
            created += 1
        else:
            updated += 1
        for field, value in candidate.scores.items():
            setattr(recommendation, field, value)
        recommendation.ai_summary = enrichment.get("ai_summary") or candidate.ai_summary
        recommendation.recommended_action = enrichment.get("recommended_action") or candidate.recommended_action
        recommendation.reasoning = enrichment.get("reasoning") or candidate.reasoning
        recommendations.append(recommendation)
    db.commit()
    for recommendation in recommendations:
        db.refresh(recommendation)
    return {
        "success": True,
        "created_count": created,
        "updated_count": updated,
        "total_candidates": len(candidates),
        "recommendations": [item.to_dict() for item in rank_recommendations(recommendations)],
        "summary": summarize_site_strategy(db),
    }


def summarize_site_strategy(db: Session) -> dict[str, list[dict[str, Any]]]:
    """Summarize the highest-impact SEO work across recommendations and site readiness signals."""
    recommendations = (
        db.query(SEOStrategyRecommendation)
        .filter(SEOStrategyRecommendation.status == "pending")
        .order_by(SEOStrategyRecommendation.priority_score.desc(), SEOStrategyRecommendation.id.desc())
        .all()
    )
    highest_priority = [item.to_dict() for item in recommendations[:5]]
    quick_wins = [
        item.to_dict()
        for item in recommendations
        if item.publishing_readiness_score >= 60
        or item.recommendation_type in {"rewrite_title", "rewrite_meta", "improve_ctr"}
    ][:5]
    traffic_growth = [item.to_dict() for item in recommendations if item.traffic_potential_score >= 40][:5]
    ready_to_publish = [item.to_dict() for item in recommendations if item.publishing_readiness_score >= 70][:5]
    weak_clusters = [item.to_dict() for item in recommendations if item.topical_authority_score >= 60][:5]
    next_actions = [
        {
            "page_url": item.page_url,
            "recommendation_type": item.recommendation_type,
            "priority_score": item.priority_score,
            "recommended_action": item.recommended_action,
        }
        for item in recommendations[:5]
    ]
    return {
        "highest_priority_pages": highest_priority,
        "quick_wins": quick_wins,
        "traffic_growth_opportunities": traffic_growth,
        "pages_ready_to_publish": ready_to_publish,
        "weak_clusters": weak_clusters,
        "recommended_next_actions": next_actions,
    }
