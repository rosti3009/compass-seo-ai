from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

SEO_BASICS = {"title", "meta_description", "h1"}


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _get(page: Any, key: str, default: Any = None) -> Any:
    if isinstance(page, dict):
        return page.get(key, default)
    return getattr(page, key, default)


def page_url_depth(url: str) -> int:
    """Estimate crawl depth from URL path segments when a crawler depth field is unavailable."""
    path = urlparse(url).path.strip("/")
    if not path:
        return 0
    return len([segment for segment in path.split("/") if segment])


def page_missing_fields(page: Any) -> set[str]:
    """Return normalized missing title/meta/H1 signals for a page dict or PageAudit model."""
    raw_missing = _get(page, "missing_fields", "")
    if isinstance(raw_missing, str):
        missing_fields = {field.strip() for field in raw_missing.split(",") if field.strip()}
    elif isinstance(raw_missing, list | tuple | set):
        missing_fields = {str(field).strip() for field in raw_missing if str(field).strip()}
    else:
        missing_fields = set()

    for field in SEO_BASICS:
        if not _text(_get(page, field)).strip():
            missing_fields.add(field)
    return missing_fields


def authority_score(page: Any) -> int:
    """Score a source page's internal-link authority from 0 to 100."""
    seo_component = min(max(_number(_get(page, "seo_score")), 0), 100) * 0.50
    link_count = _number(_get(page, "internal_links", _get(page, "internal_links_count")))
    links_component = min(max(link_count, 0), 50) / 50 * 20
    word_component = min(max(_number(_get(page, "word_count")), 0), 2000) / 2000 * 20
    depth = _get(page, "crawl_depth", None)
    if depth is None:
        depth = _get(page, "depth", None)
    if depth is None:
        depth = page_url_depth(_text(_get(page, "url")))
    depth_component = max(0, 10 - min(max(_number(depth), 0), 5) * 2)
    return round(seo_component + links_component + word_component + depth_component)


def opportunity_score(page: Any, task: Any | None = None) -> int:
    """Score how much a target page may benefit from new internal links from 0 to 100."""
    seo_score = min(max(_number(_get(page, "seo_score")), 0), 100)
    seo_component = (100 - seo_score) / 100 * 35

    link_count = _number(_get(page, "internal_links", _get(page, "internal_links_count")))
    links_component = (1 - min(max(link_count, 0), 25) / 25) * 25

    missing_basics = page_missing_fields(page).intersection(SEO_BASICS)
    missing_component = len(missing_basics) / len(SEO_BASICS) * 25

    article_status = _text(_get(task, "article_status", _get(page, "article_status", "not_generated")))
    article_component = 0 if article_status == "generated" else 15

    return round(seo_component + links_component + missing_component + article_component)


def best_anchor_text(page: Any, task: Any | None = None) -> str:
    """Choose the best available anchor text before OpenAI enrichment."""
    for value in (
        _get(task, "keyword") if task is not None else None,
        _get(task, "suggested_h1") if task is not None else None,
        _get(page, "h1"),
        _get(task, "suggested_title") if task is not None else None,
        _get(page, "title"),
    ):
        text = _text(value).strip()
        if text:
            return text
    return _text(_get(page, "url")).rstrip("/").rsplit("/", maxsplit=1)[-1].replace("-", " ") or "this page"
