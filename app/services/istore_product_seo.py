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

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


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
    score: int
    issues: list[str]
    recommendations: list[str]
    suggested_title: str
    suggested_meta_description: str
    suggested_h1: str
    suggested_slug: str
    detected_family: str
    confidence_score: int
    review_status: str
    image_count: int
    price: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "url": self.url,
            "category": self.category,
            "title": self.title,
            "meta_description": self.meta_description,
            "description_text": self.description_text,
            "score": self.score,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "suggested_title": self.suggested_title,
            "suggested_meta_description": self.suggested_meta_description,
            "suggested_h1": self.suggested_h1,
            "suggested_slug": self.suggested_slug,
            "detected_family": self.detected_family,
            "confidence_score": self.confidence_score,
            "review_status": self.review_status,
            "image_count": self.image_count,
            "price": self.price,
        }


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
            product,
            ("meta_description", "seo_description", "description_short", "short_description", "subtitle"),
        )
        or ""
    )
    raw_description = (
        _first_text(description_data, ("description", "description_html", "long_description", "body", "content"))
        or _first_text(product, ("description", "description_html", "long_description", "body", "content"))
        or ""
    )

    description_text = _clean_text(raw_description)
    image_count = _image_count(product)
    price = _price(product)

    issues: list[str] = []
    recommendations: list[str] = []
    score = 100

    title_length = len(title)
    if not title:
        score -= 20
        issues.append("חסרה כותרת SEO")
        recommendations.append(
            "להוסיף כותרת SEO ממוקדת בעברית שכוללת את שם המוצר וכוונת קנייה."
        )
    elif title_length < MIN_TITLE_LENGTH or title_length > MAX_TITLE_LENGTH:
        score -= 10
        issues.append(f"אורך כותרת ה-SEO הוא {title_length} תווים")
        recommendations.append("לשמור על כותרת SEO באורך 30 עד 60 תווים.")

    meta_length = len(meta_description)
    if not meta_description:
        score -= 20
        issues.append("חסר תיאור מטא")
        recommendations.append("להוסיף תיאור מטא בעברית עם יתרונות, פרטי מוצר וסיבה ברורה להיכנס לעמוד.")
    elif meta_length < MIN_META_DESCRIPTION_LENGTH or meta_length > MAX_META_DESCRIPTION_LENGTH:
        score -= 10
        issues.append(f"אורך תיאור המטא הוא {meta_length} תווים")
        recommendations.append("לשמור על תיאור מטא באורך 70 עד 160 תווים.")

    description_length = len(description_text)
    if description_length < MIN_DESCRIPTION_LENGTH:
        score -= 15
        issues.append(f"תיאור המוצר כולל רק {description_length} תווים")
        recommendations.append(
            "להרחיב את תיאור המוצר עם חומרים, שימושים, מידות, אחריות ופרטי משלוח."
        )

    if image_count == 0:
        score -= 10
        issues.append("לא זוהו תמונות מוצר")
        recommendations.append("להוסיף תמונות מוצר מתארות עם טקסט חלופי משמעותי.")

    if not category:
        score -= 5
        issues.append("חסרה קטגוריית מוצר")
        recommendations.append("לשייך את המוצר לקטגוריה ברורה כדי לחזק קישורים פנימיים ופירורי לחם.")

    if not url:
        score -= 5
        issues.append("חסר URL מוצר")
        recommendations.append("להציג כתובת קנונית למוצר לצורך אינדוקס ודיווח.")

    suggested_title = sanitize_generated_seo_copy(_clip_text(f"{name} | קומפס", MAX_TITLE_LENGTH))
    suggested_h1 = name
    suggested_meta_description = sanitize_generated_seo_copy(
        _clip_text(_suggested_meta_description(name, category, price), MAX_META_DESCRIPTION_LENGTH)
    )
    detected_family, confidence_score = detect_istore_product_family(
        name=name,
        url=url,
        category=category,
        keyword=_first_text(product, ("keyword", "slug", "normalized_slug")),
    )
    review_status = "Needs Review" if confidence_score < 80 else "Ready for manual review"

    return ProductSEOAnalysis(
        product_id=product_id,
        name=name,
        url=url,
        category=category,
        title=title,
        meta_description=meta_description,
        description_text=description_text,
        score=max(score, 0),
        issues=issues,
        recommendations=recommendations,
        suggested_title=suggested_title,
        suggested_meta_description=suggested_meta_description,
        suggested_h1=suggested_h1,
        suggested_slug=suggest_english_product_slug(name, detected_family),
        detected_family=detected_family,
        confidence_score=confidence_score,
        review_status=review_status,
        image_count=image_count,
        price=price,
    )


