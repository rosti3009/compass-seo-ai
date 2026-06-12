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
from urllib.parse import unquote, urlparse

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, GSCKeywordMetric, IStoreProduct, PageAudit

try:  # sitemap support is best-effort and read-only
    from app.services.content_articles import _load_sitemap_index
except Exception:  # pragma: no cover - defensive fallback for optional import failures
    _load_sitemap_index = None

CATEGORY_HINTS = ("category", "categories", "collections", "collection", "קטגור")
PRODUCT_HINTS = ("product", "products", "item", "shop")
UNKNOWN_NOTICE = "לא נסרק — נדרש בדיקה ידנית"
MANUAL_NOTICE = "יש להעתיק ידנית לאתר ISTORE לאחר בדיקה."
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
    slug = unquote(parsed.path.rstrip("/").split("/")[-1] or parsed.netloc or url)
    return re.sub(r"[-_]+", " ", slug).strip() or url


def _normalize_url_key(url: str | None) -> str:
    """Normalize page URLs for matching crawl, product, sitemap, and GSC rows."""

    raw = _clean_text(url)
    if not raw:
        return ""
    if raw.startswith("istore-"):
        return raw.rstrip("/").lower()
    parsed = urlparse(raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.I) else f"https://{raw}")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path or "/")
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    return f"{host}{path}".lower()


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


def _status(
    label: str,
    ok: bool,
    detail: str,
    severity: str = "warning",
    *,
    state: str | None = None,
    current_value: str = "",
    source: str = "",
) -> dict[str, str]:
    resolved_state = state or ("valid" if ok else "weak")
    if resolved_state == "unknown":
        display = UNKNOWN_NOTICE
        severity = "unknown"
    elif resolved_state == "missing_confirmed":
        display = "חסר לפי נתונים זמינים"
        severity = "critical"
    elif resolved_state == "weak":
        display = "קיים אך חלש"
    else:
        display = "תקין"
        severity = "ok"
    return {
        "label": label,
        "status": display,
        "detail": detail,
        "severity": severity,
        "state": resolved_state,
        "current_value": current_value,
        "source": source,
    }


def _field_state(status: dict[str, str]) -> str:
    return status.get("state", "weak")


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
        by_page[_normalize_url_key(row.page_url)].append(row)
    for row in previous_rows:
        previous_by_page[_normalize_url_key(row.page_url)].append(row)

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
    products_by_url = {_normalize_url_key(_product_url(product)): product for product in products}

    entities: dict[tuple[str, str], AuditEntity] = {}
    for page in pages:
        entity_type = _entity_type_for_page(page)
        if entity_type is None:
            continue
        name = _clean_text(page.title or page.h1) or _slug_label(page.url)
        product = products_by_url.get(_normalize_url_key(page.url))
        entities[(entity_type, page.url)] = AuditEntity(entity_type, page.url, name, page=page, product=product)

    for product in products:
        url = _product_url(product)
        key = ("product", url)
        if key not in entities:
            name = _clean_text(product.product_name) or _slug_label(url)
            entities[key] = AuditEntity("product", url, name, product=product)

    if _load_sitemap_index is not None:
        try:
            sitemap_entries, _stats = _load_sitemap_index()
        except Exception:
            sitemap_entries = []
        for entry in sitemap_entries:
            url = str(entry.get("url") or "") if isinstance(entry, dict) else ""
            entry_type = str(entry.get("page_type") or entry.get("type") or "") if isinstance(entry, dict) else ""
            if not url or entry_type not in {"category", "product"}:
                if not url or not _is_category_url(url):
                    continue
                entry_type = "category"
            key = (entry_type, url)
            if key in entities:
                continue
            title = str(entry.get("title") or entry.get("inferred_title") or "") if isinstance(entry, dict) else ""
            entities[key] = AuditEntity(entry_type, url, _clean_text(title) or _slug_label(url))

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
        return _status("אורך תוכן", False, UNKNOWN_NOTICE, state="unknown", source="crawl")
    if word_count <= 0:
        return _status("אורך תוכן", False, "חסר לפי נתוני הסריקה האחרונה.", state="missing_confirmed", source="crawl")
    return _status("אורך תוכן", word_count >= minimum, f"{word_count} מילים מתוך יעד מינימום {minimum}.", state="valid" if word_count >= minimum else "weak", current_value=str(word_count), source="crawl")


