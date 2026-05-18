from __future__ import annotations

import re

FORBIDDEN_HEBREW_PHRASES = (
    "קנייה חכמה",
    "פרטים חשובים שיעזרו לבחור נכון",
    "יתרונות מרכזיים",
    "התאמה לצרכים",
    "לפני קנייה",
    "סקירת מותג ודגמים מובילים",
    "מדריך קצר וברור",
    "לקבלת החלטה טובה יותר",
    "מוצר מוביל בתחום",
    "פתרון איכותי",
    "ביצועים מעולים",
    "מוצר המיועד לאנשים",
    "מקסימום נוחות",
    "מתאים לשימוש מקצועי וביתי",
    "מוצרים איכותיים",
    "בחירה נכונה",
    "הבחירה המושלמת",
    "חוויה מושלמת",
)

_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)+")
_SPACE_RE = re.compile(r"\s+")
_STRAY_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?])")
_REPEATED_PUNCTUATION_RE = re.compile(r"([,.;:!?])(?:\s*\1)+")


def clean_seo_text(value: str) -> str:
    """Normalize generated SEO copy without changing its customer-facing meaning."""
    cleaned = _ELLIPSIS_RE.sub(" ", value or "")
    cleaned = _SPACE_RE.sub(" ", cleaned)
    cleaned = _STRAY_PUNCTUATION_RE.sub(r"\1", cleaned)
    cleaned = _REPEATED_PUNCTUATION_RE.sub(r"\1", cleaned)
    return cleaned.strip(" -|,.;:\n\t")


def truncate_without_ellipsis(value: str, limit: int) -> str:
    """Trim text at a word boundary and never append an ellipsis."""
    cleaned = clean_seo_text(value)
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned

    candidate = re.sub(r"[\s\-|/,. ;:]+$", "", cleaned[:limit])
    boundary = max(candidate.rfind(" "), candidate.rfind("־"), candidate.rfind("-"))
    if boundary >= max(12, int(limit * 0.55)):
        candidate = candidate[:boundary]
    return clean_seo_text(candidate)


def remove_forbidden_hebrew_phrases(value: str) -> str:
    """Remove banned generic Hebrew SEO phrases from final publishable suggestions."""
    cleaned = value or ""
    for phrase in FORBIDDEN_HEBREW_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    return clean_seo_text(cleaned)


def sanitize_generated_seo_copy(value: str, *, limit: int | None = None) -> str:
    """Central guardrail for customer-facing generated Hebrew SEO copy."""
    cleaned = remove_forbidden_hebrew_phrases(value)
    if limit is not None:
        cleaned = truncate_without_ellipsis(cleaned, limit)
        cleaned = remove_forbidden_hebrew_phrases(cleaned)
    return cleaned


def contains_forbidden_hebrew_phrase(value: str) -> bool:
    return any(phrase in (value or "") for phrase in FORBIDDEN_HEBREW_PHRASES)
