from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, IStoreSEOApproval, PageAudit
from app.services.istore_approval import ALLOWED_ISTORE_FIELDS, BLOCKED_ISTORE_FIELDS
from app.services.seo_url_filters import get_url_exclusion_reason

PENDING_AUTOFIX_STATUSES = {"PENDING_APPROVAL", "APPROVED", "READY_FOR_MANUAL_PUBLISH"}
SAFE_PUBLISHABLE_FIELDS = {"meta_title", "meta_description", "keyword", "product_description"}
FORBIDDEN_HEBREW_PHRASES = (
    "פתרון איכותי",
    "ביצועים מעולים",
    "מוצר המיועד לאנשים",
    "מקסימום נוחות",
    "מוצרים איכותיים",
    "מתאים לשימוש מקצועי וביתי",
)
SUPPORTED_ISSUES = {
    "generic_ai_meta",
    "duplicate_meta_description",
    "duplicate_meta_similarity",
    "duplicate_title_similarity",
    "title_too_long",
    "meta_description_too_long",
    "meta_description_too_short",
    "title_too_short",
    "thin_content",
    "invalid_slug",
    "non_descriptive_slug",
    "missing_h1",
    "h1",
    "system_page_indexable",
}
RISK_WEIGHT = {"low": 10, "medium": 30, "high": 55, "critical": 75}
PAGE_TYPE_WEIGHT = {"product": 16, "brand": 14, "category": 10, "article": 6, "home": 5, "system": -18}
ISSUE_WEIGHT = {
    "system_page_indexable": 35,
    "duplicate_meta_description": 26,
    "duplicate_meta_similarity": 24,
    "duplicate_title_similarity": 22,
    "generic_ai_meta": 22,
    "thin_content": 18,
    "invalid_slug": 14,
    "non_descriptive_slug": 12,
    "meta_description_too_long": 10,
    "title_too_long": 10,
    "meta_description_too_short": 8,
    "title_too_short": 8,
    "missing_h1": 8,
    "h1": 8,
}


@dataclass(frozen=True)
class AutoFixOptions:
    limit: int = 50
    min_risk_level: str | None = None
    page_type: str | None = None
    dry_run: bool = True


def generate_fixes_from_latest_crawl(db: Session, options: AutoFixOptions | None = None) -> dict[str, object]:
    opts = options or AutoFixOptions()
    generated: list[IStoreSEOApproval] = []
    duplicates_skipped = 0
    pages = _latest_pages(db, limit=max(opts.limit, 1), min_risk_level=opts.min_risk_level, page_type=opts.page_type)

    for page in pages:
        for proposal in _page_fix_proposals(page):
            if _existing_autofix(db, proposal):
                duplicates_skipped += 1
                continue
            generated.append(proposal)
            if not opts.dry_run:
                db.add(proposal)

    if not opts.dry_run:
        db.commit()
        for fix in generated:
            db.refresh(fix)

    return {
        "success": True,
        "dry_run": opts.dry_run,
        "pages_scanned": len(pages),
        "fixes_generated": len(generated),
        "duplicates_skipped": duplicates_skipped,
        "fixes": [fix_to_review_dict(fix) for fix in generated],
        "safety": _safety_payload(),
    }


def pending_fixes_review(db: Session, limit: int = 250) -> dict[str, object]:
    fixes = (
        db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status == "PENDING_APPROVAL")
        .order_by(
            IStoreSEOApproval.priority_score.desc(),
            IStoreSEOApproval.created_at.desc(),
            IStoreSEOApproval.id.desc(),
        )
        .limit(limit)
        .all()
    )
    review_items = [fix_to_review_dict(fix) for fix in fixes]
    return {
        "pending_count": len(review_items),
        "fixes": review_items,
        "grouped_by_issue_type": _group(review_items, "issue_type"),
        "grouped_by_page_type": _group(review_items, "page_type"),
        "safety": _safety_payload(),
    }


def fix_to_review_dict(fix: IStoreSEOApproval) -> dict[str, object]:
    metadata = _json_dict(fix.approval_metadata_json)
    page_type = str(metadata.get("page_type") or fix.target_type or "unknown")
    safe_publish_status = _safe_publish_status(fix)
    return {
        **fix.to_dict(),
        "source_audit_id": fix.source_audit_id,
        "issue_type": fix.issue_type,
        "priority_score": fix.priority_score or 0.0,
        "page_type": page_type,
        "preview": {
            "old_value": fix.current_value,
            "new_value": fix.proposed_value,
            "issue_type": fix.issue_type,
            "reason": fix.seo_reason,
            "risk_level": fix.risk_level,
            "priority_score": fix.priority_score or 0.0,
            "safe_publish_status": safe_publish_status,
        },
        "safe_publish_status": safe_publish_status,
    }


