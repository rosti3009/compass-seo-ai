from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

FALLBACK_TOPIC = "General"
LOW_VALUE_SEGMENTS = {
    "",
    "blog",
    "category",
    "tag",
    "page",
    "pages",
    "articles",
    "article",
    "guides",
    "guide",
    "services",
    "service",
    "products",
    "product",
    "collections",
    "collection",
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "best",
    "by",
    "for",
    "from",
    "guide",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "our",
    "page",
    "the",
    "to",
    "with",
    "your",
}


def _get(page: dict[str, Any], key: str, default: Any = None) -> Any:
    return page.get(key, default) if isinstance(page, dict) else default


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _title_case_topic(value: str) -> str:
    words = [word for word in re.split(r"[\s_-]+", value.strip()) if word]
    if not words:
        return FALLBACK_TOPIC
    return " ".join(word.capitalize() for word in words)


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token not in STOPWORDS and len(token) > 2]


def infer_topic(page: dict) -> str:
    """Infer a stable topical label from task keyword, URL path, and on-page text."""
    keyword = _text(_get(page, "keyword"))
    if keyword:
        tokens = _tokens(keyword)
        if tokens:
            return _title_case_topic(" ".join(tokens[:2]))

    url = _text(_get(page, "url"))
    path_segments = [segment for segment in urlparse(url).path.split("/") if segment]
    for segment in reversed(path_segments):
        normalized = re.sub(r"[^a-z0-9-]+", "-", segment.lower()).strip("-")
        if normalized and normalized not in LOW_VALUE_SEGMENTS:
            segment_tokens = _tokens(normalized.replace("-", " "))
            if segment_tokens:
                return _title_case_topic(" ".join(segment_tokens[:2]))

    combined_text = " ".join(_text(_get(page, field)) for field in ("title", "h1", "meta"))
    text_tokens = _tokens(combined_text)
    if text_tokens:
        return _title_case_topic(" ".join(text_tokens[:2]))
    return FALLBACK_TOPIC


def group_pages_by_topic(pages: list[dict]) -> dict[str, list[dict]]:
    """Group crawled page payloads by inferred topic with deterministic ordering."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for page in pages:
        grouped[infer_topic(page)].append(page)
    return {
        topic: sorted(topic_pages, key=lambda item: (-_number(_get(item, "seo_score")), _text(_get(item, "url"))))
        for topic, topic_pages in sorted(grouped.items(), key=lambda item: item[0].lower())
    }


def _pillar_rank(page: dict[str, Any]) -> tuple[float, float, int, int, str]:
    page_type = _text(_get(page, "page_type")).lower()
    pillar_bonus = 20 if page_type in {"homepage", "guide", "category", "service"} else 0
    generated_bonus = 8 if _text(_get(page, "article_status")) == "generated" else 0
    score = _number(_get(page, "seo_score")) + pillar_bonus + generated_bonus
    word_count = _number(_get(page, "word_count"))
    url_depth = len([segment for segment in urlparse(_text(_get(page, "url"))).path.split("/") if segment])
    return (score, word_count, -url_depth, -len(_text(_get(page, "url"))), _text(_get(page, "url")))


def select_pillar_page(pages: list[dict]) -> dict:
    """Select the strongest page in a topical group to act as the pillar page."""
    if not pages:
        return {}
    return max(pages, key=_pillar_rank)


def _page_label(page: dict[str, Any]) -> str:
    return _text(_get(page, "url"))


def _article_gap(page: dict[str, Any], topic: str) -> str:
    title = _text(_get(page, "title")) or _text(_get(page, "h1")) or _page_label(page) or topic
    return f"Create or expand supporting article for {title}"


def build_cluster_summary(pages: list[dict]) -> list[dict]:
    """Build deterministic topical cluster summaries ready for API responses or OpenAI enrichment."""
    clusters = []
    for topic, topic_pages in group_pages_by_topic(pages).items():
        pillar = select_pillar_page(topic_pages)
        pillar_url = _page_label(pillar)
        supporting_pages = [
            _page_label(page) for page in topic_pages if _page_label(page) and _page_label(page) != pillar_url
        ]
        missing_articles = [
            _article_gap(page, topic)
            for page in topic_pages
            if _text(_get(page, "article_status", "not_generated")) != "generated"
            and (_number(_get(page, "word_count")) < 800 or _number(_get(page, "seo_score")) < 70)
        ]
        internal_link_strategy = [
            f"Link from {supporting_url} to pillar page {pillar_url}." for supporting_url in supporting_pages[:5]
        ]
        if supporting_pages:
            internal_link_strategy.append(f"Add contextual links from {pillar_url} to the strongest supporting pages.")
        clusters.append(
            {
                "cluster_name": topic,
                "pillar_page": pillar_url,
                "supporting_pages": supporting_pages,
                "missing_articles": missing_articles[:5],
                "internal_link_strategy": internal_link_strategy,
            }
        )
    return clusters
