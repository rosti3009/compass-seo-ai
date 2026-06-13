# ruff: noqa: E501, BLE001, E731
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

import httpx
from bs4 import BeautifulSoup
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
    "בזלת",
    "סו-ויד",
    "ואקום",
    "סכין",
    "שיפוד",
    "יצוק",
    "קאזן",
    "טנדור",
    "kamado",
    "weber",
    "traeger",
    "napoleon",
    "broil",
)

FOOD_KEYWORDS = (
    "אסאדו",
    "אנגוס",
    "אנטריקוט",
    "פילה",
    "שורט ריב",
    "דנוור",
    "המבורגר",
    "צלעות",
    "סטייק",
    "בשר",
    "עגל",
    "צ׳וריסוס",
    "צ'וריסוס",
    "פידלוט",
    "בריסקט",
    "פרוס",
    "טרי",
    "ללא עצם",
    "עם עצם",
    "עוף",
    "נקניק",
    "meat",
    "steak",
    "beef",
    "burger",
)
NON_FOOD_FAMILIES = {
    "grill",
    "smoker",
    "taboon",
    "basalt stone",
    "pizza stone",
    "wood chips/chunks",
    "charcoal/firewood",
    "thermometer",
    "butcher paper",
    "vacuum bags",
    "sous vide",
    "knives",
    "skewers",
    "cast iron cookware",
    "kazan/tandoor",
    "outdoor kitchen",
    "fireplace/fire pit",
    "gloves",
    "burners",
    "grill accessories",
}
GENERIC_FORBIDDEN_PHRASES = (
    "מידע ברור",
    "השוואה וטיפים",
    "מתאים ללקוחות שמחפשים",
    "התאמה לשימוש יומיומי",
    "פתרון איכותי",
    "יש להוסיף תיאור מוצר מפורט",
)
ISTORE_PATHS = {
    "name": "עריכת מוצר/קטגוריה > כללי > שם",
    "meta_title": "עריכת מוצר/קטגוריה > כללי > כותרת לקידום במנוע חיפוש",
    "meta_description": "עריכת מוצר/קטגוריה > כללי > תיאור לקידום במנוע חיפוש",
    "h1": "עריכת מוצר/קטגוריה > כללי > שם / כותרת העמוד",
    "short_product_description": "עריכת מוצר > כללי > תיאור קצר",
    "long_product_description": "עריכת מוצר/קטגוריה > כללי > תיאור",
    "category_intro": "עריכת קטגוריה > כללי > תיאור",
    "faq": "עריכת מוצר/קטגוריה > כללי > תיאור",
    "alt_text": "עריכת תמונה / העלאת תמונה > ALT / תיאור תמונה",
    "internal_link": "עריכת תוכן רלוונטי > הוספת קישור",
    "suggested_slug": "עריכת מוצר/קטגוריה > נתונים > שם ייחודי לקישור",
    "schema": "בדיקת מפתח/אחראי אתר > Schema מובנה",
}


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
    """Normalize page URLs for matching crawl, product, sitemap, canonical, and GSC rows."""

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
    # Query strings, including ?from_admin, are intentionally ignored for canonical matching.
    return f"{host}{path}".lower()


def _slug_tokens(url: str) -> set[str]:
    slug = _slug_label(url).lower()
    slug = re.sub(r"\d+", " ", slug)
    return {token for token in re.split(r"[^\w\u0590-\u05FF]+", slug) if len(token) > 2}


