from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from app.services.seo_copy_quality import sanitize_generated_seo_copy, truncate_without_ellipsis

MIN_TITLE_LENGTH = 30
MAX_TITLE_LENGTH = 60
MIN_META_DESCRIPTION_LENGTH = 70
MAX_META_DESCRIPTION_LENGTH = 160
MIN_DESCRIPTION_LENGTH = 250
UNKNOWN_MANUAL_REVIEW = "Unknown – manual review required"
NEEDS_REVIEW = "Needs Review"
NO_INTERNAL_LINKS = "No internal link opportunities found"
PRODUCT_FAMILIES = (
    "plancha",
    "cast_iron",
    "grill",
    "smoker",
    "tandoor",
    "kazan",
    "vacuum",
    "sous_vide",
    "accessories",
    "wood_chunks",
    "pizza_oven",
    "charcoal_grill",
    "gas_grill",
    "fire_pit",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_HEBREW_RE = re.compile(r"[\u0590-\u05ff]")

_FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sous_vide": ("סו ויד", "סו-ויד", "sous vide", "sous-vide"),
    "wood_chunks": ("צ'אנקים", "צאנקים", "chunks", "wood chunks", "עץ לעישון", "שבבי עישון"),
    "vacuum": ("ואקום", "vacuum", "שקיות ואקום", "מכונת ואקום"),
    "plancha": ("פלנצ'ה", "פלנצה", "plancha"),
    "pizza_oven": ("טאבון", "תנור פיצה", "pizza oven", "tabun"),
    "charcoal_grill": ("גריל פחם", "פחמים", "charcoal grill"),
    "gas_grill": ("גריל גז", "gas grill"),
    "smoker": ("מעשנת", "עישון", "smoker"),
    "tandoor": ("טנדור", "tandoor"),
    "kazan": ("קאזאן", "קזן", "kazan"),
    "fire_pit": ("מדורת גן", "fire pit", "firepit"),
    "cast_iron": ("יציקת ברזל", "ברזל יצוק", "cast iron"),
    "accessories": ("כפפה", "אביזר", "אביזרים", "accessory", "accessories", "כיסוי", "מברשת", "מדחום"),
    "grill": ("גריל", "grill", "bbq", "barbecue"),
}

_FAMILY_META: dict[str, str] = {
    "plancha": "קנו {name} מבית קומפס: פלנצ'ה איכותית לצלייה מדויקת, פיזור חום אחיד ותחזוקה קלה בבית או בחוץ.",
    "cast_iron": "קנו {name} מבית קומפס: כלי יציקת ברזל עמיד לשמירת חום, צריבה איכותית ושימוש ארוך שנים.",
    "grill": "קנו {name} מבית קומפס: פתרון צלייה איכותי עם מפרט ברור, התאמה לצרכים ושירות מקצועי.",
    "gas_grill": "קנו {name} מבית קומפס: גריל גז איכותי לגינה עם מפרט ברור, ביצועי צלייה ושירות מקצועי.",
    "charcoal_grill": "קנו {name} מבית קומפס: גריל פחם לחוויית צלייה אותנטית, חום גבוה וטעם מעושן.",
    "smoker": "קנו {name} מבית קומפס: מעשנה איכותית לבישול ארוך, שליטה בחום וטעמי עישון עמוקים.",
    "tandoor": "קנו {name} מבית קומפס: טנדור איכותי לאפייה וצלייה בחום גבוה עם תוצאה אותנטית.",
    "kazan": "קנו {name} מבית קומפס: קאזאן עמיד לבישול שטח, תבשילים עשירים ופיזור חום אחיד.",
    "vacuum": "קנו {name} מבית קומפס: פתרון ואקום לשמירת טריות מזון, אחסון נוח והכנה להקפאה או בישול.",
    "sous_vide": "קנו {name} מבית קומפס: ציוד סו ויד לבישול מדויק בטמפרטורה קבועה ותוצאות עקביות בבית.",
    "accessories": "קנו {name} מבית קומפס: אביזר איכותי לשימוש נוח, בטיחותי ומדויק במטבח או בחוץ.",
    "wood_chunks": "קנו {name} מבית קומפס: צ'אנקים לעישון להוספת ארומת עץ, עומק טעם והתאמה לסוגי מזון שונים.",
    "pizza_oven": "קנו {name} מבית קומפס: טאבון איכותי לפיצה, מאפים וצלייה בחום גבוה עם תוצאה פריכה.",
    "fire_pit": "קנו {name} מבית קומפס: מדורת גן איכותית לאווירה, חימום ושימוש חוץ נוח ובטוח.",
}