def _internal_link_score(page: PageAudit | None) -> int:
    if page is None:
        return -1
    return max(0, min(100, int((page.internal_links or 0) * 20)))


def _schema_status(page: PageAudit | None) -> dict[str, str]:
    missing = _missing_fields(page)
    remediations = _remediations(page)
    has_problem = bool({"schema", "structured_data", "product_schema", "faq_schema"} & (missing | remediations))
    if page is None:
        return _status("Schema", False, UNKNOWN_NOTICE, state="unknown", source="crawl")
    return _status("Schema", not has_problem, "לא נמצאה בעיית schema בסריקה." if not has_problem else "נמצאה בעיית Schema/structured data לפי הסריקה.", state="valid" if not has_problem else "missing_confirmed", source="crawl")


def _faq_status(entity: AuditEntity) -> dict[str, str]:
    if entity.page is None:
        return _status("FAQ", False, UNKNOWN_NOTICE, state="unknown", source="crawl")
    missing = _missing_fields(entity.page)
    remediations = _remediations(entity.page)
    has_faq_signal = "faq" in " ".join(sorted(missing | remediations)).lower()
    if has_faq_signal or (entity.page and (entity.page.word_count or 0) < (250 if entity.entity_type == "category" else 120)):
        return _status("FAQ", False, "מומלץ להוסיף 3-5 שאלות נפוצות בעברית לפני פרסום ידני.", "warning", state="weak", source="crawl")
    return _status("FAQ", True, "לא זוהה חוסר FAQ קריטי בסריקה.", source="crawl")


def _alt_status(page: PageAudit | None) -> dict[str, str]:
    missing = _missing_fields(page)
    has_alt_problem = bool({"image_alt", "image_missing_alt", "missing_image_alt"} & missing)
    if page is None:
        return _status("ALT", False, UNKNOWN_NOTICE, state="unknown", source="crawl")
    return _status("ALT", not has_alt_problem, "כיסוי ALT נראה תקין." if not has_alt_problem else "יש תמונות ללא ALT ברור בעברית לפי הסריקה.", state="valid" if not has_alt_problem else "missing_confirmed", source="crawl")


def _meta_title(entity: AuditEntity) -> tuple[str, str]:
    if entity.product and entity.product.meta_title:
        return entity.product.meta_title, "ISTORE"
    if entity.page and entity.page.title:
        return entity.page.title, "crawl"
    return "", ""


def _meta_description(entity: AuditEntity) -> tuple[str, str]:
    if entity.product and entity.product.meta_description:
        return entity.product.meta_description, "ISTORE"
    if entity.page and entity.page.meta_description:
        return entity.page.meta_description, "crawl"
    return "", ""


def _h1(entity: AuditEntity) -> str:
    return entity.page.h1 if entity.page and entity.page.h1 else entity.name


def _text_field_status(label: str, value: str, source: str, minimum: int, maximum: int, missing_aliases: set[str], missing: set[str]) -> dict[str, str]:
    clean = _clean_text(value)
    if not clean:
        if source or missing_aliases & missing:
            detail = f"חסר לפי נתוני {source or 'הסריקה'}"
            return _status(label, False, detail, state="missing_confirmed", source=source or "crawl")
        return _status(label, False, UNKNOWN_NOTICE, state="unknown")
    if missing_aliases & missing:
        return _status(label, False, f"חסר לפי נתוני הסריקה למרות שקיים מקור אחר: {clean}", state="missing_confirmed", current_value=clean, source=source)
    ok = minimum <= len(clean) <= maximum
    detail = f"{len(clean)} תווים — מקור: {source}" if source else f"{len(clean)} תווים"
    return _status(label, ok, detail, state="valid" if ok else "weak", current_value=clean, source=source)