def detect_istore_product_family(
    *, name: str, url: str | None = None, category: str | None = None, keyword: str | None = None
) -> tuple[str, int]:
    """Return the corrected ISTORE product family classification and confidence."""
    text = f"{name} {url or ''} {category or ''} {keyword or ''}".lower()
    rules: list[tuple[str, tuple[str, ...], int]] = [
        ("kazan", ("קאזן", "kazan"), 96),
        ("tandoor", ("טנדור", "tandoor", "persian", "roma"), 94),
        ("meat_food", ("אסאדו", "אנטריקוט", "המבורגר", "סטייק", "בשר", "beef", "steak"), 90),
        ("basalt stone", ("בזלת", "basalt"), 90),
        ("vacuum bags", ("שקיות ואקום", "ואקום", "vacuum bag", "grooved bags"), 90),
        ("sous vide", ("סו-ויד", "sous vide", "anova"), 88),
        ("smoker", ("מעשנה", "smoker", "פלט"), 88),
        ("grill", ("גריל", "מנגל", "grill", "bbq"), 86),
        ("taboon", ("טאבון", "taboon", "pizza oven"), 86),
        ("pizza stone", ("אבן פיצה", "pizza stone"), 86),
        ("wood chips/chunks", ("שבבי עץ", "צ׳אנק", "צ'אנק", "wood chips", "chunks"), 86),
        ("charcoal/firewood", ("פחם", "עצי הסקה", "charcoal", "firewood"), 86),
        ("thermometer", ("מדחום", "thermometer"), 86),
        ("butcher paper", ("נייר קצבים", "butcher paper"), 86),
        ("knives", ("סכין", "סכינים", "knife", "knives"), 86),
        ("skewers", ("שיפוד", "שיפודים", "skewer"), 86),
        ("gloves", ("כפפות", "gloves", "glove"), 82),
        ("burners", ("מבער", "מבערים", "burner", "burners"), 82),
        ("grill accessories", ("אביזר", "אביזרים", "accessories", "accessory"), 75),
        ("cast iron cookware", ("ברזל יצוק", "מחבת", "סיר", "cast iron"), 84),
        ("outdoor kitchen", ("מטבח חוץ", "outdoor kitchen"), 86),
        ("fireplace/fire pit", ("מדורה", "קמין", "fire pit", "fireplace"), 84),
    ]
    for family, tokens, confidence in rules:
        if any(token in text for token in tokens):
            return family, confidence
    return "unknown", 35


_SLUG_TOKEN_MAP = {
    "kazan": "kazan",
    "אסייתי": "asian",
    "ליטר": "liter",
    "מכסה": "lid",
    "ברזל": "cast-iron",
    "יצוק": "cast-iron",
    "טנדור": "tandoor",
    "גריל": "grill",
    "מעשנה": "smoker",
}


def suggest_english_product_slug(name: str, family: str) -> str:
    """Build an English-only slug suggestion from the detected family and known product tokens."""
    parts = [family.replace(" ", "-").replace("/", "-")] if family != "unknown" else []
    for token in re.findall(r"[\w\u0590-\u05FF]+", name.lower()):
        mapped = _SLUG_TOKEN_MAP.get(token)
        if mapped:
            parts.extend(mapped.split("-"))
        elif token.isascii() and re.search(r"[a-z0-9]", token):
            parts.append(token)
        elif token.isdigit():
            parts.append(token)
    deduped: list[str] = []
    for part in parts:
        if part and part not in deduped:
            deduped.append(part)
    slug = "-".join(deduped[:8])
    return re.sub(r"[^a-z0-9-]+", "-", slug).strip("-") or "product-manual-review"


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


def _suggested_meta_description(name: str, category: str | None, price: str | None) -> str:
    parts = [f"קנו {name}"]
    if category:
        parts.append(f"בקטגוריית {category}")
    if price:
        parts.append(f"במחיר {price}")
    parts.append("עם מידע מלא, תמונות מוצר ושירות מקצועי מבית קומפס.")
    return " ".join(parts)