_FAMILY_FAQ: dict[str, list[str]] = {
    "plancha": [
        "לאילו כיריים או מתקני צלייה הפלנצ'ה מתאימה?",
        "איך מנקים ומתחזקים פלנצ'ה לאחר שימוש?",
        "איך מבצעים seasoning לפלנצ'ה לפני שימוש ראשון?",
    ],
    "vacuum": [
        "כמה זמן שקיות ואקום עוזרות לשמור מזון?",
        "האם שקיות ואקום מתאימות להקפאה?",
        "מה עובי השקיות ולמה הוא חשוב?",
    ],
    "wood_chunks": [
        "איזה טעם עישון נותן סוג העץ הזה?",
        "לאילו סוגי מזון מתאים זן העץ?",
        "איך משתמשים בצ'אנקים לעישון בצורה נכונה?",
    ],
}

_SLUG_WORDS = {
    "פלנצ'ה": "plancha",
    "פלנצה": "plancha",
    "עגולה": "round",
    "יציקת": "cast",
    "ברזל": "iron",
    "שקיות": "bags",
    "ואקום": "vacuum",
    "מיכל": "container",
    "סו": "sous",
    "ויד": "vide",
    "צ'אנקים": "wood-chunks",
    "צאנקים": "wood-chunks",
    "לעישון": "smoking",
    "כפפה": "glove",
    "נגד": "heat",
    "חום": "resistant",
    "גריל": "grill",
    "גז": "gas",
    "פחם": "charcoal",
    "מעשנת": "smoker",
    "טאבון": "pizza-oven",
}


@dataclass(frozen=True)
class ProductSEOAnalysis:
    """Read-only SEO analysis for an ISTORE product payload."""

    product_id: str
    name: str
    url: str | None
    category: str | None
    title: str
    meta_description: str
    description_text: str
    score: int | str
    confidence: str
    status: str
    product_family: str
    issues: list[str]
    recommendations: list[str]
    suggested_title: str
    suggested_meta_description: str
    suggested_h1: str
    suggested_slug: str
    faq_recommendations: list[str]
    internal_link_opportunities: list[str]
    image_count: int
    price: str | None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def analyze_istore_product_seo(payload: dict[str, Any]) -> ProductSEOAnalysis:
    """Build a deterministic, read-only SEO analysis for a raw ISTORE product."""
    product = _product_payload(payload)
    description_data = _first_description(product)

    product_id = _first_text(product, ("id", "product_id", "sku", "catalog_number", "item_id")) or "unknown-product"
    name = (
        _first_text(description_data, ("name", "title", "product_name", "item_name"))
        or _first_text(product, ("name", "title", "product_name", "item_name"))
        or product_id
    )
    url = _first_text(product, ("url", "link", "product_url", "canonical_url"))
    slug = _first_text(product, ("slug", "url_slug", "normalized_slug")) or _slug_from_url(url)
    category = _category(product)

    title = (
        _first_text(description_data, ("meta_title", "seo_title", "page_title"))
        or _first_text(product, ("meta_title", "seo_title", "page_title"))
        or ""
    )
    meta_description = (
        _first_text(
            description_data,
            ("meta_description", "seo_description", "description_short", "short_description", "subtitle"),
        )
        or _first_text(
            product, ("meta_description", "seo_description", "description_short", "short_description", "subtitle")
        )
        or ""
    )
    raw_description = (
        _first_text(description_data, ("description", "description_html", "long_description", "body", "content"))
        or _first_text(product, ("description", "description_html", "long_description", "body", "content"))
        or ""
    )
    h1 = _first_text(description_data, ("h1", "page_h1")) or _first_text(product, ("h1", "page_h1"))

    product_family = classify_product_family(name=name, slug=slug, category=category, description=raw_description)
    description_text = _clean_text(raw_description)
    image_count = _image_count(product)
    price = _price(product)
    scanned = _scanned_fields(product, description_data)
    unknown_ratio = _unknown_ratio(scanned)
    live_data_unavailable = not any(scanned.values()) or (
        not url and not title and not meta_description and not raw_description and image_count == 0
    )

    issues: list[str] = []
    recommendations: list[str] = []
    score = 100

    if live_data_unavailable:
        status = NEEDS_REVIEW
        score_value: int | str = "Unknown"
        confidence = "Low"
        recommendations.append(NEEDS_REVIEW)
    else:
        status = "Ready"
        confidence = "Low" if unknown_ratio > 0.40 else "High"
        score_value = "Unknown" if unknown_ratio > 0.40 else score

    if scanned["title"]:
        title_length = len(title)
        if not title:
            score -= 20
            issues.append("חסרה כותרת SEO")
            recommendations.append("להוסיף כותרת SEO ממוקדת בעברית לפי משפחת המוצר וכוונת קנייה.")
        elif title_length < MIN_TITLE_LENGTH or title_length > MAX_TITLE_LENGTH:
            score -= 10
            issues.append(f"אורך כותרת ה-SEO הוא {title_length} תווים")
            recommendations.append("לשמור על כותרת SEO באורך 30 עד 60 תווים.")

    if scanned["meta_description"]:
        meta_length = len(meta_description)
        if not meta_description:
            score -= 20
            issues.append("חסר תיאור מטא")
            recommendations.append("להוסיף תיאור מטא בעברית לפי משפחת המוצר בלבד.")
        elif meta_length < MIN_META_DESCRIPTION_LENGTH or meta_length > MAX_META_DESCRIPTION_LENGTH:
            score -= 10
            issues.append(f"אורך תיאור המטא הוא {meta_length} תווים")
            recommendations.append("לשמור על תיאור מטא באורך 70 עד 160 תווים.")

    if scanned["description"]:
        description_length = len(description_text)
        if description_length < MIN_DESCRIPTION_LENGTH:
            score -= 15
            issues.append(f"תיאור המוצר כולל רק {description_length} תווים")
            recommendations.append("להרחיב את תיאור המוצר רק על בסיס פרטים שנסרקו בעמוד.")
    else:
        issues.append(f"תיאור מוצר: {UNKNOWN_MANUAL_REVIEW}")

    if scanned["alt"]:
        recommendations.append("לבדוק שכל תמונות המוצר כוללות ALT תיאורי ורלוונטי למשפחת המוצר.")
    else:
        recommendations.append(f"ALT לתמונות: {UNKNOWN_MANUAL_REVIEW}")

    if image_count == 0 and scanned["alt"]:
        score -= 10
        issues.append("לא זוהו תמונות מוצר")

    if not category:
        score -= 5
        issues.append("חסרה קטגוריית מוצר")
    if not url:
        score -= 5
        issues.append("חסר URL מוצר")

    if isinstance(score_value, int):
        score_value = max(score, 0)

    suggested_title = (
        sanitize_generated_seo_copy(_clip_text(f"{name} | קומפס", MAX_TITLE_LENGTH))
        if scanned["title"]
        else UNKNOWN_MANUAL_REVIEW
    )
    suggested_h1 = h1 if scanned["h1"] and h1 else UNKNOWN_MANUAL_REVIEW
    suggested_meta_description = UNKNOWN_MANUAL_REVIEW
    if scanned["meta_description"]:
        suggested_meta_description = sanitize_generated_seo_copy(
            _clip_text(_suggested_meta_description(name, product_family), MAX_META_DESCRIPTION_LENGTH)
        )
    suggested_slug = _english_slug(name, product_family)
    faq_recommendations = _faq_for_family(product_family)
    internal_links = _internal_links(product)

    _validate_family_content(
        product_family, (suggested_title, suggested_meta_description, " ".join(faq_recommendations))
    )

    return ProductSEOAnalysis(
        product_id,
        name,
        url,
        category,
        title,
        meta_description,
        description_text,
        score_value,
        confidence,
        status,
        product_family,
        issues,
        recommendations,
        suggested_title,
        suggested_meta_description,
        suggested_h1,
        suggested_slug,
        faq_recommendations,
        internal_links,
        image_count,
        price,
    )


