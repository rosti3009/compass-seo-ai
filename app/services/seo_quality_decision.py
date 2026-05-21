from __future__ import annotations

import re
from dataclasses import dataclass

GENERIC_PHRASES = (
    "המוצר המוביל בתחום",
    "פתרון איכותי",
    "ביצועים מעולים",
    "נראה שהטקסט הקיים כבר איכותי",
)
SYSTEM_PATHS = ("/account", "/about", "/accessibility")


@dataclass(frozen=True)
class SEOQualityDecision:
    decision: str
    reason: str
    confidence: float
    weakness_flags: list[str]
    safe_for_quick_approval: bool
    publishable: bool
    recommendation_text: str = ""


def evaluate_seo_text(
    *, target_url: str, field_path: str, old_text: str, new_text: str, page_type: str
) -> SEOQualityDecision:
    old = (old_text or "").strip()
    new = (new_text or "").strip()
    flags: list[str] = []
    if any(path in (target_url or "") for path in SYSTEM_PATHS) or page_type == "system":
        return SEOQualityDecision("HIDE_FROM_EMPLOYEE", "system_page", 0.98, ["system_page"], False, False)
    if field_path == "keyword":
        return SEOQualityDecision("HIDE_FROM_EMPLOYEE", "slug_dangerous", 0.95, ["slug_dangerous"], False, False)
    if len(old) > 65 and field_path == "meta_title" and page_type in {"product", "category", "article", "blog"}:
        flags.append("too_long")
    if any(p in old for p in GENERIC_PHRASES):
        flags.append("generic_phrase")
    if old.count("|") > 1:
        flags.append("duplicated_brand")
    if re.search(r"\bלולה\b", old):
        flags.append("awkward_translation")
    if field_path == "meta_description" and len(old) < 70:
        flags.append("low_organic_value")

    if flags:
        return SEOQualityDecision("REWRITE", "דורש שכתוב", 0.9, flags, True, True)

    if new in {"נראה שהטקסט הקיים כבר איכותי", "אין צורך בשינוי", "אין צורך בשינוי כרגע"}:
        return SEOQualityDecision("KEEP_EXISTING", "אין צורך בשינוי", 0.92, [], False, False, "אין צורך בשינוי כרגע")
    return SEOQualityDecision("KEEP_EXISTING", "אין צורך בשינוי", 0.85, [], False, False, "אין צורך בשינוי כרגע")
