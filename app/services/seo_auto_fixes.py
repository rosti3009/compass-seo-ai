from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, IStoreSEOApproval, PageAudit
from app.services.istore_approval import ALLOWED_ISTORE_FIELDS, BLOCKED_ISTORE_FIELDS
from app.services.seo_copy_quality import (
    contains_forbidden_hebrew_phrase,
    sanitize_generated_seo_copy,
    truncate_without_ellipsis,
)
from app.services.seo_engine_version import CURRENT_SEO_ENGINE_VERSION
from app.services.seo_quality_decision import evaluate_seo_text
from app.services.seo_url_filters import get_url_exclusion_reason

PENDING_AUTOFIX_STATUSES = {"PENDING_APPROVAL", "APPROVED", "READY_FOR_MANUAL_PUBLISH"}
SAFE_PUBLISHABLE_FIELDS = {"meta_title", "meta_description", "keyword", "product_description"}
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
WEAK_DRAFT_THRESHOLD = 66


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
    weak_drafts = [
        item for item in review_items if item.get("quality", {}).get("overall_score", 100) < WEAK_DRAFT_THRESHOLD
    ]
    return {
        "pending_count": len(review_items),
        "fixes": review_items,
        "quick_approval_fixes": [item for item in review_items if item not in weak_drafts],
        "weak_drafts_rewrite_required": weak_drafts,
        "grouped_by_issue_type": _group(review_items, "issue_type"),
        "grouped_by_page_type": _group(review_items, "page_type"),
        "safety": _safety_payload(),
    }