def _latest_pages(
    db: Session, *, limit: int, min_risk_level: str | None = None, page_type: str | None = None
) -> list[PageAudit]:
    latest_run_id = db.query(CrawlRun.id).order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc()).limit(1).scalar()
    query = db.query(PageAudit)
    if latest_run_id is not None:
        query = query.filter(PageAudit.crawl_run_id == latest_run_id)
    if page_type:
        query = query.filter(PageAudit.page_type == page_type)
    pages = query.order_by(PageAudit.crawled_at.desc(), PageAudit.id.desc()).limit(limit).all()
    if min_risk_level:
        threshold = RISK_WEIGHT.get(min_risk_level, 0)
        pages = [page for page in pages if RISK_WEIGHT.get(page.seo_risk_level or "low", 0) >= threshold]
    return pages


def _page_fix_proposals(page: PageAudit) -> list[IStoreSEOApproval]:
    issues = _issues(page)
    if not issues:
        return []
    if page.page_type == "system" or "system_page_indexable" in issues or get_url_exclusion_reason(page.url):
        return [
            _build_fix(
                page,
                "system_page_indexable",
                "noindex_recommendation",
                _current_snapshot(page),
                _system_recommendation(page),
            )
        ]

    proposals: list[IStoreSEOApproval] = []
    meta_issues = {
        "generic_ai_meta",
        "duplicate_meta_description",
        "duplicate_meta_similarity",
        "meta_description_too_long",
        "meta_description_too_short",
    }
    if issues & meta_issues:
        issue = _first_issue(
            issues,
            [
                "generic_ai_meta",
                "duplicate_meta_description",
                "duplicate_meta_similarity",
                "meta_description_too_long",
                "meta_description_too_short",
            ],
        )
        proposals.append(
            _build_fix(page, issue, "meta_description", page.meta_description or "", _meta_description(page))
        )
    if issues & {"duplicate_title_similarity", "title_too_long", "title_too_short"}:
        issue = _first_issue(issues, ["duplicate_title_similarity", "title_too_long", "title_too_short"])
        proposals.append(_build_fix(page, issue, "meta_title", page.title or "", _title(page)))
    if issues & {"invalid_slug", "non_descriptive_slug"}:
        issue = _first_issue(issues, ["invalid_slug", "non_descriptive_slug"])
        proposals.append(_build_fix(page, issue, "keyword", _url_slug(page.url), _keyword_slug(page)))
    if "thin_content" in issues:
        proposals.append(
            _build_fix(page, "thin_content", "content_draft", _content_snapshot(page), _content_expansion(page))
        )
    if "missing_h1" in issues or "h1" in issues:
        proposals.append(_build_fix(page, "missing_h1", "h1_recommendation", page.h1 or "", _h1_recommendation(page)))
    return [
        proposal
        for proposal in proposals
        if proposal.proposed_value and proposal.proposed_value != (proposal.current_value or "")
    ]


