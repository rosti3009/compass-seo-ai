from __future__ import annotations

from urllib.parse import unquote, urlparse

SYSTEM_PAGE_EXCLUSION_REASON = "system_page"

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
    return any(marker in normalized for marker in _SYSTEM_PATH_MARKERS) or any(
        marker in normalized for marker in _SYSTEM_TEXT_MARKERS
    )


def is_seo_eligible_url(url: str) -> bool:
    """Return whether a URL can be selected for SEO tasks, AI content, links, or publishing."""
    return not is_system_url(url)


def get_url_exclusion_reason(url: str) -> str | None:
    """Return the public exclusion reason for non-SEO system URLs, if excluded."""
    return SYSTEM_PAGE_EXCLUSION_REASON if is_system_url(url) else None