def _extract_live_page_html(
    html: str, url: str = "", status_code: int = 200, final_url: str | None = None
) -> dict[str, Any]:
    """Extract confirmed live-page SEO signals from HTML without inventing missing data."""

    soup = BeautifulSoup(html or "", "html.parser")
    script_soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    title = _clean_text(soup.title.get_text(" ") if soup.title else "")
    meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_description = _clean_text(str(meta_tag.get("content") or "")) if meta_tag else ""
    canonical_tag = soup.find(
        "link", attrs={"rel": lambda value: value and "canonical" in (value if isinstance(value, list) else [value])}
    )
    canonical = _clean_text(str(canonical_tag.get("href") or "")) if canonical_tag else ""
    robots_tags = soup.find_all("meta", attrs={"name": re.compile(r"robots", re.I)})
    robots = ", ".join(_clean_text(str(tag.get("content") or "")) for tag in robots_tags)
    h1_values = [_clean_text(tag.get_text(" ")) for tag in soup.find_all("h1") if _clean_text(tag.get_text(" "))]
    headings = [
        {"level": tag.name, "text": _clean_text(tag.get_text(" "))}
        for tag in soup.find_all(["h2", "h3"])
        if _clean_text(tag.get_text(" "))
    ]
    images = [
        {"src": str(img.get("src") or img.get("data-src") or ""), "alt": _clean_text(str(img.get("alt") or ""))}
        for img in soup.find_all("img")
        if img.get("src") or img.get("data-src")
    ]
    internal_links = []
    base_host = urlparse(url or final_url or "").netloc.lower()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.startswith(("#", "tel:", "mailto:", "javascript:")):
            continue
        parsed = urlparse(href)
        if not parsed.netloc or parsed.netloc.lower().replace("www.", "") == base_host.replace("www.", ""):
            internal_links.append({"href": href, "text": _clean_text(anchor.get_text(" "))})
    json_ld = []
    schema_types: set[str] = set()
    price = ""
    availability = ""
    for script in script_soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        blocks = parsed if isinstance(parsed, list) else [parsed]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            json_ld.append(block)
            candidates = [
                block,
                *(
                    [x for x in block.get("@graph", []) if isinstance(x, dict)]
                    if isinstance(block.get("@graph"), list)
                    else []
                ),
            ]
            for candidate in candidates:
                typ = candidate.get("@type")
                values = typ if isinstance(typ, list) else [typ]
                schema_types.update(str(value) for value in values if value)
                offers = candidate.get("offers")
                offer = offers[0] if isinstance(offers, list) and offers else offers if isinstance(offers, dict) else {}
                if isinstance(offer, dict):
                    price = price or _clean_text(str(offer.get("price") or ""))
                    availability = availability or _clean_text(str(offer.get("availability") or ""))
    visible_text = _clean_text(soup.get_text(" "))
    breadcrumbs = [
        _clean_text(node.get_text(" "))
        for node in soup.find_all(attrs={"class": re.compile("breadcrumb|breadcrumbs|crumb", re.I)})[:3]
    ]
    product_name = h1_values[0] if h1_values else title
    canonical_key = _normalize_url_key(canonical) if canonical else ""
    url_key = _normalize_url_key(final_url or url)
    return {
        "http_status": status_code,
        "final_url": final_url or url,
        "canonical_url": canonical,
        "title_tag": title,
        "meta_description": meta_description,
        "h1": h1_values[0] if h1_values else "",
        "h2_h3_structure": headings,
        "visible_text_length": len(visible_text),
        "visible_word_count": len(visible_text.split()),
        "product_name_from_page": product_name,
        "breadcrumbs": breadcrumbs,
        "images": images,
        "image_urls": [item["src"] for item in images],
        "image_alt_attributes": [item["alt"] for item in images],
        "internal_links": internal_links,
        "json_ld": json_ld,
        "schema_types": sorted(schema_types),
        "product_schema_present": any(str(typ).lower().endswith("product") for typ in schema_types),
        "faq_schema_present": any(str(typ).lower().endswith("faqpage") for typ in schema_types),
        "price": price,
        "availability": availability,
        "robots": robots,
        "noindex": "noindex" in robots.lower(),
        "canonicalized_elsewhere": bool(canonical_key and url_key and canonical_key != url_key),
        "blocked_or_noindex": "noindex" in robots.lower(),
    }


def fetch_and_analyze_live_page(url: str, timeout_seconds: float = 12.0) -> dict[str, Any]:
    """Fetch a live URL and return read-only SEO signals, preserving unknown vs confirmed states."""

    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout_seconds, headers={"User-Agent": "CompassSEOAudit/1.0"}
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return {"http_status": 0, "final_url": url, "fetch_error": str(exc), "scan_status": UNKNOWN_NOTICE}
    analysis = _extract_live_page_html(
        response.text, url=url, status_code=response.status_code, final_url=str(response.url)
    )
    if response.status_code == 404:
        analysis["scan_status"] = "בעיית URL / 404"
    elif _normalize_url_key(url) != _normalize_url_key(str(response.url)):
        analysis["scan_status"] = "מופנה לעמוד אחר"
    elif analysis.get("canonicalized_elsewhere"):
        analysis["scan_status"] = "קנוניקל לעמוד אחר"
    else:
        analysis["scan_status"] = "תקין"
    return analysis


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
            sum(row.average_position * max(row.impressions, 0) for row in current) / impressions if impressions else 0.0
        )
        query_counts: dict[str, int] = defaultdict(int)
        for row in current:
            query_counts[row.query] += max(row.impressions, 0)
        top_queries = tuple(
            query for query, _count in sorted(query_counts.items(), key=lambda item: item[1], reverse=True)[:3]
        )
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
    return _status(
        "אורך תוכן",
        word_count >= minimum,
        f"{word_count} מילים מתוך יעד מינימום {minimum}.",
        state="valid" if word_count >= minimum else "weak",
        current_value=str(word_count),
        source="crawl",
    )


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
    return _status(
        "Schema",
        not has_problem,
        "לא נמצאה בעיית schema בסריקה." if not has_problem else "נמצאה בעיית Schema/structured data לפי הסריקה.",
        state="valid" if not has_problem else "missing_confirmed",
        source="crawl",
    )


def _faq_status(entity: AuditEntity) -> dict[str, str]:
    if entity.page is None:
        return _status("FAQ", False, UNKNOWN_NOTICE, state="unknown", source="crawl")
    missing = _missing_fields(entity.page)
    remediations = _remediations(entity.page)
    has_faq_signal = "faq" in " ".join(sorted(missing | remediations)).lower()
    if has_faq_signal or (
        entity.page and (entity.page.word_count or 0) < (250 if entity.entity_type == "category" else 120)
    ):
        return _status(
            "FAQ",
            False,
            "מומלץ להוסיף 3-5 שאלות נפוצות בעברית לפני פרסום ידני.",
            "warning",
            state="weak",
            source="crawl",
        )
    return _status("FAQ", True, "לא זוהה חוסר FAQ קריטי בסריקה.", source="crawl")