def _build_fix(
    page: PageAudit, issue_type: str, field_path: str, current_value: str, proposed_value: str
) -> IStoreSEOApproval:
    proposed_value = _remove_forbidden_phrases(proposed_value)
    target_type = _target_type(page, field_path)
    payload = _proposed_payload(field_path, proposed_value)
    rollback = _rollback_payload(field_path, current_value)
    metadata = {
        "page_type": page.page_type or "unknown",
        "primary_intent": page.primary_intent or "general",
        "context_keywords": _json_list(page.context_keywords),
        "safe_publish_status": _safe_publish_status_for(target_type, field_path),
        "auto_generated_from_latest_crawl": True,
    }
    approval = IStoreSEOApproval(
        target_type=target_type,
        target_id=_target_id(page),
        target_url=page.url,
        field_path=field_path,
        current_value=current_value,
        proposed_value=proposed_value,
        seo_reason=_reason(page, issue_type, field_path),
        risk_level=page.seo_risk_level or "low",
        status="PENDING_APPROVAL",
        source_audit_id=page.id,
        issue_type=issue_type,
        priority_score=_priority_score(page, issue_type),
        before_snapshot_json=json.dumps(_before_snapshot(page), ensure_ascii=False),
        proposed_payload_json=json.dumps(payload, ensure_ascii=False),
        rollback_payload_json=json.dumps(rollback, ensure_ascii=False),
        approval_metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    _append_log(approval, "טיוטת SEO נוצרה מתוצאות זחילה אחרונות; לא בוצע פרסום אוטומטי")
    return approval


def _issues(page: PageAudit) -> set[str]:
    fields = {field.strip() for field in (page.missing_fields or "").split(",") if field.strip()}
    suggestions = set(_json_list(page.remediation_suggestions))
    if "h1" in fields:
        fields.add("missing_h1")
    if "expand_content" in suggestions and "thin_content" in fields:
        fields.add("thin_content")
    return fields & SUPPORTED_ISSUES


def _title(page: PageAudit) -> str:
    base = _entity_name(page)
    keywords = _hebrew_keywords(page)
    suffix = " | Compass"
    title = f"{base} - {', '.join(keywords[:2])}" if keywords else base
    if len(title) + len(suffix) <= 65:
        title = f"{title}{suffix}"
    return _truncate(title, 65)


def _meta_description(page: PageAudit) -> str:
    base = _entity_name(page)
    keywords = _hebrew_keywords(page)
    intent = (page.primary_intent or "general").replace("_", " ")
    page_type_phrase = {
        "brand": "מותג מוביל",
        "product": "מוצר נבחר",
        "category": "קטגוריה ממוקדת",
        "article": "מדריך מקצועי",
    }.get(page.page_type or "", "עמוד מידע")
    keyword_text = ", ".join(keywords[:3]) if keywords else intent
    meta = (
        f"{base} - {page_type_phrase} עם מידע ברור על {keyword_text}, "
        "יתרונות מרכזיים והתאמה לצרכים לפני קנייה ב-Compass."
    )
    if page.page_type == "brand":
        meta = f"{base} ב-Compass: סקירת מותג, דגמים רלוונטיים, יתרונות ושיקולים לבחירה חכמה לפי {keyword_text}."
    return _truncate(_pad_meta(_remove_forbidden_phrases(meta), base, keyword_text), 155)


def _keyword_slug(page: PageAudit) -> str:
    source = " ".join([_entity_name(page), *[str(item) for item in _json_list(page.context_keywords)]])
    normalized = re.sub(r"[^\w\u0590-\u05FF]+", "-", source.strip().lower(), flags=re.UNICODE).strip("-")
    return normalized or "seo-keyword"


def _content_expansion(page: PageAudit) -> str:
    base = _entity_name(page)
    keywords = ", ".join(_hebrew_keywords(page)[:3]) or page.primary_intent or "SEO"
    return _remove_forbidden_phrases(
        f"<section><h2>מידע נוסף על {base}</h2>"
        f"<p>{base} מתאים למי שמחפש מידע ממוקד על {keywords}. מומלץ להשוות מאפיינים, "
        "שימושים מרכזיים ותנאי שירות לפני החלטה.</p>"
        "<h3>שאלות נפוצות</h3><ul>"
        f"<li>למי {base} מתאים?</li><li>אילו מאפיינים חשוב לבדוק לפני רכישה?</li>"
        "<li>איך משווים בין אפשרויות דומות?</li>"
        "</ul><h3>קישורים פנימיים מוצעים</h3><p>לקשר לעמודי קטגוריה, מדריכים ומוצרים "
        f"משלימים הקשורים ל-{keywords}.</p></section>"
    )


def _system_recommendation(page: PageAudit) -> str:
    return (
        "Recommendation only: add noindex, exclude this system URL from SEO automation, "
        "and suppress product/content update tasks. No ISTORE product payload is generated."
    )


def _h1_recommendation(page: PageAudit) -> str:
    return f"להוסיף H1 ייחודי וברור: {_entity_name(page)}"


def _priority_score(page: PageAudit, issue_type: str) -> float:
    score = RISK_WEIGHT.get(page.seo_risk_level or "low", 10)
    score += PAGE_TYPE_WEIGHT.get(page.page_type or "unknown", 0)
    score += ISSUE_WEIGHT.get(issue_type, 5)
    score += min(max(page.commercial_intent_score or 0.0, 0.0), 1.0) * 18
    score += max(0.0, 100.0 - (page.seo_score or 0.0)) * 0.18
    if issue_type.startswith("duplicate_"):
        score += 8
    if issue_type == "generic_ai_meta":
        score += 6
    return round(min(score, 100.0), 2)


def _existing_autofix(db: Session, proposal: IStoreSEOApproval) -> bool:
    return (
        db.query(IStoreSEOApproval.id)
        .filter(
            IStoreSEOApproval.target_url == proposal.target_url,
            IStoreSEOApproval.field_path == proposal.field_path,
            IStoreSEOApproval.issue_type == proposal.issue_type,
            IStoreSEOApproval.proposed_value == proposal.proposed_value,
            IStoreSEOApproval.status.in_(PENDING_AUTOFIX_STATUSES),
        )
        .first()
        is not None
    )


def _safe_publish_status(fix: IStoreSEOApproval) -> str:
    return _safe_publish_status_for(fix.target_type, fix.field_path)


def _safe_publish_status_for(target_type: str, field_path: str) -> str:
    recommendation_fields = {"content_draft", "h1_recommendation", "noindex_recommendation"}
    if target_type == "recommendation" or field_path in recommendation_fields:
        return "recommendation_only_not_publishable"
    if field_path == "keyword":
        return "approval_required_keyword_slug_never_auto_publish"
    if target_type == "product" and field_path in SAFE_PUBLISHABLE_FIELDS:
        return "approval_required_publish_gated"
    return "manual_review_only"


def _target_type(page: PageAudit, field_path: str) -> str:
    if field_path in {"content_draft", "h1_recommendation", "noindex_recommendation"}:
        return "recommendation"
    return "product" if page.page_type == "product" else "page"


def _target_id(page: PageAudit) -> str:
    return str(page.id or _url_slug(page.url) or page.url)


def _proposed_payload(field_path: str, proposed_value: str) -> dict[str, object]:
    if field_path in SAFE_PUBLISHABLE_FIELDS:
        return {field_path: proposed_value}
    return {"recommendation": proposed_value, "api_publish_allowed": False}


def _rollback_payload(field_path: str, current_value: str) -> dict[str, object]:
    if field_path in SAFE_PUBLISHABLE_FIELDS:
        return {field_path: current_value or ""}
    return {"recommendation": current_value or "", "api_publish_allowed": False}


def _before_snapshot(page: PageAudit) -> dict[str, object]:
    return {
        "source_audit_id": page.id,
        "target_url": page.url,
        "title": page.title,
        "meta_description": page.meta_description,
        "h1": page.h1,
        "page_type": page.page_type,
        "seo_score": page.seo_score,
        "missing_fields": [field for field in (page.missing_fields or "").split(",") if field],
    }


def _reason(page: PageAudit, issue_type: str, field_path: str) -> str:
    return (
        f"Generated from latest crawl audit #{page.id}: {issue_type} affects {field_path}. "
        f"Risk={page.seo_risk_level}; intent={page.primary_intent}; page_type={page.page_type}. "
        "Draft requires human approval and is not published automatically."
    )


def _entity_name(page: PageAudit) -> str:
    for value in (page.h1, page.title, _url_slug(page.url)):
        cleaned = _clean_text(value or "")
        if cleaned:
            return _truncate(cleaned.replace(" | Compass", ""), 42)
    return "עמוד Compass"


def _hebrew_keywords(page: PageAudit) -> list[str]:
    values = [str(item).replace("_", " ") for item in _json_list(page.context_keywords)]
    if page.primary_intent and page.primary_intent != "general":
        values.append(page.primary_intent.replace("_", " "))
    if not values:
        values.append("בחירה חכמה")
    return list(dict.fromkeys(values))


def _pad_meta(meta: str, base: str, keyword_text: str) -> str:
    if len(meta) >= 120:
        return meta
    return f"{meta} כולל פירוט שימושים, התאמה והשוואה כדי לבחור {base} נכון עבור {keyword_text}."


def _remove_forbidden_phrases(value: str) -> str:
    cleaned = value
    for phrase in FORBIDDEN_HEBREW_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    return _clean_text(cleaned)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -|,\n\t")


def _truncate(value: str, limit: int) -> str:
    value = _clean_text(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip(" ,-|/") + "…"


def _url_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else "home"


def _first_issue(issues: set[str], ordered: list[str]) -> str:
    return next((issue for issue in ordered if issue in issues), ordered[0])


def _current_snapshot(page: PageAudit) -> str:
    return json.dumps(_before_snapshot(page), ensure_ascii=False)


def _content_snapshot(page: PageAudit) -> str:
    return json.dumps({"word_count": page.word_count, "h1": page.h1, "title": page.title}, ensure_ascii=False)


def _json_list(value: str | list[object] | None) -> list[object]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _append_log(approval: IStoreSEOApproval, message: str) -> None:
    approval.publish_log_json = json.dumps(
        [{"status": approval.status, "message": message, "actor": "system"}], ensure_ascii=False
    )


def _group(items: list[dict[str, object]], key: str) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in items:
        grouped.setdefault(str(item.get(key) or "unknown"), []).append(item)
    for values in grouped.values():
        values.sort(key=lambda item: float(item.get("priority_score") or 0.0), reverse=True)
    return grouped


def _safety_payload() -> dict[str, object]:
    return {
        "auto_publish": False,
        "all_generated_status": "PENDING_APPROVAL",
        "requires_explicit_approval": True,
        "requires_istore_publish_enabled": True,
        "requires_istore_safe_mode_false": True,
        "allowed_istore_fields": sorted(ALLOWED_ISTORE_FIELDS),
        "blocked_commerce_fields": sorted(BLOCKED_ISTORE_FIELDS),
    }