def _status_pack(entity: AuditEntity) -> dict[str, Any]:
    title, title_source = _meta_title(entity)
    description, description_source = _meta_description(entity)
    h1 = entity.page.h1 if entity.page else ""
    missing = _missing_fields(entity.page)
    return {
        "meta_title": _text_field_status("Meta title", title, title_source, 25, 65, {"title", "meta_title"}, missing),
        "meta_description": _text_field_status("Meta description", description, description_source, 70, 160, {"meta_description"}, missing),
        "h1": _text_field_status("H1", h1, "crawl" if entity.page else "", 2, 90, {"h1"}, missing),
        "content_length": _length_status(entity),
        "faq": _faq_status(entity),
        "alt_coverage": _alt_status(entity.page),
        "schema": _schema_status(entity.page),
    }


def _base_seo_score(entity: AuditEntity, statuses: dict[str, dict[str, str]]) -> int:
    if entity.page and entity.page.seo_score:
        score = int(round(entity.page.seo_score))
    else:
        score = 72 if entity.product and (entity.product.meta_title or entity.product.meta_description) else 62
    penalties = 0
    for key, status in statuses.items():
        state = _field_state(status)
        if state == "missing_confirmed":
            penalties += 14 if key in {"meta_title", "meta_description", "h1"} else 9
        elif state == "weak":
            penalties += 7 if key in {"meta_title", "meta_description", "content_length"} else 4
        elif state == "unknown":
            penalties += 2
    return max(0, min(100, score - penalties))


def _data_confidence_score(statuses: dict[str, dict[str, str]]) -> int:
    total = len(statuses) or 1
    unknown = sum(1 for status in statuses.values() if _field_state(status) == "unknown")
    return max(0, min(100, int(round(100 - (unknown / total * 70)))))


def _confirmed_missing_count(statuses: dict[str, dict[str, str]]) -> int:
    return sum(1 for status in statuses.values() if _field_state(status) == "missing_confirmed")


def _unknown_count(statuses: dict[str, dict[str, str]]) -> int:
    return sum(1 for status in statuses.values() if _field_state(status) == "unknown")


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


def _priority_reason(entity: AuditEntity, statuses: dict[str, dict[str, str]], gsc: GSCPageMetrics, seo_score: int, confidence: int, value_score: int) -> str:
    if entity.entity_type == "category":
        return "קטגוריה בעדיפות ראשונה כי עמודי קטגוריה משפיעים על קבוצת מוצרים רחבה."
    if gsc.impressions or gsc.clicks:
        return f"יש נתוני GSC: {gsc.impressions} חשיפות ו-{gsc.clicks} קליקים, לכן יש כאן הזדמנות SEO מוכחת."
    if value_score >= 40:
        return "מוצר בעל ערך מסחרי גבוה לפי שם/מותג/קטגוריה."
    if any(_field_state(statuses[key]) == "missing_confirmed" for key in ("meta_title", "meta_description")):
        return "Meta title או Meta description חסרים לפי נתוני ISTORE/סריקה."
    if seo_score < 70:
        return "ציון SEO נמוך בגלל שדות חלשים או תוכן דל."
    if confidence < 70:
        return "חלק מנתוני הסריקה אינם זמינים — נדרש אימות ידני לפני החלטה."
    return "בדיקה שוטפת לשיפור תוכן וקישורים פנימיים."


def _rec(field_key: str, label: str, instruction: str, current: str, suggested: str, manual_notice: str = MANUAL_NOTICE) -> dict[str, str]:
    return {
        "field_key": field_key,
        "label_he": label,
        "instruction_he": instruction,
        "current_value": current or "",
        "suggested_value": suggested,
        "copy_text": suggested,
        "manual_notice": manual_notice,
        # Backward-compatible keys for older templates/tests.
        "field": label,
        "recommendation": instruction,
        "copy": suggested,
    }