def classify_product_family(*, name: str, slug: str | None, category: str | None, description: str | None) -> str:
    haystack = " ".join(
        part for part in (name, slug or "", category or "", _clean_text(description or "")) if part
    ).lower()
    for family, keywords in _FAMILY_KEYWORDS.items():
        if any(keyword.lower() in haystack for keyword in keywords):
            return family
    return "accessories"


def _scanned_fields(product: dict[str, Any], description_data: dict[str, Any]) -> dict[str, bool]:
    return {
        "title": _has_any_key(product, description_data, ("meta_title", "seo_title", "page_title")),
        "meta_description": _has_any_key(
            product,
            description_data,
            ("meta_description", "seo_description", "description_short", "short_description", "subtitle"),
        ),
        "description": _has_any_key(
            product, description_data, ("description", "description_html", "long_description", "body", "content")
        ),
        "h1": _has_any_key(product, description_data, ("h1", "page_h1")),
        "alt": _images_include_alt(product),
        "url": _has_any_key(product, description_data, ("url", "link", "product_url", "canonical_url")),
        "category": bool(_category(product)),
    }


def _has_any_key(product: dict[str, Any], description_data: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in product or key in description_data for key in keys)


def _unknown_ratio(scanned: dict[str, bool]) -> float:
    return list(scanned.values()).count(False) / max(len(scanned), 1)


def _images_include_alt(product: dict[str, Any]) -> bool:
    images = product.get("images") or product.get("gallery") or product.get("media")
    return _nested_has_key(images, ("alt", "alt_text", "title"))


def _nested_has_key(value: Any, keys: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in keys) or any(_nested_has_key(item, keys) for item in value.values())
    if isinstance(value, list):
        return any(_nested_has_key(item, keys) for item in value)
    return False


