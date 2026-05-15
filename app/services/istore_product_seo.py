from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any

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
            "image_count": self.image_count,
            "price": self.price,
        }


def analyze_istore_product_seo(product: dict[str, Any]) -> ProductSEOAnalysis:
    """Build a deterministic, read-only SEO analysis for a raw ISTORE product."""
    product_id = _first_text(product, ("id", "product_id", "sku", "catalog_number", "item_id")) or "unknown-product"
    name = _first_text(product, ("name", "title", "product_name", "item_name")) or product_id
    url = _first_text(product, ("url", "link", "product_url", "canonical_url"))
    category = _category(product)
    title = _first_text(product, ("meta_title", "seo_title", "page_title")) or ""
    meta_description = _first_text(
        product,
        ("meta_description", "seo_description", "description_short", "short_description", "subtitle"),
    ) or ""
    raw_description = (
        _first_text(product, ("description", "description_html", "long_description", "body", "content")) or ""
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
        issues.append("Missing SEO title")
        recommendations.append(
            "Add a focused product SEO title that includes the product name and primary buying intent."
        )
    elif title_length < MIN_TITLE_LENGTH or title_length > MAX_TITLE_LENGTH:
        score -= 10
        issues.append(f"SEO title length is {title_length} characters")
        recommendations.append("Keep the product SEO title between 30 and 60 characters.")

    meta_length = len(meta_description)
    if not meta_description:
        score -= 20
        issues.append("Missing meta description")
        recommendations.append("Add a benefit-led meta description with product details and a clear reason to click.")
    elif meta_length < MIN_META_DESCRIPTION_LENGTH or meta_length > MAX_META_DESCRIPTION_LENGTH:
        score -= 10
        issues.append(f"Meta description length is {meta_length} characters")
        recommendations.append("Keep the meta description between 70 and 160 characters.")

    description_length = len(description_text)
    if description_length < MIN_DESCRIPTION_LENGTH:
        score -= 15
        issues.append(f"Product description has only {description_length} characters")
        recommendations.append(
            "Expand the product description with materials, use cases, sizing, warranty, and delivery details."
        )

    if image_count == 0:
        score -= 10
        issues.append("No product images detected")
        recommendations.append("Add descriptive product images with meaningful alt text.")

    if not category:
        score -= 5
        issues.append("Missing product category")
        recommendations.append("Assign the product to a clear category to strengthen internal linking and breadcrumbs.")

    if not url:
        score -= 5
        issues.append("Missing product URL")
        recommendations.append("Expose a canonical product URL for indexing and reporting.")

    suggested_title = _clip_text(f"{name} | Compass", MAX_TITLE_LENGTH)
    suggested_h1 = name
    suggested_meta_description = _clip_text(
        _suggested_meta_description(name, category, price),
        MAX_META_DESCRIPTION_LENGTH,
    )

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
        image_count=image_count,
        price=price,
    )


def _first_text(product: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return _clean_text(value)
        if isinstance(value, int | float):
            return str(value)
    return None


def _clean_text(value: str) -> str:
    text = unescape(_HTML_TAG_RE.sub(" ", value))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _category(product: dict[str, Any]) -> str | None:
    direct = _first_text(product, ("category", "category_name", "department"))
    if direct:
        return direct
    categories = product.get("categories")
    if isinstance(categories, list) and categories:
        first = categories[0]
        if isinstance(first, str):
            return _clean_text(first)
        if isinstance(first, dict):
            return _first_text(first, ("name", "title", "label"))
    return None


def _image_count(product: dict[str, Any]) -> int:
    images = product.get("images") or product.get("gallery") or product.get("media")
    if isinstance(images, list):
        return len([image for image in images if image])
    if _first_text(product, ("image", "image_url", "main_image")):
        return 1
    return 0


def _price(product: dict[str, Any]) -> str | None:
    value = product.get("price") or product.get("sale_price") or product.get("regular_price")
    if isinstance(value, str) and value.strip():
        return _clean_text(value)
    if isinstance(value, int | float):
        return f"{value:g}"
    return None


def _clip_text(value: str, max_length: int) -> str:
    value = _clean_text(value)
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"


def _suggested_meta_description(name: str, category: str | None, price: str | None) -> str:
    parts = [f"קנו {name}"]
    if category:
        parts.append(f"בקטגוריית {category}")
    if price:
        parts.append(f"במחיר {price}")
    parts.append("עם מידע מלא, תמונות מוצר ושירות מקצועי מבית Compass.")
    return " ".join(parts)