def _ready_fixes(entity: AuditEntity, statuses: dict[str, dict[str, str]], gsc: GSCPageMetrics) -> list[dict[str, str]]:
    name = _clean_text(entity.name) or _slug_label(entity.url)
    keyword = gsc.top_queries[0] if gsc.top_queries else name
    meta_title = _truncate(f"{name} | Compass Grill", 60)
    meta_description = _truncate(
        f"{name} - מידע ברור, השוואה וטיפים שיעזרו לבחור נכון. היכנסו ל-Compass Grill לפרטים, התאמה וזמינות.",
        155,
    )
    short_description = f"{name} מתאים ללקוחות שמחפשים {keyword}, עם דגש על בחירה נכונה, התאמה לשימוש יומיומי ומידע ברור לפני רכישה."
    long_description = f"בעמוד {name} מומלץ להציג מידע מפורט בעברית: למי המוצר או הקטגוריה מתאימים, יתרונות מרכזיים, מאפיינים חשובים, שימושים נפוצים ונקודות שכדאי לבדוק לפני שמוסיפים לעגלה. תוכן כזה עוזר גם ללקוחות לקבל החלטה וגם לגוגל להבין את הערך של העמוד."
    faq = (
        f"שאלה: איך בוחרים {name}?\n"
        f"תשובה: בודקים התאמה לשימוש, מידות, חומרי גלם וזמינות. מומלץ לוודא שהמידע בעמוד תואם למוצר בפועל.\n"
        f"שאלה: האם {name} מתאים לשימוש ביתי?\n"
        f"תשובה: יש לבדוק מפרט, גודל, אחריות והמלצות שימוש לפני רכישה."
    )
    fixes: list[dict[str, str]] = []
    if statuses["meta_title"]["status"] != "תקין":
        fixes.append(_rec("meta_title", "Meta title", "להחליף/להשלים כותרת מטא ממוקדת.", statuses["meta_title"].get("current_value", ""), meta_title))
    if statuses["meta_description"]["status"] != "תקין":
        fixes.append(_rec("meta_description", "Meta description", "להוסיף תיאור מטא ברור עם ערך ללקוח.", statuses["meta_description"].get("current_value", ""), meta_description))
    if statuses["h1"]["status"] != "תקין":
        fixes.append(_rec("h1", "H1", "להגדיר ככותרת ראשית אחת בעמוד.", statuses["h1"].get("current_value", ""), name))
    if entity.entity_type == "product":
        fixes.append(_rec("short_product_description", "תיאור מוצר קצר", "להוסיף תקציר מוצר מוכן להעתקה.", "", short_description))
        fixes.append(_rec("long_product_description", "תיאור מוצר ארוך", "להוסיף פסקת תוכן מוצר מפורטת לאחר בדיקת דיוק המפרט.", "", long_description))
    else:
        fixes.append(_rec("category_intro", "פסקת פתיחה לקטגוריה", "להוסיף פתיח קטגוריה שמסביר למי הקטגוריה מתאימה.", "", long_description))
    if statuses["content_length"]["status"] != "תקין":
        fixes.append(_rec("content", "תוכן עמוד", f"להוסיף פסקה עם מילת מפתח: {keyword}.", statuses["content_length"].get("current_value", ""), short_description))
    if statuses["faq"]["status"] != "תקין":
        fixes.append(_rec("faq", "FAQ", "להוסיף ידנית 3-5 שאלות נפוצות.", "", faq))
    if statuses["alt_coverage"]["status"] != "תקין":
        fixes.append(_rec("alt_text", "ALT", "לעבור על התמונות בעמוד ולהוסיף ALT לכל תמונה חסרה.", "", f"תמונה של {name} באתר Compass Grill"))
    if statuses["schema"]["status"] != "תקין":
        schema_type = "CollectionPage" if entity.entity_type == "category" else "Product"
        fixes.append(_rec("schema", "Schema", "להעביר למפתח/אחראי אתר לבדיקה ידנית.", "", f"לבדוק שהעמוד כולל {schema_type} schema ו-FAQ schema אם נוספו שאלות."))
    if entity.entity_type == "category":
        fixes.append(_rec("category_internal_links", "קישורים למוצרים חשובים", "לקשר מהקטגוריה למוצרים מובילים לאחר בחירה ידנית.", "", f"מומלץ להתחיל עם {name} ולבדוק גם מוצרים מובילים בקטגוריה לקבלת התאמה מלאה לצרכים."))
    if _internal_link_score(entity.page) < 60:
        fixes.append(_rec("internal_link", "קישור פנימי", "להוסיף קישור מעמוד רלוונטי באתר.", "", f"למידע נוסף על {name}, בקרו בעמוד: {entity.url}"))
    return fixes