def _alt_status(page: PageAudit | None) -> dict[str, str]:
    missing = _missing_fields(page)
    has_alt_problem = bool({"image_alt", "image_missing_alt", "missing_image_alt"} & missing)
    if page is None:
        return _status("ALT", False, UNKNOWN_NOTICE, state="unknown", source="crawl")
    return _status(
        "ALT",
        not has_alt_problem,
        "כיסוי ALT נראה תקין." if not has_alt_problem else "יש תמונות ללא ALT ברור בעברית לפי הסריקה.",
        state="valid" if not has_alt_problem else "missing_confirmed",
        source="crawl",
    )


def _meta_title(entity: AuditEntity) -> tuple[str, str]:
    if entity.product is not None:
        return entity.product.meta_title or "", "ISTORE"
    if entity.page and entity.page.title:
        return entity.page.title, "crawl"
    return "", ""


def _meta_description(entity: AuditEntity) -> tuple[str, str]:
    if entity.product is not None:
        return entity.product.meta_description or "", "ISTORE"
    if entity.page and entity.page.meta_description:
        return entity.page.meta_description, "crawl"
    return "", ""


def _h1(entity: AuditEntity) -> str:
    return entity.page.h1 if entity.page and entity.page.h1 else entity.name


def _text_field_status(
    label: str, value: str, source: str, minimum: int, maximum: int, missing_aliases: set[str], missing: set[str]
) -> dict[str, str]:
    clean = _clean_text(value)
    if not clean:
        if source or missing_aliases & missing:
            detail = f"חסר לפי נתוני {source or 'הסריקה'}"
            return _status(label, False, detail, state="missing_confirmed", source=source or "crawl")
        return _status(label, False, UNKNOWN_NOTICE, state="unknown")
    if missing_aliases & missing:
        return _status(
            label,
            False,
            f"חסר לפי נתוני הסריקה למרות שקיים מקור אחר: {clean}",
            state="missing_confirmed",
            current_value=clean,
            source=source,
        )
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
        "meta_description": _text_field_status(
            "Meta description", description, description_source, 70, 160, {"meta_description"}, missing
        ),
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
    family = _classify_product_family(entity)
    commercial_bonus = 35 if family in NON_FOOD_FAMILIES else 8 if family == "meat_food" else 12
    commercial_bonus += 20 if any(keyword.lower() in text for keyword in HIGH_VALUE_KEYWORDS) else 0
    category_bonus = 25 if entity.entity_type == "category" else 0
    catalog_bonus = min(25, entity.category_product_count * 2) if entity.entity_type == "category" else 0
    gsc_bonus = min(20, int(gsc.impressions / 80))
    return min(100, commercial_bonus + category_bonus + catalog_bonus + gsc_bonus)