def _suggested_meta_description(name: str, family: str) -> str:
    return _FAMILY_META.get(family, _FAMILY_META["accessories"]).format(name=name)


def _faq_for_family(family: str) -> list[str]:
    return _FAMILY_FAQ.get(
        family, ["מה חשוב לבדוק לפני קנייה?", "איך מתחזקים את המוצר לאורך זמן?", "לאיזה שימושים המוצר מתאים?"]
    )


def _internal_links(product: dict[str, Any]) -> list[str]:
    candidates = (
        product.get("internal_link_opportunities") or product.get("relevant_pages") or product.get("internal_links")
    )
    if isinstance(candidates, list) and candidates:
        links = [_text_value(item) for item in candidates]
        return [link for link in links if link]
    return [NO_INTERNAL_LINKS]


def _english_slug(name: str, family: str) -> str:
    if family == "plancha" and ("עגולה" in name or "round" in name.lower()):
        return "cast-iron-round-plancha" if "ברזל" in name or "cast" in name.lower() else "round-plancha"
    words: list[str] = []
    for raw in re.split(r"[\s_/|–—-]+", name):
        cleaned = raw.strip(".,:;()[]{}\"'")
        if not cleaned:
            continue
        if _HEBREW_RE.search(cleaned):
            mapped = _SLUG_WORDS.get(cleaned)
            if mapped:
                words.extend(mapped.split("-"))
        else:
            words.append(re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-"))
    if family not in words:
        words.append(family.replace("_", "-"))
    slug = "-".join(word for word in words if word)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or family.replace("_", "-")


def _validate_family_content(family: str, content_parts: tuple[str, ...]) -> None:
    content = " ".join(content_parts).lower()
    forbidden = {
        "plancha": ("gas grill", "גריל גז"),
        "sous_vide": ("grill", "גריל"),
        "vacuum": ("grill", "גריל"),
        "wood_chunks": ("sous vide", "סו ויד", "ואקום"),
    }.get(family, ())
    if any(term in content for term in forbidden):
        raise ValueError(f"FAIL VALIDATION: product family {family} conflicts with generated content")


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").split("/")[-1] or None


def _product_payload(payload: dict[str, Any]) -> dict[str, Any]:
    product = payload.get("product")
    if isinstance(product, dict):
        return product
    return payload


def _first_description(product: dict[str, Any]) -> dict[str, Any]:
    descriptions = product.get("product_description")
    if isinstance(descriptions, dict):
        preferred = descriptions.get("3")
        if isinstance(preferred, dict):
            return preferred

        for value in descriptions.values():
            if isinstance(value, dict):
                return value

    return {}


def _first_text(product: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = product.get(key)
        text = _text_value(value)
        if text:
            return text
    return None


def _text_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return _clean_text(value)
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "title", "label", "value", "text", "description", "content", "html"):
            nested = _text_value(value.get(key))
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _text_value(item)
            if nested:
                return nested
    return None


def _clean_text(value: str) -> str:
    text = unescape(value)
    text = _HTML_TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _category(product: dict[str, Any]) -> str | None:
    direct = _first_text(product, ("category", "category_name", "department"))
    if direct:
        return direct

    category = product.get("category")
    nested_category = _category_name(category)
    if nested_category:
        return nested_category

    categories = product.get("categories")
    return _category_name(categories)


def _category_name(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return _clean_text(value)
    if isinstance(value, dict):
        direct = _first_text(value, ("name", "title", "label", "category_name"))
        if direct:
            return direct
        for nested_key in ("category", "parent", "primary", "data", "item"):
            nested = _category_name(value.get(nested_key))
            if nested:
                return nested
        for nested_value in value.values():
            nested = _category_name(nested_value)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _category_name(item)
            if nested:
                return nested
    return None


def _image_count(product: dict[str, Any]) -> int:
    images = product.get("images") or product.get("gallery") or product.get("media")
    count = _count_images(images)
    if count:
        return count
    if _first_text(product, ("image", "image_url", "main_image")):
        return 1
    return 0


def _count_images(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip() else 0

    if isinstance(value, dict):
        if any(
            _text_value(value.get(key))
            for key in (
                "url",
                "src",
                "image",
                "image_url",
                "main_image",
                "thumbnail",
                "path",
            )
        ):
            return 1

        return sum(_count_images(item) for item in value.values())

    if isinstance(value, list):
        return sum(_count_images(item) for item in value)

    return 0


def _price(product: dict[str, Any]) -> str | None:
    value = product.get("price") or product.get("sale_price") or product.get("regular_price")
    if isinstance(value, str) and value.strip():
        return _clean_text(value)
    if isinstance(value, int | float):
        return f"{value:g}"
    return None


def _clip_text(value: str, max_length: int) -> str:
    return truncate_without_ellipsis(_clean_text(value), max_length)