def build_product_category_audit_center(db: Session, limit: int = 100) -> dict[str, Any]:
    """Return prioritized product/category SEO audits with copy-ready Hebrew fixes.

    Prioritization order follows the product request: categories first, then
    high-value products, then products/pages with GSC impressions.
    """

    gsc_by_url = _aggregate_gsc_metrics(db)
    audits = []
    for entity in _entities(db):
        gsc = gsc_by_url.get(_normalize_url_key(entity.url), GSCPageMetrics())
        statuses = _status_pack(entity)
        seo_score = _base_seo_score(entity, statuses)
        data_confidence_score = _data_confidence_score(statuses)
        missing_confirmed_count = _confirmed_missing_count(statuses)
        unknown_count = _unknown_count(statuses)
        internal_link_score = _internal_link_score(entity.page)
        value_score = _entity_value_score(entity, gsc)
        traffic_loss_score = _traffic_loss_score(gsc)
        priority_reason = _priority_reason(entity, statuses, gsc, seo_score, data_confidence_score, value_score)
        priority_score = (
            (1000 if entity.entity_type == "category" else 0)
            + (500 if gsc.impressions or gsc.clicks else 0)
            + value_score * 3
            + gsc.impressions / 10
            + traffic_loss_score * 4
            + missing_confirmed_count * 45
            + max(0, 100 - seo_score) * 2
            - unknown_count * 20
        )
        audits.append(
            {
                "entity_type": entity.entity_type,
                "entity_type_hebrew": "קטגוריה" if entity.entity_type == "category" else "מוצר",
                "name": entity.name,
                "url": entity.url,
                "seo_score": seo_score,
                "data_confidence_score": data_confidence_score,
                "missing_confirmed_count": missing_confirmed_count,
                "unknown_count": unknown_count,
                "priority_reason": priority_reason,
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
                "confirmed_problems": [status["label"] for status in statuses.values() if _field_state(status) in {"missing_confirmed", "weak"}],
                "unknown_fields": [status["label"] for status in statuses.values() if _field_state(status) == "unknown"],
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
            -int(item["gsc"]["impressions"]),
            -int(item["gsc"]["clicks"]),
            -int(item["value_score"]),
            -int(item["missing_confirmed_count"]),
            float(item["seo_score"]),
            int(item["unknown_count"]),
            -float(item["priority_score"]),
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
            "low_data_confidence": sum(1 for item in audits if int(item["data_confidence_score"]) < 70),
            "confirmed_missing_meta": sum(1 for item in audits if any(_field_state(item["statuses"][key]) == "missing_confirmed" for key in ("meta_title", "meta_description"))),
            "unknown_scan_data": sum(1 for item in audits if int(item["unknown_count"]) > 0),
            "safety": "אין פרסום אוטומטי ואין עריכת תוכן חי.",
        },
        "filters": ["category/product", "confirmed missing meta", "has GSC impressions", "high value", "low SEO score", "low data confidence", "unknown scan data"],
        "prioritization": ["קטגוריות", "מוצרים עם חשיפות/קליקים ב-GSC", "מוצרים בעלי ערך גבוה", "חוסר Meta מאומת", "תוכן חלש", "נתוני סריקה לא ידועים"],
        "audits": audits,
        "categories": categories,
        "products": products,
    }
