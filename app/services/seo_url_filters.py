from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

SYSTEM_PAGE_EXCLUSION_REASON = "system_page"
logger = logging.getLogger(__name__)

_SYSTEM_PATH_MARKERS = (
    "/account",
    "/login",
    "/cart",
    "/checkout",
    "/orders",
    "/newsletter",
    "/wishlist",
    "/search",
    "/privacy",
    "/terms",
    "/accessibility",
    "/contact",
)
_SYSTEM_TEXT_MARKERS = ("accessibility-statement",)


def _normalized_url_text(url: str) -> str:
    """Return a lowercase, decoded URL/path string for deterministic SEO eligibility checks."""
    raw = unquote(str(url or "")).strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path or raw.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def is_system_url(url: str) -> bool:
    """Return True for login/account/cart/legal/contact pages that should not become SEO work."""
    normalized = _normalized_url_text(url)
    if not normalized:
        return False
    if is_blog_url(url):
        logger.info("[BLOG VALIDATION] url=%s allowed=true reason=blog_url", url)
        return False
    return any(marker in normalized for marker in _SYSTEM_PATH_MARKERS) or any(
        marker in normalized for marker in _SYSTEM_TEXT_MARKERS
    )

def is_blog_url(url: str) -> bool:
    """Return True when the URL is the blog index or any blog article under /blog/{slug}."""
    normalized = _normalized_url_text(url)
    return normalized == "/blog" or normalized.startswith("/blog/")


def is_seo_eligible_url(url: str) -> bool:
    """Return whether a URL can be selected for SEO tasks, AI content, links, or publishing."""
    excluded = is_system_url(url)
    allowed = not excluded
    logger.info(
        "[BLOG VALIDATION] url=%s allowed=%s exclusion_reason=%s",
        url,
        allowed,
        SYSTEM_PAGE_EXCLUSION_REASON if excluded else None,
    )
    return allowed


def get_url_exclusion_reason(url: str) -> str | None:
    """Return the public exclusion reason for non-SEO system URLs, if excluded."""
    excluded = is_system_url(url)
    reason = SYSTEM_PAGE_EXCLUSION_REASON if excluded else None
    logger.info("[BLOG VALIDATION] url=%s allowed=%s exclusion_reason=%s", url, not excluded, reason)
    return reason
