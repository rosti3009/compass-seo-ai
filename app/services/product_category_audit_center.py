"""Product and category SEO Audit Center helpers.

The audit center is intentionally read-only: it scores product/category URLs from
local crawl, GSC, and synchronized ISTORE metadata, then returns employee-friendly
Hebrew recommendations that can be copied into a manual review workflow.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, GSCKeywordMetric, IStoreProduct, PageAudit

CATEGORY_HINTS = ("category", "categories", "collections", "collection", "קטגור")
PRODUCT_HINTS = ("product", "products", "item", "shop")
HIGH_VALUE_KEYWORDS = (
    "גריל",
    "מעשנה",
    "טאבון",
    "מטבח",
    "kamado",
    "weber",
    "traeger",
    "napoleon",
    "broil",
)


@dataclass(frozen=True)
class GSCPageMetrics:
    clicks: int = 0
    impressions: int = 0
    previous_clicks: int = 0
    previous_impressions: int = 0
    ctr: float = 0.0
    average_position: float = 0.0
    top_queries: tuple[str, ...] = ()

    @property
    def clicks_delta(self) -> int:
        return self.clicks - self.previous_clicks

    @property
    def impressions_delta(self) -> int:
        return self.impressions - self.previous_impressions


@dataclass(frozen=True)
class AuditEntity:
    entity_type: str
    url: str
    name: str
    page: PageAudit | None = None
    product: IStoreProduct | None = None
    category_product_count: int = 0


def _json_load(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _slug_label(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.rstrip("/").split("/")[-1] or parsed.netloc or url
    return re.sub(r"[-_]+", " ", slug).strip() or url


def _is_category_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in CATEGORY_HINTS)


def _is_product_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in PRODUCT_HINTS)


def _latest_crawl_pages(db: Session) -> list[PageAudit]:
    crawl = db.query(CrawlRun).order_by(CrawlRun.completed_at.desc().nullslast(), CrawlRun.id.desc()).first()
    if crawl is None:
        return []
    return db.query(PageAudit).filter(PageAudit.crawl_run_id == crawl.id).all()


def _entity_type_for_page(page: PageAudit) -> str | None:
    page_type = (page.page_type or "").lower()
    if page_type == "category" or _is_category_url(page.url):
        return "category"
    if page_type == "product" or _is_product_url(page.url):
        return "product"
    return None


def _product_url(product: IStoreProduct) -> str:
    return product.canonical_url or product.product_url or product.slug or f"istore-product:{product.istore_product_id}"


def _category_url(category_name: str) -> str:
    slug = re.sub(r"\s+", "-", category_name.strip())
    return f"istore-category:{slug}"


def _status(label: str, ok: bool, detail: str, severity: str = "warning") -> dict[str, str]:
    return {"label": label, "status": "תקין" if ok else "דורש טיפול", "detail": detail, "severity": "ok" if ok else severity}


def _missing_fields(page: PageAudit | None) -> set[str]:
    if page is None:
        return set()
    return {field.strip() for field in (page.missing_fields or "").split(",") if field.strip()}


def _remediations(page: PageAudit | None) -> set[str]:
    if page is None:
        return set()
    values = _json_load(page.remediation_suggestions, [])
    return {str(value) for value in values} if isinstance(values, list) else set()


def _aggregate_gsc_metrics(db: Session) -> dict[str, GSCPageMetrics]:
    rows = db.query(GSCKeywordMetric).order_by(GSCKeywordMetric.date.desc(), GSCKeywordMetric.impressions.desc()).all()
    if not rows:
        return {}

    available_dates = sorted({row.date for row in rows if row.date})
    latest_date = available_dates[-1] if available_dates else date.today()
    current_start = latest_date - timedelta(days=29)
    previous_start = current_start - timedelta(days=30)
    previous_end = current_start - timedelta(days=1)
    current_rows = [row for row in rows if row.date and current_start <= row.date <= latest_date]
    previous_rows = [row for row in rows if row.date and previous_start <= row.date <= previous_end]
    if not previous_rows and len(available_dates) > 1:
        midpoint = max(1, len(available_dates) // 2)
        previous_dates = set(available_dates[:midpoint])
        current_dates = set(available_dates[midpoint:]) or {latest_date}
        previous_rows = [row for row in rows if row.date in previous_dates]
        current_rows = [row for row in rows if row.date in current_dates]

    by_page: dict[str, list[GSCKeywordMetric]] = defaultdict(list)
    previous_by_page: dict[str, list[GSCKeywordMetric]] = defaultdict(list)
    for row in current_rows:
        by_page[row.page_url].append(row)
    for row in previous_rows:
        previous_by_page[row.page_url].append(row)

    metrics: dict[str, GSCPageMetrics] = {}
    all_urls = set(by_page) | set(previous_by_page)
    for url in all_urls:
        current = by_page.get(url, [])
        previous = previous_by_page.get(url, [])
        clicks = sum(max(row.clicks, 0) for row in current)
        impressions = sum(max(row.impressions, 0) for row in current)
        previous_clicks = sum(max(row.clicks, 0) for row in previous)
        previous_impressions = sum(max(row.impressions, 0) for row in previous)
        weighted_position = (
            sum(row.average_position * max(row.impressions, 0) for row in current) / impressions
            if impressions
            else 0.0
        )
        query_counts: dict[str, int] = defaultdict(int)
        for row in current:
            query_counts[row.query] += max(row.impressions, 0)
        top_queries = tuple(query for query, _count in sorted(query_counts.items(), key=lambda item: item[1], reverse=True)[:3])
        metrics[url] = GSCPageMetrics(
            clicks=clicks,
            impressions=impressions,
            previous_clicks=previous_clicks,
            previous_impressions=previous_impressions,
            ctr=clicks / impressions if impressions else 0.0,
            average_position=weighted_position,
            top_queries=top_queries,
        )
    return metrics


def _entities(db: Session) -> list[AuditEntity]:
    pages = _latest_crawl_pages(db)
    products = db.query(IStoreProduct).limit(1000).all()
    products_by_url = {_product_url(product): product for product in products}

    entities: dict[tuple[str, str], AuditEntity] = {}
    for page in pages:
        entity_type = _entity_type_for_page(page)
        if entity_type is None:
            continue
        name = _clean_text(page.title or page.h1) or _slug_label(page.url)
        product = products_by_url.get(page.url)
        entities[(entity_type, page.url)] = AuditEntity(entity_type, page.url, name, page=page, product=product)

    for product in products:
        url = _product_url(product)
        key = ("product", url)
        if key not in entities:
            name = _clean_text(product.product_name) or _slug_label(url)
            entities[key] = AuditEntity("product", url, name, product=product)

    category_counts: dict[str, int] = defaultdict(int)
    for product in products:
        if product.category:
            category_counts[product.category] += 1
    existing_category_names = {entity.name for entity in entities.values() if entity.entity_type == "category"}
    for category_name, count in category_counts.items():
        if category_name in existing_category_names:
            continue
        url = _category_url(category_name)
        entities[("category", url)] = AuditEntity("category", url, category_name, category_product_count=count)

    return list(entities.values())


def _length_status(entity: AuditEntity) -> dict[str, str]:
    word_count = entity.page.word_count if entity.page else 0
    minimum = 250 if entity.entity_type == "category" else 120
    if entity.page is None:
        return _status("אורך תוכן", False, "אין נתון סריקה. לבדוק ידנית בעמוד.", "warning")
    return _status("אורך תוכן", word_count >= minimum, f"{word_count} מילים מתוך יעד מינימום {minimum}.")


def _internal_link_score(page: PageAudit | None) -> int:
    if page is None:
        return 0
    return max(0, min(100, int((page.internal_links or 0) * 20)))


def _schema_status(page: PageAudit | None) -> dict[str, str]:
    missing = _missing_fields(page)
    remediations = _remediations(page)
    has_problem = bool({"schema", "structured_data", "product_schema", "faq_schema"} & (missing | remediations))
    if page is None:
        return _status("Schema", False, "אין נתון סריקה. לבדוק Product/Category/FAQ schema ידנית.", "warning")
    return _status("Schema", not has_problem, "לא נמצאה בעיית schema בסריקה." if not has_problem else "נמצאה בעיית Schema/structured data.")


def _faq_status(entity: AuditEntity) -> dict[str, str]:
    missing = _missing_fields(entity.page)
    remediations = _remediations(entity.page)
    has_faq_signal = "faq" in " ".join(sorted(missing | remediations)).lower()
    if has_faq_signal or (entity.page and (entity.page.word_count or 0) < (250 if entity.entity_type == "category" else 120)):
        return _status("FAQ", False, "מומלץ להוסיף 3-5 שאלות נפוצות בעברית לפני פרסום ידני.", "warning")
    return _status("FAQ", True, "לא זוהה חוסר FAQ קריטי בסריקה.")


def _alt_status(page: PageAudit | None) -> dict[str, str]:
    missing = _missing_fields(page)
    has_alt_problem = bool({"image_alt", "image_missing_alt", "missing_image_alt"} & missing)
    if page is None:
        return _status("ALT", False, "אין נתון תמונות. לבדוק ידנית כיסוי ALT.", "warning")
    return _status("ALT", not has_alt_problem, "כיסוי ALT נראה תקין." if not has_alt_problem else "יש תמונות ללא ALT ברור בעברית.")


def _meta_title(entity: AuditEntity) -> str:
    if entity.page and entity.page.title:
        return entity.page.title
    if entity.product and entity.product.meta_title:
        return entity.product.meta_title
    return ""


def _meta_description(entity: AuditEntity) -> str:
    if entity.page and entity.page.meta_description:
        return entity.page.meta_description
    if entity.product and entity.product.meta_description:
        return entity.product.meta_description
    return ""


def _h1(entity: AuditEntity) -> str:
    return entity.page.h1 if entity.page and entity.page.h1 else entity.name


def _status_pack(entity: AuditEntity) -> dict[str, Any]:
    title = _meta_title(entity)
    description = _meta_description(entity)
    h1 = entity.page.h1 if entity.page else ""
    missing = _missing_fields(entity.page)
    return {
        "meta_title": _status(
            "Meta title",
            bool(title.strip()) and 25 <= len(title) <= 65 and "title" not in missing and "meta_title" not in missing,
            f"{len(title)} תווים" if title else "חסר Meta title.",
        ),
        "meta_description": _status(
            "Meta description",
            bool(description.strip()) and 70 <= len(description) <= 160 and "meta_description" not in missing,
            f"{len(description)} תווים" if description else "חסר Meta description.",
        ),
        "h1": _status("H1", bool(h1.strip()) and "h1" not in missing, h1 or "חסר H1 ברור."),
        "content_length": _length_status(entity),
        "faq": _faq_status(entity),
        "alt_coverage": _alt_status(entity.page),
        "schema": _schema_status(entity.page),
    }


def _base_seo_score(entity: AuditEntity, statuses: dict[str, dict[str, str]]) -> int:
    if entity.page and entity.page.seo_score:
        score = int(round(entity.page.seo_score))
    else:
        score = 55
    penalties = sum(7 for status in statuses.values() if status["status"] != "תקין")
    if entity.page is None:
        penalties += 8
    return max(0, min(100, score - penalties))


def _entity_value_score(entity: AuditEntity, gsc: GSCPageMetrics) -> int:
    text = f"{entity.name} {entity.url} {entity.product.brand if entity.product else ''} {entity.product.category if entity.product else ''}".lower()
    commercial_bonus = 20 if any(keyword.lower() in text for keyword in HIGH_VALUE_KEYWORDS) else 0
    category_bonus = 25 if entity.entity_type == "category" else 0
    catalog_bonus = min(25, entity.category_product_count * 2) if entity.entity_type == "category" else 0
    gsc_bonus = min(30, int(gsc.impressions / 50))
    return min(100, commercial_bonus + category_bonus + catalog_bonus + gsc_bonus)


def _traffic_loss_score(gsc: GSCPageMetrics) -> int:
    impression_loss = max(0, -gsc.impressions_delta)
    click_loss = max(0, -gsc.clicks_delta)
    return min(100, int(impression_loss / 20) + click_loss * 5)


def _estimated_revenue_risk(entity: AuditEntity, gsc: GSCPageMetrics) -> dict[str, Any]:
    lost_clicks = max(0, -gsc.clicks_delta)
    value_multiplier = 3 if entity.entity_type == "category" else 2
    if _entity_value_score(entity, gsc) >= 50:
        value_multiplier += 2
    return {
        "label": "גבוה" if lost_clicks * value_multiplier >= 25 else "בינוני" if lost_clicks else "לא זוהתה ירידה",
        "estimated_lost_clicks": lost_clicks,
        "estimated_value_points": lost_clicks * value_multiplier,
        "note": "אומדן לפי ירידת קליקים אורגניים ופוטנציאל מסחרי; אינו מחליף נתוני הכנסות GA4/חנות.",
    }


def _ready_fixes(entity: AuditEntity, statuses: dict[str, dict[str, str]], gsc: GSCPageMetrics) -> list[dict[str, str]]:
    name = _clean_text(entity.name) or _slug_label(entity.url)
    product_or_category = "קטגוריית" if entity.entity_type == "category" else "מוצר"
    keyword = gsc.top_queries[0] if gsc.top_queries else name
    meta_title = _truncate(f"{name} | Compass Grill", 60)
    meta_description = _truncate(
        f"{name} - מידע ברור, השוואה וטיפים שיעזרו לבחור נכון. היכנסו ל-Compass Grill לפרטים, התאמה וזמינות.",
        155,
    )
    faq = (
        f"שאלה: איך בוחרים {name}?\n"
        f"תשובה: בודקים התאמה לשימוש, מידות, חומרי גלם וזמינות. מומלץ לוודא שהמידע בעמוד תואם למוצר בפועל."
    )
    fixes = []
    if statuses["meta_title"]["status"] != "תקין":
        fixes.append({"field": "Meta title", "recommendation": "להעתיק רק לאחר בדיקה ידנית.", "copy": meta_title})
    if statuses["meta_description"]["status"] != "תקין":
        fixes.append({"field": "Meta description", "recommendation": "ניסוח ידידותי לעובדים, לא לפרסום אוטומטי.", "copy": meta_description})
    if statuses["h1"]["status"] != "תקין":
        fixes.append({"field": "H1", "recommendation": "להגדיר ככותרת ראשית אחת בעמוד.", "copy": name})
    if statuses["content_length"]["status"] != "תקין":
        fixes.append(
            {
                "field": "תוכן עמוד",
                "recommendation": f"להוסיף פסקת פתיחה ל{product_or_category} עם מילת מפתח: {keyword}.",
                "copy": f"{name} מתאים ללקוחות שמחפשים {keyword}. מומלץ להציג בעמוד יתרונות מרכזיים, שימושים נפוצים, מפרט רלוונטי ותשובות קצרות לשאלות לפני רכישה.",
            }
        )
    if statuses["faq"]["status"] != "תקין":
        fixes.append({"field": "FAQ", "recommendation": "להוסיף ידנית 3-5 שאלות נפוצות.", "copy": faq})
    if statuses["alt_coverage"]["status"] != "תקין":
        fixes.append({"field": "ALT", "recommendation": "לעבור על התמונות בעמוד ולהוסיף ALT לכל תמונה חסרה.", "copy": f"תמונה של {name} באתר Compass Grill"})
    if statuses["schema"]["status"] != "תקין":
        schema_type = "CollectionPage" if entity.entity_type == "category" else "Product"
        fixes.append({"field": "Schema", "recommendation": "להעביר למפתח/אחראי אתר לבדיקה ידנית.", "copy": f"לבדוק שהעמוד כולל {schema_type} schema ו-FAQ schema אם נוספו שאלות."})
    if _internal_link_score(entity.page) < 60:
        fixes.append({"field": "קישור פנימי", "recommendation": "להוסיף קישור מעמוד רלוונטי באתר.", "copy": f"למידע נוסף על {name}, בקרו בעמוד: {entity.url}"})
    return fixes


def build_product_category_audit_center(db: Session, limit: int = 100) -> dict[str, Any]:
    """Return prioritized product/category SEO audits with copy-ready Hebrew fixes.

    Prioritization order follows the product request: categories first, then
    high-value products, then products/pages with GSC impressions.
    """

    gsc_by_url = _aggregate_gsc_metrics(db)
    audits = []
    for entity in _entities(db):
        gsc = gsc_by_url.get(entity.url, GSCPageMetrics())
        statuses = _status_pack(entity)
        seo_score = _base_seo_score(entity, statuses)
        internal_link_score = _internal_link_score(entity.page)
        value_score = _entity_value_score(entity, gsc)
        traffic_loss_score = _traffic_loss_score(gsc)
        priority_score = (
            (1000 if entity.entity_type == "category" else 0)
            + value_score * 3
            + gsc.impressions / 10
            + traffic_loss_score * 4
            + max(0, 100 - seo_score) * 2
        )
        audits.append(
            {
                "entity_type": entity.entity_type,
                "entity_type_hebrew": "קטגוריה" if entity.entity_type == "category" else "מוצר",
                "name": entity.name,
                "url": entity.url,
                "seo_score": seo_score,
                "statuses": statuses,
                "content_length": entity.page.word_count if entity.page else 0,
                "internal_link_score": internal_link_score,
                "gsc": {
                    "clicks": gsc.clicks,
                    "impressions": gsc.impressions,
                    "clicks_delta": gsc.clicks_delta,
                    "impressions_delta": gsc.impressions_delta,
                    "ctr": gsc.ctr,
                    "average_position": gsc.average_position,
                    "top_queries": list(gsc.top_queries),
                },
                "value_score": value_score,
                "traffic_loss_score": traffic_loss_score,
                "revenue_risk": _estimated_revenue_risk(entity, gsc),
                "ready_to_copy_fixes": _ready_fixes(entity, statuses, gsc),
                "priority_score": round(priority_score, 2),
                "safety": {
                    "auto_publish": False,
                    "edits_live_content": False,
                    "manual_review_required": True,
                    "message": "המלצות בלבד: לא מפרסם ולא עורך תוכן חי.",
                },
            }
        )

    audits = sorted(
        audits,
        key=lambda item: (
            0 if item["entity_type"] == "category" else 1,
            -float(item["priority_score"]),
            -int(item["value_score"]),
            -int(item["gsc"]["impressions"]),
            str(item["name"]),
        ),
    )[:limit]
    categories = [item for item in audits if item["entity_type"] == "category"]
    products = [item for item in audits if item["entity_type"] == "product"]
    return {
        "summary": {
            "total_entities": len(audits),
            "categories": len(categories),
            "products": len(products),
            "high_value_products": sum(1 for item in products if int(item["value_score"]) >= 40),
            "with_gsc_impressions": sum(1 for item in audits if int(item["gsc"]["impressions"]) > 0),
            "losing_traffic": sum(1 for item in audits if int(item["gsc"]["clicks_delta"]) < 0 or int(item["gsc"]["impressions_delta"]) < 0),
            "safety": "אין פרסום אוטומטי ואין עריכת תוכן חי.",
        },
        "prioritization": ["קטגוריות", "מוצרים בעלי ערך גבוה", "מוצרים/עמודים עם חשיפות GSC"],
        "audits": audits,
        "categories": categories,
        "products": products,
    }