def _traffic_opportunity_score(gsc: GSCPageMetrics) -> int:
    if not (gsc.impressions or gsc.clicks):
        return 0
    ctr_pct = gsc.ctr * 100
    impression_score = min(35, int(gsc.impressions / 30))
    quick_win = 35 if 5 <= gsc.average_position <= 15 and ctr_pct < 2 else 15 if ctr_pct < 2 else 0
    loss = min(20, max(0, -gsc.impressions_delta) // 20 + max(0, -gsc.clicks_delta) * 3)
    click_signal = min(10, gsc.clicks)
    return min(100, impression_score + quick_win + loss + click_signal)


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


def _priority_reason(
    entity: AuditEntity,
    statuses: dict[str, dict[str, str]],
    gsc: GSCPageMetrics,
    seo_score: int,
    confidence: int,
    value_score: int,
) -> str:
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


def _classify_product_family(entity: AuditEntity) -> str:
    text = f"{entity.name} {entity.url} {entity.product.category if entity.product else ''} {entity.product.keyword if entity.product else ''}".lower()
    rules = [
        ("meat_food", FOOD_KEYWORDS),
        ("basalt stone", ("בזלת", "basalt")),
        ("vacuum bags", ("שקיות ואקום", "ואקום", "vacuum bag", "grooved bags")),
        ("sous vide", ("סו-ויד", "sous vide", "anova")),
        ("kazan/tandoor", ("קאזן", "טנדור", "tandoor", "kazan", "persian", "roma")),
        ("smoker", ("מעשנה", "smoker", "פלט")),
        ("grill", ("גריל", "מנגל", "grill", "bbq")),
        ("taboon", ("טאבון", "taboon", "pizza oven")),
        ("pizza stone", ("אבן פיצה", "pizza stone")),
        ("wood chips/chunks", ("שבבי עץ", "צ׳אנק", "צ'אנק", "wood chips", "chunks")),
        ("charcoal/firewood", ("פחם", "עצי הסקה", "charcoal", "firewood")),
        ("thermometer", ("מדחום", "thermometer")),
        ("butcher paper", ("נייר קצבים", "butcher paper")),
        ("knives", ("סכין", "סכינים", "knife", "knives")),
        ("skewers", ("שיפוד", "שיפודים", "skewer")),
        ("gloves", ("כפפות", "gloves", "glove")),
        ("burners", ("מבער", "מבערים", "burner", "burners")),
        ("grill accessories", ("אביזר", "אביזרים", "accessories", "accessory")),
        ("cast iron cookware", ("ברזל יצוק", "מחבת", "סיר", "cast iron")),
        ("outdoor kitchen", ("מטבח חוץ", "outdoor kitchen")),
        ("fireplace/fire pit", ("מדורה", "קמין", "fire pit", "fireplace")),
    ]
    for family, tokens in rules:
        if any(token in text for token in tokens):
            return family
    return "unknown"


def _is_non_food(entity: AuditEntity, family: str) -> bool:
    return family in NON_FOOD_FAMILIES and family != "meat_food"


def _identity(entity: AuditEntity, family: str) -> dict[str, Any]:
    raw = _clean_text(entity.name) or _slug_label(entity.url)
    normalized = raw.lower()
    unclear = normalized in {"assman", "atman", "skiff"} or (
        family == "unknown" and not re.search(r"[\u0590-\u05FF]", raw)
    )
    mapping = {
        "tandoor roma model": "טנדור דגם רומא",
        "professional gas burner": "מבער גז מקצועי",
        "vacuum grooved bags size 20x30": "שקיות ואקום מחורצות 20×30",
        "anova vacuum sealer": "מכונת ואקום Anova",
        "sous vide ball set": "כדורי בידוד לסו-ויד",
        "butcher paper sheets brown for smoking meat": "נייר קצבים חום לעישון בשר",
        "basalt stones": "אבני בזלת לגריל גז",
    }
    if normalized == "persian" and family == "kazan/tandoor":
        return {"name": "טנדור דגם פרסי", "unclear": False}
    if normalized in mapping:
        return {"name": mapping[normalized], "unclear": False}
    if unclear:
        return {"name": "זהות מוצר לא ברורה — נדרש בדיקה ידנית", "unclear": True}
    return {"name": raw, "unclear": False}


def _specific_copy(entity: AuditEntity, gsc: GSCPageMetrics) -> dict[str, str]:
    family = _classify_product_family(entity)
    identity = _identity(entity, family)
    name = identity["name"]
    if identity["unclear"]:
        manual = "זהות מוצר לא ברורה — נדרש שיוך ידני לפני כתיבת SEO. אין לייצר טקסט שיווקי עד שמוודאים מה המוצר."
        return {
            "family": family,
            "name": name,
            "meta_title": name,
            "meta_description": manual,
            "h1": name,
            "short": manual,
            "long": manual,
            "faq": manual,
            "anchor": name,
            "sentence": manual,
            "alt": name,
            "schema": "לא להוסיף Schema מוצר לפני זיהוי ידני.",
        }
    if family == "meat_food":
        meat_name = _truncate(name.replace(" FL ", " ").strip(), 42)
        return {
            "family": family,
            "name": meat_name,
            "meta_title": _truncate(f"{meat_name} | Compass Grill", 58),
            "meta_description": _truncate(
                f"{meat_name} להכנה בתנור, גריל או מעשנה. נתח עסיסי ועשיר בטעם, מתאים לאירוח ולבישול ארוך.",
                155,
            ),
            "h1": meat_name,
            "short": f"{meat_name} הוא מוצר בשר עסיסי ועשיר בטעם, המתאים לבישול ארוך, צלייה או עישון.",
            "long": f"{meat_name} דורש תוכן מוצר שמתייחס לבשר עצמו בלבד: סוג הנתח, אופי ההכנה, זמני בישול משוערים והנחיות בטיחות מזון. אין להחליף אותו בטקסט על ציוד כמו גריל, טנדור, קאזן או מעשנה.",
            "faq": f"שאלה: איך מומלץ להכין {meat_name}?\nתשובה: מומלץ להכין בבישול איטי, צלייה בתנור או עישון עד לריכוך מלא.",
            "anchor": meat_name,
            "sentence": f"למידע נוסף על {meat_name}, עברו לעמוד {meat_name} באתר Compass Grill.",
            "alt": f"{meat_name} - תמונת מוצר Compass Grill",
            "schema": "לוודא שהעמוד כולל Product schema ו-FAQ schema רק לאחר הוספת FAQ מאומת, ללא פרסום אוטומטי.",
        }
    templates = {
        "basalt stone": {
            "name": "אבני בזלת לגריל גז",
            "meta_title": "אבני בזלת לגריל גז – פיזור חום והפחתת התלקחויות | Compass Grill",
            "meta_description": "אבני בזלת לגריל גז לשיפור פיזור החום, שמירה על טמפרטורה יציבה והפחתת התלקחויות בזמן צלייה.",
            "short": "אבני בזלת לגריל גז מסייעות לפיזור חום אחיד, להפחתת התלקחויות ולשמירה על צלייה יציבה יותר לאורך זמן.",
            "long": "אבני בזלת לגריל גז מיועדות לשדרוג חוויית הצלייה: הן מסייעות לפיזור חום יציב יותר, מצמצמות התלקחויות משומן מטפטף ועוזרות לשמור על טמפרטורה אחידה. לפני רכישה יש לבדוק התאמה למבנה הגריל, גודל המשטח והוראות ניקוי ותחזוקה.",
            "faq": "שאלה: למה משתמשים באבני בזלת לגריל גז?\nתשובה: כדי לשפר פיזור חום בגריל, להפחית התלקחויות ולייצב את הצלייה.\nשאלה: מה ההבדל בין אבני בזלת לאבני לבה?\nתשובה: שתיהן מפזרות חום, אך חשוב לבדוק התאמה לגריל הספציפי והוראות תחזוקה.\nשאלה: איך מנקים אבני בזלת?\nתשובה: מקררים את הגריל, מסירים שאריות לפי הוראות היצרן ולא שוטפים אם ההנחיות אוסרות זאת.",
        },
        "vacuum bags": {
            "meta_title": "שקיות ואקום מחורצות 20×30 לסו-ויד ואחסון מזון | Compass Grill",
            "meta_description": "שקיות ואקום מחורצות בגודל 20×30 לשימוש עם מכונות ואקום ביתיות ומקצועיות. מתאימות לסו-ויד, הקפאה ושמירה על טריות המזון.",
            "short": "שקיות ואקום מחורצות לשמירה על טריות, הקפאה ובישול סו-ויד, עם מבנה המתאים למכונות ואקום תואמות.",
            "long": "שקיות ואקום מחורצות מיועדות לאיטום מזון במכונות ואקום תואמות, לאחסון מסודר, הקפאה ובישול סו-ויד. לפני שימוש יש לוודא התאמת גודל השקית והמכונה ולהקפיד על הוראות בטיחות מזון.",
            "faq": "שאלה: למה מיועדות שקיות ואקום מחורצות?\nתשובה: לאיטום מזון, הקפאה, שמירת טריות ובישול סו-ויד במכונה תואמת.\nשאלה: האם הן מתאימות לכל מכונת ואקום?\nתשובה: יש לבדוק התאמת רוחב וסוג השקית למכשיר לפני שימוש.",
        },
        "kazan/tandoor": {
            "meta_title": "קאזן אסייתי מברזל יצוק לבישול שטח | Compass Grill",
            "meta_description": "קאזן אסייתי מברזל יצוק לבישול שטח, קדירות, תבשילים וצלייה מעל אש. מתאים לחובבי בישול חוץ, קמפינג ומטבחי גינה.",
            "short": "קאזן או טנדור לבישול חוץ מאפשר הכנת קדירות, תבשילים וצלייה מעל אש פתוחה במטבח גינה או בקמפינג.",
            "long": "קאזן/טנדור מיועד לחובבי בישול שטח ומטבחי חוץ שרוצים לעבוד עם חום גבוה ואש חיה. מתאים להכנת קדירות, תבשילים, צלייה ובישול ארוך. יש לבדוק נפח, חומר, אביזרים תואמים והוראות שימוש לפני רכישה.",
            "faq": "שאלה: למי מתאים קאזן או טנדור?\nתשובה: לחובבי בישול חוץ, קמפינג ומטבחי גינה.\nשאלה: מה חשוב לבדוק?\nתשובה: נפח, חומר, יציבות, מקור חום ואביזרים תואמים.",
        },
        "smoker": {
            "meta_title": "מעשנת פלט מקצועית לבשר, דגים וירקות | Compass Grill",
            "meta_description": "מעשנת פלט לשליטה מדויקת בטמפרטורה, עישון ארוך וצלייה איטית. מתאימה לבריסקט, אסאדו, עוף, דגים וירקות.",
            "short": "מעשנה מאפשרת עישון ארוך וצלייה איטית עם שליטה בחום לקבלת טעמי עישון עמוקים בבשר, דגים וירקות.",
            "long": "מעשנה מיועדת להכנת בריסקט, אסאדו, עוף, דגים וירקות בעישון איטי. היתרון המרכזי הוא עבודה בטמפרטורה יציבה לאורך זמן ושילוב טעמי עץ. לפני רכישה יש לבדוק גודל תא, מקור חום, טווח טמפרטורות ואביזרים תואמים.",
            "faq": "שאלה: מה אפשר להכין במעשנה?\nתשובה: בריסקט, אסאדו, עוף, דגים, ירקות ונתחים לבישול ארוך.\nשאלה: מה חשוב לבדוק במעשנה?\nתשובה: גודל, שליטה בטמפרטורה, מקור חום ונוחות ניקוי.",
        },
        "grill": {
            "meta_title": "גריל גז מקצועי לגינה ולמטבח חוץ | Compass Grill",
            "meta_description": "גריל גז איכותי לצלייה ביתית ומקצועית, עם פיזור חום יציב, מבערים חזקים וחוויית בישול נוחה בגינה או במרפסת.",
            "short": "גריל גז לגינה או למרפסת מאפשר צלייה נוחה, חימום מהיר ושליטה טובה יותר בטמפרטורה בזמן הכנת בשר, דגים וירקות.",
            "long": "גריל גז מתאים למי שמחפש חוויית צלייה נוחה בבית, בגינה או במטבח חוץ. מומלץ להדגיש בעמוד את מספר המבערים, שטח הצלייה, חומרי הגלם, פיזור החום ואפשרויות ניקוי — רק אם הפרטים מאומתים במפרט המוצר.",
            "faq": "שאלה: איך בוחרים גריל גז?\nתשובה: בודקים שטח צלייה, מספר מבערים, חומרי גוף ורשת, ניקוי ואחריות.\nשאלה: האם גריל גז מתאים למרפסת?\nתשובה: יש לבדוק מידות, אוורור, תקנות בניין והנחיות בטיחות.",
        },
        "knives": {
            "meta_title": "סכין מקצועית לחיתוך בשר ועבודת מטבח | Compass Grill",
            "meta_description": "סכין מקצועית לחיתוך, פריסה והכנת בשר במטבח ובאזור הגריל. מתאימה לעבודה מדויקת לפני צלייה, עישון ובישול.",
            "short": "סכין מקצועית מסייעת בחיתוך מדויק של בשר וחומרי גלם לפני צלייה, עישון או בישול.",
            "long": "סכין איכותית היא כלי מרכזי בהכנת בשר וירקות לגריל או למעשנה. בעמוד מומלץ לציין סוג להב, אורך, חומר ידית ושימוש מתאים רק אם הנתונים מאומתים במפרט.",
            "faq": "שאלה: למה מיועדת סכין מקצועית?\nתשובה: לחיתוך, פריסה והכנת חומרי גלם לפני בישול או צלייה.\nשאלה: מה חשוב לבדוק?\nתשובה: אורך להב, חומר, איזון ונוחות אחיזה.",
        },
    }
    data = templates.get(family, {})
    final_name = data.get("name", name)
    product_type = final_name if re.search(r"[\u0590-\u05FF]", final_name) else name
    meta_title = data.get("meta_title") or _truncate(f"{product_type} ללקוחות Compass Grill", 58)
    meta_description = data.get("meta_description") or _truncate(
        f"{product_type} עם שימוש ברור בתחום הגריל, הבישול והמטבח החיצוני. מומלץ לבדוק מפרט, התאמה וזמינות לפני רכישה.",
        155,
    )
    short = (
        data.get("short")
        or f"{product_type} מיועד לשימוש בתחום הגריל, הבישול או מטבח החוץ, עם דגש על התאמה למוצר ולצורך בפועל."
    )
    long = (
        data.get("long")
        or f"{product_type} הוא פריט רלוונטי לעבודה עם גריל, מעשנה או מטבח חוץ. לפני עדכון התוכן יש לוודא מפרט, מידות, חומרי גלם ואביזרים תואמים כדי לא להמציא נתונים שאינם מופיעים במוצר."
    )
    faq = (
        data.get("faq")
        or f"שאלה: למי מתאים {product_type}?\nתשובה: למי שמחפש פריט רלוונטי לעבודה עם גריל, בישול חוץ או הכנת מזון.\nשאלה: מה חשוב לבדוק לפני רכישה?\nתשובה: מפרט מאומת, מידות, התאמה לציוד קיים והוראות שימוש."
    )
    return {
        "family": family,
        "name": final_name,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "h1": final_name,
        "short": short,
        "long": long,
        "faq": faq,
        "anchor": final_name,
        "sentence": f"למידע נוסף על {final_name}, עברו לעמוד {final_name} באתר Compass Grill.",
        "alt": f"{final_name} - תמונת מוצר Compass Grill",
        "schema": f"לוודא שהעמוד כולל {'CollectionPage' if entity.entity_type == 'category' else 'Product'} schema ו-FAQ schema רק לאחר הוספת FAQ מאומת.",
    }


def _quality_flags(text: str, keyword: str = "") -> list[str]:
    flags: list[str] = []
    if not re.search(r"[\u0590-\u05FF]", text or ""):
        flags.append("not Hebrew")
    if any(phrase in (text or "") for phrase in GENERIC_FORBIDDEN_PHRASES):
        flags.append("too generic")
    if keyword and keyword not in text:
        flags.append("missing keyword")
    if len(text) < 8:
        flags.append("too short")
    if len(text) > 320:
        flags.append("too long")
    return flags


def _rec(
    field_key: str,
    label: str,
    instruction: str,
    current: str,
    suggested: str,
    detected_status: str = "קיים אבל חלש",
    issue: str = "נדרש שיפור ידני",
    manual_notice: str = MANUAL_NOTICE,
) -> dict[str, Any]:
    safe_suggested = str(suggested or "").replace("<built-in method copy of dict object>", "").strip()
    return {
        "field_key": field_key,
        "label_he": label,
        "instruction_he": instruction,
        "current_value": current or "",
        "detected_status": detected_status,
        "issue_he": issue,
        "suggested_value": safe_suggested,
        "copy_text": safe_suggested,
        "manual_notice": manual_notice,
        "istore_path_he": ISTORE_PATHS.get(
            field_key, ISTORE_PATHS.get(field_key.replace("suggested_", ""), "עריכה ידנית ב-ISTORE לאחר בדיקה")
        ),
        "quality_flags": _quality_flags(safe_suggested),
        # Backward-compatible keys for older templates/tests.
        "field": label,
        "recommendation": instruction,
        "copy": safe_suggested,
    }


def _suggested_slug_if_needed(entity: AuditEntity, name: str) -> str:
    slug = urlparse(entity.url).path.rstrip("/").split("/")[-1]
    if not slug or re.search(r"[\u0590-\u05FF]", slug):
        return ""
    generic = slug.lower() in {"assman", "atman", "skiff", "product", "item"} or bool(
        re.fullmatch(r"[a-z0-9-]+", slug.lower())
    )
    if not generic:
        return ""
    tokens = re.findall(r"[\u0590-\u05FF0-9]+", name)
    return "-".join(tokens[:6])


def _ready_fixes(entity: AuditEntity, statuses: dict[str, dict[str, str]], gsc: GSCPageMetrics) -> list[dict[str, Any]]:
    copy = _specific_copy(entity, gsc)
    name = copy["name"]
    identity_unclear = name.startswith("זהות מוצר לא ברורה")
    fixes: list[dict[str, Any]] = []
    status_for = lambda key: statuses.get(key, {}).get("status", "לא נסרק — נדרש בדיקה ידנית")
    current_for = lambda key: statuses.get(key, {}).get("current_value", "")

    fixes.append(
        _rec(
            "name",
            "שם מוצר/קטגוריה מוצע בעברית",
            "להדביק בשדה השם רק לאחר אימות שזה אכן המוצר.",
            entity.name,
            name,
            status_for("h1"),
            "שם באנגלית/סלאג או שם חלש",
        )
    )
    fixes.append(
        _rec(
            "meta_title",
            "Meta title",
            "שדה לשינוי: כותרת לקידום במנוע חיפוש.",
            current_for("meta_title"),
            copy["meta_title"],
            status_for("meta_title"),
            "כותרת חסרה/חלשה או לא ממוקדת מוצר",
        )
    )
    fixes.append(
        _rec(
            "meta_description",
            "Meta description",
            "שדה לשינוי: תיאור לקידום במנוע חיפוש.",
            current_for("meta_description"),
            copy["meta_description"],
            status_for("meta_description"),
            "תיאור חסר/חלש או לא מספיק משכנע",
        )
    )
    fixes.append(
        _rec(
            "h1",
            "H1",
            "להגדיר ככותרת ראשית אחת בעמוד.",
            current_for("h1"),
            copy["h1"],
            status_for("h1"),
            "כותרת ראשית חסרה/חלשה",
        )
    )
    if entity.entity_type == "product":
        fixes.append(
            _rec(
                "short_product_description",
                "תיאור מוצר קצר",
                "להדביק בתיאור הקצר אחרי אימות מפרט ומלאי.",
                "",
                copy["short"],
                "קיים אבל חלש",
                "חסר תקציר מוצר ספציפי",
            )
        )
        fixes.append(
            _rec(
                "long_product_description",
                "תיאור מוצר ארוך",
                "להדביק בתיאור המוצר אחרי בדיקת דיוק המפרט.",
                "",
                copy["long"],
                status_for("content_length"),
                "תוכן מוצר דל או לא ממוקד",
            )
        )
    else:
        intro = copy["short"]
        if copy["family"] == "basalt stone":
            intro = "אבני בזלת לגריל גז מסייעות לשיפור פיזור חום בגריל, להפחתת התלקחויות ולצלייה יציבה יותר. בקטגוריה זו מומלץ לבדוק התאמה לגרילי גז, הבדל בין אבני בזלת מול אבני לבה והנחיות ניקוי ותחזוקה."
        fixes.append(
            _rec(
                "category_intro",
                "פתיח קטגוריה",
                "להדביק בתחילת תיאור הקטגוריה.",
                "",
                intro,
                status_for("content_length"),
                "חסר פתיח קטגוריה ממוקד",
            )
        )
        fixes.append(
            _rec(
                "long_product_description",
                "תיאור קטגוריה ארוך",
                "להדביק בתיאור הקטגוריה לאחר בדיקה ידנית.",
                "",
                copy["long"],
                status_for("content_length"),
                "חסר תוכן קטגוריה מסביר",
            )
        )
    fixes.append(
        _rec(
            "faq",
            "FAQ block",
            "להוסיף בסוף התיאור או באזור FAQ, ללא Schema אוטומטי.",
            "",
            copy["faq"],
            status_for("faq"),
            "חסר FAQ שימושי",
        )
    )
    fixes.append(
        _rec(
            "alt_text",
            "ALT לתמונות",
            "לעבור על התמונות ולהדביק ALT רק בתמונות של המוצר/קטגוריה.",
            "",
            copy["alt"],
            status_for("alt_coverage"),
            "ALT חסר/חלש",
        )
    )
    fixes.append(
        _rec(
            "internal_link",
            "Internal link anchor",
            "להשתמש כטקסט עוגן מקישור פנימי רלוונטי.",
            "",
            copy["anchor"],
            "קיים אבל חלש",
            "חסר טקסט עוגן פנימי",
        )
    )
    fixes.append(
        _rec(
            "internal_link",
            "Internal link sentence",
            "להדביק במשפט קישור פנימי מעמוד רלוונטי.",
            "",
            copy["sentence"],
            "קיים אבל חלש",
            "חסר משפט קישור פנימי",
        )
    )
    slug = _suggested_slug_if_needed(entity, name)
    if slug:
        fixes.append(
            _rec(
                "suggested_slug",
                "Slug מוצע",
                "להדביק בשדה שם ייחודי לקישור רק אם מחליטים לשנות URL ידנית ולנהל הפניות.",
                "",
                slug,
                "קיים אבל חלש",
                "סלאג באנגלית/גנרי",
            )
        )
    fixes.append(
        _rec(
            "schema",
            "Schema recommendation",
            "להעביר למפתח/אחראי אתר לבדיקה ידנית; לא מפרסם אוטומטית.",
            "",
            copy["schema"],
            status_for("schema"),
            "Schema חסר/לא מאומת",
        )
    )
    if identity_unclear:
        for fix in fixes:
            fix["detected_status"] = "לא נסרק — נדרש בדיקה ידנית"
            fix["issue_he"] = "זהות מוצר לא ברורה — נדרש בדיקה ידנית"
    return [fix for fix in fixes if fix["label_he"] and fix["copy_text"]]


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
        family = _classify_product_family(entity)
        non_food = _is_non_food(entity, family)
        identity = _identity(entity, family)
        value_score = _entity_value_score(entity, gsc)
        commercial_value_score = value_score
        traffic_opportunity_score = _traffic_opportunity_score(gsc)
        quick_win = 5 <= gsc.average_position <= 15 and gsc.ctr < 0.02 and gsc.impressions > 0
        traffic_loss_score = _traffic_loss_score(gsc)
        priority_reason = _priority_reason(entity, statuses, gsc, seo_score, data_confidence_score, value_score)
        priority_score = (
            (1000 if entity.entity_type == "category" else 0)
            + (500 if gsc.impressions or gsc.clicks else 0)
            + (
                350
                if non_food
                else -120
                if family == "meat_food" and not (gsc.impressions or gsc.clicks)
                else 0
            )
            + (300 if quick_win else 0)
            + value_score * 2
            + traffic_opportunity_score * 3
            + traffic_loss_score * 4
            + missing_confirmed_count * 45
            + max(0, 100 - seo_score) * 2
            - unknown_count * 35
            - (500 if identity.get("unclear") and not (gsc.impressions or gsc.clicks) else 0)
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
                "product_family": family,
                "is_non_food": non_food,
                "identity_unclear": bool(identity.get("unclear")),
                "commercial_value_score": commercial_value_score,
                "traffic_opportunity_score": traffic_opportunity_score,
                "quick_win": quick_win,
                "value_score": value_score,
                "traffic_loss_score": traffic_loss_score,
                "revenue_risk": _estimated_revenue_risk(entity, gsc),
                "ready_to_copy_fixes": _ready_fixes(entity, statuses, gsc),
                "confirmed_problems": [
                    status["label"]
                    for status in statuses.values()
                    if _field_state(status) in {"missing_confirmed", "weak"}
                ],
                "unknown_fields": [
                    status["label"] for status in statuses.values() if _field_state(status) == "unknown"
                ],
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
            int(item.get("identity_unclear", False)) if not int(item["gsc"]["impressions"]) else 0,
            -int(item.get("is_non_food", False)),
            -int(item.get("quick_win", False)),
            -int(item["gsc"]["impressions"]),
            -int(item["gsc"]["clicks"]),
            -int(item["traffic_opportunity_score"]),
            -int(item["value_score"]),
            -int(item["missing_confirmed_count"]),
            float(item["seo_score"]),
            int(item["unknown_count"]),
            -float(item["priority_score"]),
            str(item["name"]),
        ),
    )[:limit]
    default_top_20_work_queue = [
        item
        for item in audits
        if item["product_family"] != "meat_food" or int(item["traffic_opportunity_score"]) >= 80
    ][:20]
    categories = [item for item in audits if item["entity_type"] == "category"]
    products = [item for item in audits if item["entity_type"] == "product"]
    return {
        "summary": {
            "total_entities": len(audits),
            "categories": len(categories),
            "products": len(products),
            "high_value_products": sum(1 for item in products if int(item["value_score"]) >= 40),
            "with_gsc_impressions": sum(1 for item in audits if int(item["gsc"]["impressions"]) > 0),
            "losing_traffic": sum(
                1
                for item in audits
                if int(item["gsc"]["clicks_delta"]) < 0 or int(item["gsc"]["impressions_delta"]) < 0
            ),
            "low_data_confidence": sum(1 for item in audits if int(item["data_confidence_score"]) < 70),
            "confirmed_missing_meta": sum(
                1
                for item in audits
                if any(
                    _field_state(item["statuses"][key]) == "missing_confirmed"
                    for key in ("meta_title", "meta_description")
                )
            ),
            "unknown_scan_data": sum(1 for item in audits if int(item["unknown_count"]) > 0),
            "safety": "אין פרסום אוטומטי ואין עריכת תוכן חי.",
        },
        "filters": [
            "show only non-food products",
            "show only categories",
            "show only GSC opportunities",
            "show only 404 tasks",
            "show only confirmed issues",
            "hide unknown scan data",
            "non-food focus enabled by default",
            "hide food/meat products",
            "show only quick wins",
            "show only high commercial value",
            "show only low CTR",
            "show only position 5-15",
        ],
        "prioritization": [
            "GSC 404 עם חלופה",
            "קטגוריות לא-מזון",
            "מוצרים לא-מזון תחילה כברירת מחדל",
            "מוצרי בשר/מזון רק לאחר מוצרי non-food או עם הזדמנות GSC גבוהה",
            "מיקום 5-15 עם CTR נמוך",
            "ירידת חשיפות/קליקים",
            "ערך מסחרי גבוה",
            "Meta חסר/חלש מאומת",
            "תוכן דל מאומת",
            "נתוני סריקה לא ידועים בסוף",
        ],
        "non_food_focus_default": True,
        "top_20_work_queue": default_top_20_work_queue,
        "audits": audits,
        "categories": categories,
        "products": products,
    }