def fix_to_review_dict(fix: IStoreSEOApproval) -> dict[str, object]:
    metadata = _json_dict(fix.approval_metadata_json)
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
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
        "quality": quality,
        "weak_draft_rewrite_required": (quality.get("overall_score", 100) < WEAK_DRAFT_THRESHOLD),
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
    if page.page_type == "home":
        issue = _first_issue(
            issues, ["generic_ai_meta", "duplicate_title_similarity", "title_too_long", "title_too_short"]
        )
        return [
            _build_fix(
                page,
                issue,
                "content_draft",
                _current_snapshot(page),
                _homepage_recommendation(page),
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
    classification = _classify_page_context(page)
    quality = _quality_score(page, proposed_value, classification)
    decision = evaluate_seo_text(
        target_url=page.url,
        field_path=field_path,
        old_text=current_value,
        new_text=proposed_value,
        page_type=page.page_type or "",
    )
    metadata = {
        "page_type": page.page_type or "unknown",
        "primary_intent": page.primary_intent or "general",
        "context_keywords": _json_list(page.context_keywords),
        "classification": classification,
        "quality": quality,
        "safe_publish_status": _safe_publish_status_for(target_type, field_path),
        "decision": {
            "decision": decision.decision,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "weakness_flags": decision.weakness_flags,
            "safe_for_quick_approval": decision.safe_for_quick_approval,
            "publishable": decision.publishable,
            "recommendation_text": decision.recommendation_text,
        },
        "quick_approval_visible": quality["overall_score"] >= WEAK_DRAFT_THRESHOLD and decision.safe_for_quick_approval,
        "weak_draft_bucket": "טיוטות חלשות / דורש שכתוב" if quality["overall_score"] < WEAK_DRAFT_THRESHOLD else None,
        "auto_generated_from_latest_crawl": True,
    }
    approval = IStoreSEOApproval(
        target_type=target_type,
        target_id="",
        target_url=page.url,
        source_page_audit_id=page.id,
        source_url=page.url,
        publish_mapping_verified=False,
        mapping_conflict=False,
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
        generated_engine_version=CURRENT_SEO_ENGINE_VERSION,
        generated_at=datetime.now(UTC),
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
    base = _hebrew_entity_name(page)
    keyword = _primary_context_keyword(page)
    title_by_type = {
        "brand": f"{base} | מותג ומוצרים ב-Compass",
        "category": f"{base} | קטגוריה ב-Compass",
        "article": f"{base} | מדריך Compass",
        "blog": f"{base} | מדריך Compass",
        "product": f"{base} | Compass",
    }
    title = title_by_type.get(page.page_type or "", f"{base} | Compass")
    if page.page_type == "product" and keyword and keyword not in base and len(f"{base} {keyword} | Compass") <= 65:
        title = f"{base} {keyword} | Compass"
    return _sanitize_customer_copy(page, title, limit=65)


def _meta_description(page: PageAudit) -> str:
    base = _hebrew_entity_name(page)
    keyword = _primary_context_keyword(page)
    category_hint = f" בתחום {keyword}" if keyword and keyword not in base else ""
    page_type = page.page_type or ""
    if page_type == "brand":
        meta = (
            f"עמוד המותג {base} ב-Compass מרכז מוצרים, קטגוריות ומידע שימושי על סדרות, "
            "מאפיינים ושירות כדי להמשיך לעמודים הרלוונטיים."
        )
    elif page_type == "product":
        meta = (
            f"גלו את {base} ב-Compass{category_hint}: מפרט ברור, תמונות, אחריות ושירות "
            "שיעזרו להבין אם זה המוצר המתאים לעמוד, לחצר או למטבח שלכם."
        )
    elif page_type == "category":
        meta = (
            f"השוו אפשרויות של {base} ב-Compass לפי מפרט, שימושים, אחריות וזמינות, "
            "עם קישורים למוצרים רלוונטיים ומידע שמצמצם את החיפוש."
        )
    elif page_type in {"article", "blog"}:
        meta = (
            f"קראו על {base} ב-Compass עם הסברים מעשיים, דגשים להשוואה " "וקישורים למוצרים או קטגוריות רלוונטיים באתר."
        )
    else:
        meta = f"{base} ב-Compass עם מידע ממוקד, פרטים שימושיים וקישורים רלוונטיים " "להמשך בדיקה באתר."
    return _sanitize_customer_copy(page, _pad_meta(meta, base, keyword or "הנושא"), limit=155)


def _keyword_slug(page: PageAudit) -> str:
    source = _hebrew_entity_name(page)
    normalized = re.sub(r"[^\w\u0590-\u05FF]+", "-", source.strip().lower(), flags=re.UNICODE).strip("-")
    return normalized or "seo-keyword"


def _content_expansion(page: PageAudit) -> str:
    base = _hebrew_entity_name(page)
    classification = _classify_page_context(page)
    template = _content_template(base, classification)
    value = (
        f"<section><h2>{template['h2']}</h2>"
        f"<p>{template['body']}</p>"
        f"<h3>שאלות נפוצות</h3><ul>{template['faq_html']}</ul>"
        f"<h3>קישורים פנימיים מוצעים</h3><p>{template['internal_links']}</p></section>"
    )
    return _sanitize_customer_copy(page, _remove_forbidden_phrases(value))


def _classify_page_context(page: PageAudit) -> dict[str, str]:
    text = f"{_hebrew_entity_name(page)} {' '.join(_hebrew_keywords(page))}".lower()
    if "כנף" in text or "עוף" in text:
        return {
            "product_type": "poultry",
            "food_type": "ready_to_cook",
            "cooking_method": "grill",
            "usage_context": "bbq",
            "customer_intent": "family_cooking",
            "business_vertical": "meat",
            "seo_intent": "organic_food_intent",
        }
    if "שבב" in text or "עישון" in text:
        return {
            "product_type": "smoking_accessory",
            "food_type": "flavor_enhancement",
            "cooking_method": "smoking",
            "usage_context": "bbq",
            "customer_intent": "enthusiast",
            "business_vertical": "bbq_accessories",
            "seo_intent": "informational_commercial",
        }
    if "גריל גז" in text or ("גריל" in text and "גז" in text):
        return {
            "product_type": "outdoor_appliance",
            "food_type": "equipment",
            "cooking_method": "grill",
            "usage_context": "hosting",
            "customer_intent": "comparison",
            "business_vertical": "outdoor_cooking",
            "seo_intent": "commercial_investigation",
        }
    if "טאבון" in text:
        return {
            "product_type": "pizza_oven",
            "food_type": "appliance",
            "cooking_method": "high_heat_bake",
            "usage_context": "outdoor_cooking",
            "customer_intent": "premium_buyer",
            "business_vertical": "outdoor_cooking",
            "seo_intent": "commercial_investigation",
        }
    return {
        "product_type": "bbq_product",
        "food_type": "culinary",
        "cooking_method": "grill",
        "usage_context": "outdoor_cooking",
        "customer_intent": "commercial",
        "business_vertical": "bbq",
        "seo_intent": "organic_search",
    }


def _content_template(base: str, classification: dict[str, str]) -> dict[str, str]:
    product_type = classification["product_type"]
    if product_type == "poultry":
        return {
            "h2": f"{base} לצלייה, תנור ומנגל",
            "body": f"{base} מתאים להכנה מהירה על האש, בתנור או בגריל גז, עם דגש על עסיסיות, תיבול וחריכה מאוזנת.",
            "faq_html": "<li>איך מומלץ לצלות את המוצר כדי לשמור על עסיסיות?</li><li>האם מתאים גם לתנור וגם למנגל?</li>",
            "internal_links": "לקשר למרינדות, שיפודים, גרילי גז, פחמים ואביזרי מנגל משלימים.",
        }
    if product_type == "smoking_accessory":
        return {
            "h2": f"{base} לעישון בשר ודגים",
            "body": f"{base} מוסיף עומק טעמים טבעי לעישון איטי, ומתאים למעשנה, גריל סגור ומתכוני BBQ ארוכים.",
            "faq_html": "<li>איזה עץ מתאים לעישון בקר?</li><li>כמה זמן להשרות שבבי עץ לפני עישון?</li>",
            "internal_links": "לקשר למעשנות, מדחומי בשר, נתחי בריסקט וכלי ניקוי לגריל.",
        }
    return {
        "h2": f"{base} לעולם הצלייה וה-BBQ",
        "body": f"{base} נכתב לקהל שאוהב בישול חוץ, אירוח, על האש ועבודה עם ציוד אמין לפיזור חום אחיד.",
        "faq_html": "<li>מה ההבדל בין דגמים דומים בשימוש אמיתי?</li><li>לאיזה שימוש יומיומי המוצר מתאים?</li>",
        "internal_links": "לקשר לקטגוריות רלוונטיות בלבד: גרילים, פחמים, כלים, כיסויים ואביזרי תחזוקה.",
    }


def _quality_score(page: PageAudit, proposed_value: str, classification: dict[str, str]) -> dict[str, object]:
    text = (proposed_value or "").lower()
    culinary_terms = {"צלייה", "עישון", "מנגל", "טאבון", "על האש", "עסיסיות", "פחמים", "מעשנה", "תיבול", "נתח"}
    term_hits = sum(1 for term in culinary_terms if term in text)
    generic_penalty = 25 if contains_forbidden_hebrew_phrase(text) else 0
    uniqueness = 90 if len(set(text.split())) > 12 else 65
    organic_intent = 88 if any(term in text for term in ("איך", "איזה", "לעישון", "לצלייה", "מבערים")) else 62
    premium_vertical = classification["business_vertical"] in {"meat", "bbq", "outdoor_cooking"}
    culinary_relevance = min(100, 45 + term_hits * 9 + (10 if premium_vertical else 0))
    naturalness = 90 if "קטגוריית הקטגוריה" not in text else 20
    semantic_quality = round((culinary_relevance + uniqueness + naturalness) / 3)
    overall = max(
        0,
        round(
            (culinary_relevance * 0.25)
            + (semantic_quality * 0.2)
            + (uniqueness * 0.15)
            + (naturalness * 0.2)
            + (organic_intent * 0.2)
            - generic_penalty
        ),
    )
    return {
        "culinary_relevance": culinary_relevance,
        "semantic_quality": semantic_quality,
        "uniqueness": uniqueness,
        "naturalness": naturalness,
        "organic_search_intent_match": organic_intent,
        "anti_generic_score": max(0, 100 - generic_penalty),
        "overall_score": overall,
        "blocked_from_quick_approval": overall < WEAK_DRAFT_THRESHOLD,
    }


def _system_recommendation(page: PageAudit) -> str:
    return (
        "Recommendation only: add noindex, exclude this system URL from SEO automation, "
        "and suppress product/content update tasks. No ISTORE product payload is generated."
    )


def _homepage_recommendation(page: PageAudit) -> str:
    return sanitize_generated_seo_copy(
        "Recommendation only: homepage SEO copy requires manual brand strategy review. "
        "Do not create a simple title or meta rewrite from crawl data alone; review positioning, "
        "priority categories, shipping/service promises and legal/commercial claims before drafting."
    )


def _h1_recommendation(page: PageAudit) -> str:
    return _sanitize_customer_copy(page, f"להוסיף H1 ייחודי וברור: {_hebrew_entity_name(page)}")


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
        return "non_publishable_until_istore_mapping_verified"
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


def _hebrew_entity_name(page: PageAudit) -> str:
    for value in (page.h1, page.title):
        cleaned = _clean_text((value or "").replace(" | Compass", ""))
        if re.search(r"[֐-׿]", cleaned):
            return _truncate(cleaned, 42)
    slug = _url_slug(page.url).replace("-", " ").replace("_", " ")
    translations = {
        "gas": "גז",
        "grill": "גריל",
        "grills": "גרילים",
        "smoker": "מעשנה",
        "smokers": "מעשנות",
        "butcher": "קצב",
        "tools": "כלים",
        "tool": "כלי",
        "bbq": "ברביקיו",
        "weber": "Weber",
        "napoleon": "Napoleon",
    }
    words = [translations.get(part.lower(), part) for part in slug.split() if part]
    translated = " ".join(words).strip()
    if translated and translated != slug:
        if translated == "גז גריל":
            translated = "גריל גז"
        return _truncate(translated, 42)
    return _entity_name(page)


def _sanitize_customer_copy(page: PageAudit, value: str, *, limit: int | None = None) -> str:
    cleaned = sanitize_generated_seo_copy(value)
    for keyword in _json_list(page.context_keywords):
        phrase = _clean_text(str(keyword))
        if phrase and phrase in cleaned and ("," in phrase or re.search(r"[A-Za-z]", phrase)):
            cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*(?=[.:;])", "", cleaned)
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    return sanitize_generated_seo_copy(cleaned, limit=limit)


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
        values.append("מידע שימושי")
    return list(dict.fromkeys(values))


def _primary_context_keyword(page: PageAudit) -> str:
    for keyword in _json_list(page.context_keywords):
        cleaned = _clean_text(str(keyword))
        if cleaned and re.search(r"[֐-׿]", cleaned) and not re.search(r"[A-Za-z]", cleaned):
            return _truncate(cleaned, 28)
    if page.primary_intent and re.search(r"[֐-׿]", page.primary_intent):
        return _truncate(page.primary_intent, 28)
    return ""


def _pad_meta(meta: str, base: str, keyword_text: str) -> str:
    if len(meta) >= 120:
        return meta
    return f"{meta} כולל פירוט שימושים, מפרט ושירות כדי לבדוק את {base} בהקשר של {keyword_text}."


def _remove_forbidden_phrases(value: str) -> str:
    return sanitize_generated_seo_copy(value)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -|,\n\t")


def _truncate(value: str, limit: int) -> str:
    return truncate_without_ellipsis(value, limit)


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
