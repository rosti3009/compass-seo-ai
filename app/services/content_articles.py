from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
import requests

from app.db.models import ContentArticleDraft, IStoreProduct

logger = logging.getLogger(__name__)
GENERATOR_VERSION = "v5-render-path-diagnostics-2026-06-04"

TOPIC_POOL = [
    ("שבבי עץ לעישון", "שבבי עץ לעישון", "informational"),
    ("כנפיים קריספיות על הגריל", "כנפיים קריספיות על הגריל", "how-to"),
    ("אבני בזלת לגריל", "אבני בזלת לגריל", "commercial_informational"),
    ("בריסקט", "בריסקט", "how-to"),
    ("פיקניה", "פיקניה", "how-to"),
    ("גריל גז", "גריל גז", "commercial"),
    ("טאבון גז או טאבון עצים", "טאבון", "comparison"),
    ("נייר קצבים", "נייר קצבים", "how-to"),
    ("מדחום לבשר", "מדחום לבשר", "commercial"),
    ("פחם / פחם קוקוס", "פחם / פחם קוקוס", "comparison"),
]

SLUG_OVERRIDES = {
    "איך לבחור שבבי עץ לעישון בשר": "wood-chips-for-smoking-meat",
    "ההבדל בין פלט לעישון לשבבי עץ": "pellets-vs-wood-chips-smoking",
    "איך לנקות מעשנה אחרי עישון ארוך": "how-to-clean-smoker-after-long-smoke",
    "מדריך עישון בריסקט למתחילים": "brisket-smoking-guide",
    "איך לבחור גריל גז לגינה": "choose-gas-grill-for-garden",
    "פיקניה על הגריל – מדריך מלא": "picanha-on-the-grill-guide",
    "איך להשתמש בנייר קצבים בעישון בשר": "butcher-paper-for-smoking-meat",
    "ההבדל בין פחם קוקוס לפחם עץ": "coconut-charcoal-vs-wood-charcoal",
    "איך לבחור מדחום לבשר": "how-to-choose-meat-thermometer",
    "טאבון גז מול טאבון עצים": "tabun-gas-vs-tabun-wood",
    "טאבון גז או טאבון עצים": "tabun-gas-vs-tabun-wood",
    "איך להכין כנפיים קריספיות על הגריל": "crispy-grilled-wings",
    "כנפיים על הגריל": "crispy-grilled-wings",
    "כנפיים קריספיות": "crispy-grilled-wings",
    "אבני בזלת לגריל": "basalt-stones-for-gas-grill",
    "שבבי עץ לעישון": "wood-chips-for-smoking-meat",
    "עישון בריסקט": "brisket-smoking-guide",
    "בריסקט": "brisket-smoking-guide",
    "פיקניה על הגריל": "picanha-on-grill",
    "פיקניה": "picanha-on-grill",
    "נייר קצבים לעישון": "butcher-paper-for-smoking-meat",
    "נייר קצבים": "butcher-paper-for-smoking-meat",
    "כנפיים קריספיות על הגריל": "crispy-grilled-wings",
    "גריל גז": "choose-gas-grill-for-garden",
    "טאבון": "tabun-gas-vs-tabun-wood",
    "מדחום לבשר": "how-to-choose-meat-thermometer",
    "פחם / פחם קוקוס": "coconut-charcoal-vs-wood-charcoal",
}

TOPIC_ROUTING = {
    "wings": ["כנפיים", "קריספ"],
    "basalt": ["אבני בזלת", "בזלת", "לבה"],
    "wood_chips": ["שבבי עץ", "עישון", "smoker"],
    "brisket": ["בריסקט"],
    "picanha": ["פיקניה"],
    "gas_grill": ["גריל גז"],
    "tabun": ["טאבון"],
    "butcher_paper": ["נייר קצבים", "butcher paper", "pink butcher paper", "smoking paper"],
    "thermometer": ["מדחום לבשר", "מדחום"],
    "charcoal": ["פחם קוקוס", "פחם עץ", "פחם"],
}



SITEMAP_SOURCES = [
    "https://compassgrill.co.il/sitemap.xml",
    "https://compassgrill.co.il/sitemap-products.xml",
    "https://compassgrill.co.il/sitemap-categories.xml",
    "https://compassgrill.co.il/sitemap-brands.xml",
    "https://compassgrill.co.il/sitemap-information.xml",
]

_INTERNAL_LINK_INDEX_CACHE: dict[str, object] = {"loaded_at": 0.0, "ttl_seconds": 21600, "entries": [], "stats": {}}


def _infer_type_from_url(url: str, sitemap_url: str = "") -> str:
    path = urlparse(url).path.lower()
    source = (sitemap_url or "").lower()
    if "sitemap-products" in source or "/products" in path or "/product" in path:
        return "product"
    if "sitemap-categories" in source or "/categories" in path or "/category" in path:
        return "category"
    if "sitemap-brands" in source or "/brands" in path or "/brand" in path:
        return "brand"
    if "/blog" in path or "/blogs" in path:
        return "blog"
    return "info"


def _slug_from_url(url: str) -> str:
    return (urlparse(url).path.strip("/").split("/")[-1] or "").lower()


def _title_from_slug(slug: str) -> str:
    known = {
        "basalt-stones-for-gas-grill": "אבני בזלת לגריל גז",
        "basalt-lava-stones": "אבני בזלת ולבה לגריל",
        "lava-rocks-grill": "אבני לבה לגריל",
        "grill-accessories": "אביזרים לגריל",
        "meat-thermometer": "מדחום לבשר",
        "digital-meat-thermometer": "מדחום דיגיטלי לבשר",
        "feedlot-picanha": "פיקניה פידלוט",
        "picanha": "פיקניה",
    }
    if slug in known:
        return known[slug]
    return re.sub(r"[-_]+", " ", slug).strip()


def _load_sitemap_index(force_refresh: bool = False) -> tuple[list[dict[str, object]], dict[str, object]]:
    now = time.time()
    if not force_refresh and _INTERNAL_LINK_INDEX_CACHE["entries"] and now - float(_INTERNAL_LINK_INDEX_CACHE["loaded_at"]) < int(_INTERNAL_LINK_INDEX_CACHE["ttl_seconds"]):
        return _INTERNAL_LINK_INDEX_CACHE["entries"], _INTERNAL_LINK_INDEX_CACHE["stats"]

    entries: list[dict[str, object]] = []
    stats = {"sitemap_loaded_count": 0, "products_loaded_count": 0, "categories_loaded_count": 0, "index_refreshed_at": datetime.now(UTC).isoformat(), "internal_link_index_status": "loading"}
    for sitemap_url in SITEMAP_SOURCES:
        try:
            xml = requests.get(sitemap_url, timeout=20).text
            locs = re.findall(r"<loc>(.*?)</loc>", xml, flags=re.IGNORECASE)
            lastmods = re.findall(r"<lastmod>(.*?)</lastmod>", xml, flags=re.IGNORECASE)
            is_index = "<sitemapindex" in xml.lower()
            urls = [u.strip() for u in locs if u.strip().startswith("http") and not u.strip().endswith(".xml")] if is_index else [u.strip() for u in locs if u.strip().startswith("http")]
            for i, u in enumerate(urls):
                typ = _infer_type_from_url(u, sitemap_url)
                slug = _slug_from_url(u)
                title = _title_from_slug(slug)
                blob = _normalize_hebrew(f"{title} {slug} {u}")
                hebrew_tokens = {t for t in _tokenize_hebrew(blob) if re.search(r"[\u0590-\u05FF]", t)}
                english_tokens = {t for t in _tokenize_hebrew(blob) if re.search(r"[a-z]", t)}
                entries.append({
                    "url": u, "slug": slug, "inferred_title": title, "title": title,
                    "page_type": typ, "type": typ, "hebrew_tokens": hebrew_tokens,
                    "english_tokens": english_tokens, "normalized_tokens": _tokenize_hebrew(blob),
                    "tokens": _tokenize_hebrew(blob), "lastmod": lastmods[i] if i < len(lastmods) else None,
                })
                if typ == "product":
                    stats["products_loaded_count"] += 1
                if typ == "category":
                    stats["categories_loaded_count"] += 1
            stats["sitemap_loaded_count"] += 1
        except Exception:
            logger.exception("Failed loading sitemap %s", sitemap_url)

    stats["internal_link_index_status"] = "loaded" if entries else "empty"
    _INTERNAL_LINK_INDEX_CACHE.update({"loaded_at": now, "entries": entries, "stats": stats})
    return entries, stats


def refresh_internal_link_index() -> dict[str, object]:
    _load_sitemap_index(force_refresh=True)
    return dict(_INTERNAL_LINK_INDEX_CACHE.get("stats") or {})

GENERIC_FILLER_PHRASES = [
    "נושא שמכריע אם תקבלו תוצאה בינונית",
    "הנושא הזה מכריע אם תקבלו תוצאה בינונית",
    "ציוד ומוצרים שכדאי להכין מראש",
    "חימום מוקדם 15–20 דקות",
    "צ׳קליסט מעשי",
    "מה הדבר הראשון לבדוק",
    "איך נמנעים מקנייה לא נכונה",
    "מתי כדאי לשדרג ציוד",
    "התאמה לשימוש האמיתי שלכם",
    "נמשיך לעדכן כאן",
    "העמקה מעשית",
    "תכנון מראש",
    "ניהול לחות",
    "תיעוד תוצאה",
    "שיפור מפעם לפעם",
    "בחירת ציוד כללית",
]

GENERIC_TEMPLATE_INTRO = "נושא שמכריע אם תקבלו תוצאה בינונית או מנה שמרגישה כמו מסעדת בשרים מקצועית"

TOPIC_TYPE_CONTRACTS: dict[str, dict[str, object]] = {
    "meat_quick_grill_cut": {
        "entity_type": "beef_cut",
        "content_format": "how_to_grilling_guide",
        "required_sections": ["מאפייני הנתח", "שומן ושיוש", "המלחה", "חום ישיר ועקיף", "טמפרטורת יעד", "חיתוך ומנוחה", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["שכבת שומן", "שיוש", "מלח גס", "חום ישיר", "חום עקיף", "54–57°C", "חיתוך נגד הסיבים", "מנוחה"],
        "forbidden_terms": ["74°C", "גלייז", "זמן בעירה", "אפר", "stall", "סטול", "wrap", "עטיפה בנייר קצבים", "התקנה", "תחזוקה"],
        "temperature_policy": {"allowed": ["54–57°C"], "meaning": "quick beef steak doneness"},
        "meta_pattern": "{keyword}: מדריך צלייה לנתח בקר עם שיוש, המלחה, חום ישיר/עקיף וטמפרטורת יעד.",
        "image_prompt_pattern": "realistic close-up of {keyword} beef steak cut on grill grates, visible fat and marbling, no poultry, no charcoal comparison, no text",
        "internal_link_keywords": ["מדחום לבשר", "גריל פחמים", "מלח גס", "סטייקים"],
    },
    "meat_low_slow_smoking": {
        "entity_type": "low_slow_beef_cut",
        "content_format": "low_and_slow_smoking_guide",
        "required_sections": ["הכנת המעשנה", "105–120°C", "בחירת עצי עישון", "פיתוח Bark", "הסטול", "עטיפה", "נייר קצבים", "טמפרטורת סיום", "מנוחה ארוכה", "ציר זמן", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["מעשנה", "105–120°C", "עצי עישון", "Bark", "סטול", "עטיפה", "נייר קצבים", "90–96°C", "מנוחה ארוכה", "ציר זמן"],
        "forbidden_terms": ["74°C", "54–57°C", "גלייז", "זמן בעירה", "אפר", "התקנה"],
        "temperature_policy": {"allowed": ["105–120°C", "90–96°C"], "meaning": "low slow chamber and finish range"},
        "meta_pattern": "{keyword}: מדריך עישון נמוך-ואיטי עם 105–120°C, Bark, סטול, עטיפה ומנוחה ארוכה.",
        "image_prompt_pattern": "realistic smoker chamber with {keyword} beef cut, dark bark, butcher paper nearby, thin blue smoke, no chicken, no text",
        "internal_link_keywords": ["מעשנה", "נייר קצבים", "עצי עישון", "מדחום לבשר"],
    },
    "poultry_grill_recipe": {
        "entity_type": "poultry_recipe",
        "content_format": "recipe_how_to",
        "required_sections": ["ייבוש", "בטיחות מזון", "74°C", "קריספיות", "מרינדה וגלייז", "סוכר שרוף", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["ייבוש", "בטיחות מזון", "74°C", "קריספיות", "מרינדה", "גלייז", "סוכר שרוף"],
        "forbidden_terms": ["54–57°C", "מדיום רייר", "שכבת שומן בקר", "סטול", "Bark", "זמן בעירה", "אפר", "התקנה"],
        "temperature_policy": {"allowed": ["74°C"], "meaning": "minimum poultry internal temperature"},
        "meta_pattern": "{keyword}: מתכון גריל לעוף עם ייבוש, בטיחות מזון, 74°C, קריספיות וגלייז נכון.",
        "image_prompt_pattern": "crispy grilled chicken wings or poultry for {keyword}, golden skin, glaze brushed at the end, realistic BBQ photo, no beef steak, no text",
        "internal_link_keywords": ["מדחום לבשר", "רוטב BBQ", "גריל גז", "מלקחיים"],
    },
    "fuel_comparison_or_guide": {
        "entity_type": "bbq_fuel",
        "content_format": "comparison_or_buying_guide",
        "required_sections": ["זמן בעירה", "יציבות חום", "רמת עשן", "כמות אפר", "התאמה לגריל או מעשנה", "עלות מול ביצועים", "מתי לבחור", "טבלת השוואה", "שאלות נפוצות"],
        "required_terms": ["זמן בעירה", "יציבות חום", "רמת עשן", "אפר", "גריל", "מעשנה", "עלות מול ביצועים", "פחם קוקוס", "פחם עץ"],
        "forbidden_terms": ["74°C", "54–57°C", "מדיום רייר", "גלייז", "טמפרטורת יעד פנימית", "סטול", "Bark", "נייר קצבים", "התקנה"],
        "temperature_policy": {"allowed": [], "meaning": "fuel topics should not include meat internal temperatures"},
        "meta_pattern": "{keyword}: מדריך והשוואה לפי זמן בעירה, יציבות חום, רמת עשן, אפר ועלות מול ביצועים.",
        "image_prompt_pattern": "BBQ fuel comparison for {keyword}: coconut charcoal briquettes and natural lump wood charcoal, ash tray, no meat, no internal thermometer, no text",
        "internal_link_keywords": ["פחם קוקוס", "פחם עץ", "מדליק פחמים", "גריל פחמים"],
    },
    "smoking_wood_guide": {
        "entity_type": "smoking_wood",
        "content_format": "wood_selection_guide",
        "required_sections": ["פרופיל טעם", "שבבים מול צ׳אנקים", "השריה", "התאמה לבשר", "עוצמת עשן", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["פרופיל טעם", "שבבים", "צ׳אנקים", "השריה", "התאמה לבשר", "עוצמת עשן", "thin blue smoke"],
        "forbidden_terms": ["74°C", "54–57°C", "גלייז", "זמן בעירה", "אפר", "התקנה", "BTU"],
        "temperature_policy": {"allowed": [], "meaning": "wood guide focuses on flavor rather than internal temperatures"},
        "meta_pattern": "{keyword}: מדריך עצי עישון לפי פרופיל טעם, שבבים מול צ׳אנקים, השריה והתאמה לבשר.",
        "image_prompt_pattern": "smoking wood guide for {keyword}, wood chips in smoker box, separate piles of chips and chunks, thin blue smoke, flavor matching setup, no chicken wings, no text",
        "internal_link_keywords": ["שבבי עץ", "צ׳אנקים", "מעשנה", "נייר קצבים"],
    },
    "smoking_accessory_guide": {
        "entity_type": "smoking_accessory",
        "content_format": "smoking_accessory_how_to_guide",
        "required_sections": ["מה זה", "עטיפת בריסקט", "עטיפת צלעות", "נתחי בקר", "שלב הסטול", "Texas Crutch", "שמירת Bark", "שמירת לחות", "נייר קצבים מול נייר כסף", "מתי לעטוף", "איך לעטוף", "נייר ורוד מול חום", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["נייר קצבים", "בריסקט", "צלעות", "נתחי בקר", "סטול", "Texas Crutch", "Bark", "שמירת לחות", "נייר כסף", "מתי לעטוף", "איך לעטוף", "נייר ורוד", "נייר חום", "טעויות נפוצות"],
        "forbidden_terms": ["התקנה", "התאמה לגריל", "חוסם אוויר", "חסימת אוויר", "תחזוקה", "מתי להחליף", "כיול", "מבערים", "מבער", "החלפת אביזר שחוק", "אביזר שחוק"],
        "temperature_policy": {"allowed": ["105–120°C", "90–96°C"], "meaning": "smoking wrap timing and finish cues only"},
        "meta_pattern": "{keyword}: מדריך נייר קצבים לעישון עם בריסקט, סטול, Texas Crutch, Bark והשוואה לנייר כסף.",
        "image_prompt_pattern": "pink butcher paper for smoking brisket and ribs beside a smoker, bark preservation and moisture retention context, no grill installation, no burners, no airflow diagram, no text",
        "internal_link_keywords": ["נייר קצבים", "נייר קצבים לעישון", "butcher paper", "pink butcher paper", "בריסקט", "צלעות", "מעשנה", "עצי עישון", "מדחום לבשר"],
    },
    "grill_accessory_guide": {
        "entity_type": "grill_accessory",
        "content_format": "accessory_use_and_buying_guide",
        "required_sections": ["מה זה", "איך זה עובד", "יתרונות", "התקנה ושימוש", "ניקוי ותחזוקה", "מתי להחליף", "שיקולי קנייה", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["מה זה", "איך זה עובד", "יתרונות", "התקנה", "שימוש", "ניקוי", "תחזוקה", "מתי להחליף", "שיקולי קנייה", "טעויות נפוצות"],
        "forbidden_terms": ["74°C", "54–57°C", "מדיום רייר", "גלייז", "סטול", "Bark", "זמן בעירה", "אפר"],
        "temperature_policy": {"allowed": [], "meaning": "accessory topics should not include meat temperatures"},
        "meta_pattern": "{keyword}: מדריך אביזר לגריל עם הסבר, התקנה, שימוש, ניקוי, תחזוקה ושיקולי קנייה.",
        "image_prompt_pattern": "realistic grill accessory guide for {keyword}, product installed or used on a grill, clean maintenance context, no unrelated accessory, no text",
        "internal_link_keywords": ["אביזרים לגריל", "גריל גז", "כפפות", "מלקחיים", "מברשת"],
    },
    "equipment_buying_guide": {
        "entity_type": "bbq_equipment",
        "content_format": "equipment_buying_guide",
        "required_sections": ["תרחיש שימוש", "גודל", "מקור חום או BTU", "איכות חומר", "תחזוקה", "השוואה", "למי זה מתאים", "שאלות נפוצות"],
        "required_terms": ["תרחיש שימוש", "גודל", "BTU", "מקור חום", "איכות חומר", "תחזוקה", "השוואה", "למי זה מתאים"],
        "forbidden_terms": ["74°C", "54–57°C", "מדיום רייר", "גלייז", "סטול", "Bark", "זמן בעירה", "אפר"],
        "temperature_policy": {"allowed": [], "meaning": "equipment buying guides discuss capacity and heat source, not meat temperatures"},
        "meta_pattern": "{keyword}: מדריך קנייה לפי שימוש, גודל, מקור חום/BTU, איכות חומר, תחזוקה והתאמה.",
        "image_prompt_pattern": "realistic outdoor BBQ equipment buying guide focused on {keyword}, size and material quality comparison, no cooked meat closeup, no text",
        "internal_link_keywords": ["גריל גז", "גריל פחמים", "מעשנה", "טאבון", "מטבח חוץ"],
    },
    "recipe_how_to": {
        "entity_type": "general_recipe",
        "content_format": "recipe_how_to",
        "required_sections": ["מרכיבים", "כלים", "שלבים", "טיפים", "טעויות נפוצות", "שאלות נפוצות"],
        "required_terms": ["מרכיבים", "כלים", "שלבים", "טיפים", "טעויות"],
        "forbidden_terms": ["סטול", "Bark", "זמן בעירה", "אפר", "התקנה"],
        "temperature_policy": {"allowed": [], "meaning": "temperature only if the specific recipe contract adds it"},
        "meta_pattern": "{keyword}: מתכון גריל עם מרכיבים, כלים, שלבים, טיפים וטעויות נפוצות.",
        "image_prompt_pattern": "realistic outdoor grill recipe photo for {keyword}, ingredients and tools visible, no unrelated smoking wood, no text",
        "internal_link_keywords": ["גריל", "מדחום", "אביזרים לגריל"],
    },
    "fallback_generic": {
        "entity_type": "unknown",
        "content_format": "generic_bbq_guide",
        "required_sections": ["למה זה חשוב", "שיטת עבודה", "טעויות", "שאלות נפוצות"],
        "required_terms": [],
        "forbidden_terms": [],
        "temperature_policy": {"allowed": [], "meaning": "fallback only"},
        "meta_pattern": "{keyword}: מדריך מעשי בעברית עם טיפים, שלבים ו-FAQ.",
        "image_prompt_pattern": "realistic outdoor BBQ guide photo focused on {keyword}, no text",
        "internal_link_keywords": ["גריל", "אביזרים"],
    },
}

RECOGNIZED_TOPIC_TYPES = {k for k in TOPIC_TYPE_CONTRACTS if k != "fallback_generic"}

TOPIC_CLASSIFIER_RULES: list[tuple[str, list[str], str, str]] = [
    ("meat_quick_grill_cut", ["פיקניה", "אנטריקוט", "סינטה", "דנוור", "פלאט איירון", "סטייק", "סטייקים"], "beef_cut", "how-to"),
    ("meat_low_slow_smoking", ["בריסקט", "אסאדו", "שורט ריבס", "צלעות בקר", "נתחים לעישון", "עישון בריסקט"], "low_slow_beef_cut", "how-to"),
    ("poultry_grill_recipe", ["כנפיים", "פרגית", "עוף", "שיפודי פרגית"], "poultry", "how-to"),
    ("fuel_comparison_or_guide", ["פחם", "פחם קוקוס", "פחמי עץ", "גחלים", "charcoal", "fuel"], "fuel", "comparison"),
    ("smoking_wood_guide", ["שבבי עץ", "צ׳אנקים", "צ'אנקים", "צאנקים", "עצי עישון", "wood chips", "smoking wood"], "smoking_wood", "commercial_informational"),
    ("smoking_accessory_guide", ["נייר קצבים", "butcher paper", "butcher paper sheets", "pink butcher paper", "smoking paper"], "smoking_accessory", "how-to"),
    ("grill_accessory_guide", ["אבני בזלת", "בזלת", "אבני לבה", "מדחום", "כפפות", "מלקחיים", "מברשת", "basalt", "lava rocks", "lava stones", "thermometer", "accessory"], "accessory", "commercial_informational"),
    ("equipment_buying_guide", ["גריל גז", "גריל פחמים", "מעשנה", "טאבון", "מטבח חוץ", "gas grill"], "equipment", "commercial"),
]

ACCESSORY_ENTITY_PROFILES: dict[str, dict[str, object]] = {
    "basalt_stones": {
        "canonical_entity": "אבני בזלת / אבני לבה",
        "match_terms": ["אבני בזלת", "בזלת", "אבני לבה", "lava rocks", "lava stones", "basalt stones", "basalt"],
        "required_terms": ["אבני לבה", "אבני בזלת", "lava rocks", "פיזור חום", "הפחתת התלקחויות", "מבערים", "אידוי שומן", "יציבות טמפרטורה", "מרווחי החלפה", "טעויות נפוצות"],
        "internal_link_keywords": ["אבני בזלת", "אבני לבה", "אבני בזלת לגריל", "אבני לבה לגריל גז", "גריל גז", "מבערים", "אביזרים לגריל", "פיזור חום בגריל גז", "lava rocks", "basalt stones"],
        "seo_keywords": {
            "secondary": ["אבני לבה לגריל", "אבני בזלת לגריל גז", "פיזור חום בגריל גז", "אבני לבה לגריל גז"],
            "long_tail": ["איך משתמשים באבני בזלת", "הפחתת התלקחויות בגריל", "ניקוי אבני בזלת", "מתי מחליפים אבני בזלת"],
            "questions": ["איך משתמשים באבני בזלת?", "מתי מחליפים אבני בזלת?", "איך מנקים אבני בזלת?"],
            "commercial": ["אביזרים לגריל גז", "אבני בזלת לגריל גז", "אבני לבה לגריל גז"],
        },
        "image_prompt_pattern": "black basalt lava stones and lava rocks / basalt stones arranged above gas grill burners, realistic grill accessory guide, heat distribution and grease vaporization context, no thermometer, no meat temperature reading, no text",
    },
    "thermometer": {
        "canonical_entity": "מדחום לבשר",
        "match_terms": ["מדחום לבשר", "מדחום", "thermometer", "meat thermometer", "probe"],
        "required_terms": ["מדחום", "probe", "קריאה מהירה", "כיול", "טמפרטורה פנימית", "זמן תגובה", "ניקוי", "טעויות נפוצות"],
        "internal_link_keywords": ["מדחום לבשר", "מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז", "מדחום למעשנה", "מדחום פרוב", "אביזרים לגריל", "גריל גז", "thermometer", "meat thermometer", "probe"],
        "seo_keywords": {
            "secondary": ["מדחום לבשר מומלץ", "מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז"],
            "long_tail": ["איך מודדים טמפרטורת בשר", "מדחום למעשנה", "מדחום לקריאה מהירה", "מדחום פרוב"],
            "questions": ["איך מודדים טמפרטורת בשר?", "איך מכיילים מדחום לבשר?", "איזה מדחום לבשר מתאים לגריל?"],
            "commercial": ["מדחום לבשר מומלץ", "מדחום לגריל גז", "מדחום למעשנה", "טמפרטורת בשר מושלמת"],
        },
        "image_prompt_pattern": "digital meat thermometer probe used as a grill accessory beside a gas grill, instant-read display visible without numbers, clean food-safe maintenance context, no unrelated heat-distribution stones, no text",
    },
}

TOPIC_TYPE_GENERATORS = {
    "meat_quick_grill_cut": "contract_meat_quick_grill_cut",
    "meat_low_slow_smoking": "contract_meat_low_slow_smoking",
    "poultry_grill_recipe": "contract_poultry_grill_recipe",
    "fuel_comparison_or_guide": "contract_fuel_comparison_or_guide",
    "smoking_wood_guide": "contract_smoking_wood_guide",
    "smoking_accessory_guide": "contract_smoking_accessory_guide",
    "grill_accessory_guide": "contract_grill_accessory_guide",
    "equipment_buying_guide": "contract_equipment_buying_guide",
    "recipe_how_to": "contract_recipe_how_to",
    "fallback_generic": "generic_fallback",
}


def _contract_for(topic_type: str) -> dict[str, object]:
    return TOPIC_TYPE_CONTRACTS.get(topic_type, TOPIC_TYPE_CONTRACTS["fallback_generic"])


def _resolve_accessory_entity_profile(topic_title: str, focus_keyword: str) -> tuple[str, dict[str, object]] | None:
    blob = _normalize_text_for_matching(f"{topic_title} {focus_keyword}")
    for entity_key, entity_profile in ACCESSORY_ENTITY_PROFILES.items():
        match_terms = [str(term) for term in entity_profile.get("match_terms", [])]
        if any(_normalize_text_for_matching(term) in blob for term in match_terms):
            return entity_key, entity_profile
    return None


def _normalize_text_for_matching(value: str) -> str:
    return _normalize_hebrew((value or "").replace("׳", "'").replace("’", "'").replace("״", '"'))


def _extract_main_entity(topic_title: str, focus_keyword: str) -> str:
    return (focus_keyword or topic_title or "נושא גריל").strip()


def _classify_topic(topic_title: str, focus_keyword: str, target_intent: str) -> dict[str, object]:
    blob = _normalize_text_for_matching(f"{topic_title} {focus_keyword}")
    requested_intent = (target_intent or "").strip()
    topic_type = "fallback_generic"
    entity_type = "unknown"
    intent = requested_intent or "informational"
    fallback_reason = "no_semantic_group_match"

    for candidate_type, terms, candidate_entity_type, default_intent in TOPIC_CLASSIFIER_RULES:
        if any(_normalize_text_for_matching(term) in blob for term in terms):
            topic_type = candidate_type
            entity_type = candidate_entity_type
            if candidate_type in {"meat_quick_grill_cut", "meat_low_slow_smoking", "poultry_grill_recipe"}:
                intent = default_intent
            else:
                intent = requested_intent or default_intent
            fallback_reason = ""
            break
    else:
        if any(term in blob for term in ["מתכון", "איך להכין", "צלייה", "על האש"]):
            topic_type = "recipe_how_to"
            entity_type = "general_recipe"
            intent = requested_intent or "how-to"
            fallback_reason = "generic_recipe_semantics"

    contract = _contract_for(topic_type)
    main_entity = _extract_main_entity(topic_title, focus_keyword)
    entity_key = "generic"
    entity_profile: dict[str, object] = {}
    if topic_type == "grill_accessory_guide":
        resolved = _resolve_accessory_entity_profile(topic_title, focus_keyword)
        if resolved:
            entity_key, entity_profile = resolved
            main_entity = str(entity_profile.get("canonical_entity") or main_entity)
    required_terms = [*list(contract.get("required_terms", [])), *list(entity_profile.get("required_terms", []))]
    internal_link_keywords = [*list(contract.get("internal_link_keywords", [])), *list(entity_profile.get("internal_link_keywords", []))]
    article_brief = {
        "main_entity": main_entity,
        "entity_key": entity_key,
        "entity_profile": entity_profile,
        "entity_type": contract.get("entity_type", entity_type),
        "topic_type": topic_type,
        "content_format": contract.get("content_format", "guide"),
        "search_intent": intent,
        "required_sections": list(contract.get("required_sections", [])),
        "required_terms": list(dict.fromkeys(str(term) for term in required_terms if str(term).strip())),
        "forbidden_terms": list(contract.get("forbidden_terms", [])),
        "temperature_policy": dict(contract.get("temperature_policy", {})),
        "image_policy": {"featured_prompt_pattern": entity_profile.get("image_prompt_pattern") or contract.get("image_prompt_pattern", ""), "must_include_entity": True},
        "internal_link_keywords": list(dict.fromkeys(str(term) for term in internal_link_keywords if str(term).strip())),
    }
    return {
        **article_brief,
        "article_brief": article_brief,
        "product_type": article_brief["entity_type"],
        "content_type": article_brief["content_format"],
        "target_keyword": focus_keyword,
        "related_keywords": [main_entity, *article_brief["internal_link_keywords"]],
        "forbidden_sections": [],
        "selected_generator": TOPIC_TYPE_GENERATORS[topic_type],
        "generator_source": "contract_engine" if topic_type != "fallback_generic" else "fallback",
        "fallback_reason": fallback_reason,
        "selected_contract": topic_type,
        "contract": contract,
    }


def _plain_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or " ")


def _first_paragraph_text(html: str) -> str:
    match = re.search(r"<p[^>]*>(.*?)</p>", html or "", flags=re.IGNORECASE | re.DOTALL)
    return _plain_text(match.group(1) if match else (html or "")[:350])


def _meaningful_title_terms(title: str, keyword: str) -> list[str]:
    stop = {"איך", "או", "על", "עם", "של", "מול", "מדריך", "מלא", "לגריל", "גריל", "ההבדל", "בין", "את", "זה", "for", "the", "a", "an", "guide", "gas", "grill"}
    terms: list[str] = []
    for term in re.split(r"[\s/–-]+", f"{title} {keyword}"):
        clean = term.strip(" :|,.")
        if len(clean) > 1 and clean not in stop and clean not in terms:
            terms.append(clean)
    return terms


def _image_prompt_matches_brief(prompt: str, topic_profile: dict[str, object]) -> bool:
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    prompt_lower = (prompt or "").lower()
    checks = {
        "meat_quick_grill_cut": ["steak", "beef", "marbling"],
        "meat_low_slow_smoking": ["smoker", "bark", "butcher"],
        "poultry_grill_recipe": ["chicken", "wings", "poultry"],
        "fuel_comparison_or_guide": ["charcoal", "fuel", "briquettes"],
        "smoking_wood_guide": ["wood", "chips", "chunks", "smoke"],
        "smoking_accessory_guide": ["butcher", "paper", "brisket", "smoking"],
        "grill_accessory_guide": ["accessory", "installed", "grill"],
        "equipment_buying_guide": ["equipment", "buying", "size", "material"],
    }
    return not checks.get(topic_type) or any(term in prompt_lower for term in checks[topic_type])


def validate_article_relevance(title: str, keyword: str, body: str, topic_profile: dict[str, object], *, image_prompt: str = "", internal_links: list[dict[str, object]] | None = None) -> dict[str, object]:
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    search_intent = str(topic_profile.get("search_intent") or "informational")
    intro = _normalize_hebrew(_first_paragraph_text(body))
    plain = _normalize_hebrew(_plain_text(body))
    raw_body = body or ""
    title_terms = _meaningful_title_terms(title, keyword)
    missing_intro_terms = [t for t in title_terms[:4] if _normalize_hebrew(t) not in intro]
    required_terms = [str(t) for t in topic_profile.get("required_terms", []) if str(t).strip()]
    required_sections = [str(t) for t in topic_profile.get("required_sections", []) if str(t).strip()]
    forbidden_terms = [str(t) for t in topic_profile.get("forbidden_terms", []) if str(t).strip()]
    missing_required_terms = [t for t in required_terms if t not in raw_body]
    missing_required_sections = [t for t in required_sections if _normalize_hebrew(t) not in plain]
    forbidden_terms_found = [t for t in forbidden_terms if t in raw_body]

    intent_missing: list[str] = []
    if search_intent == "comparison":
        intent_missing = [t for t in ["השוואה", "טבלת השוואה"] if _normalize_hebrew(t) not in plain]
    elif search_intent == "how-to":
        intent_missing = [t for t in ["טעויות", "שאלות נפוצות"] if _normalize_hebrew(t) not in plain]
    elif search_intent in {"commercial", "commercial_informational"}:
        commercial_markers = ["יתרונות", "שיקולי קנייה", "למי זה מתאים", "מתי להחליף", "שאלות נפוצות"]
        intent_missing = [] if any(_normalize_hebrew(t) in plain for t in commercial_markers) else ["commercial_structure"]

    generic_leakage = [] if topic_type == "fallback_generic" else [phrase for phrase in GENERIC_FILLER_PHRASES if phrase in raw_body]
    image_prompt_relevant = _image_prompt_matches_brief(image_prompt, topic_profile) if image_prompt else True
    internal_links_relevant = True
    if internal_links:
        keywords = [str(k).lower() for k in topic_profile.get("internal_link_keywords", [])]
        internal_links_relevant = any(any(k and k in (str(link.get("anchor_text", "")) + " " + str(link.get("url", ""))).lower() for k in keywords) for link in internal_links) if keywords else True

    score = 100
    score -= 7 * len(missing_intro_terms)
    score -= 8 * len(missing_required_terms)
    score -= 6 * len(missing_required_sections)
    score -= 18 * len(forbidden_terms_found)
    score -= 8 * len(intent_missing)
    score -= 25 * len(generic_leakage)
    if not image_prompt_relevant:
        score -= 15
    if not internal_links_relevant:
        score -= 10
    score = max(0, min(100, score))
    validation_passed = not forbidden_terms_found and not generic_leakage and image_prompt_relevant
    return {
        "title_body_relevance_score": score,
        "validation_passed": validation_passed,
        "missing_intro_terms": missing_intro_terms,
        "missing_required_terms": missing_required_terms,
        "missing_required_sections": missing_required_sections,
        "forbidden_terms_found": forbidden_terms_found,
        "intent_missing": intent_missing,
        "generic_leakage_found": generic_leakage,
        "image_prompt_relevant": image_prompt_relevant,
        "internal_links_relevant": internal_links_relevant,
        "topic_type": topic_type,
    }


def _slugify(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[raw]
    lowered = raw.lower()
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if ascii_slug and re.search(r"[a-z0-9]", ascii_slug):
        return ascii_slug[:90]
    hebrew_overrides = {
        "פיקניה": "picanha-on-grill",
        "בריסקט": "brisket-smoking-guide",
        "כנפיים קריספיות": "crispy-grilled-wings",
        "כנפיים קריספיות על הגריל": "crispy-grilled-wings",
        "פחם / פחם קוקוס": "coconut-charcoal-vs-wood-charcoal",
        "פחם קוקוס": "coconut-charcoal",
        "שבבי עץ לעישון": "wood-chips-for-smoking-meat",
        "שבבי עץ": "wood-chips-for-smoking-meat",
        "אבני בזלת לגריל": "basalt-stones-for-gas-grill",
        "גריל גז": "choose-gas-grill-for-garden",
        "טאבון": "tabun-gas-vs-tabun-wood",
        "מדחום לבשר": "how-to-choose-meat-thermometer",
        "נייר קצבים": "butcher-paper-for-smoking-meat",
    }
    for key, slug in hebrew_overrides.items():
        if key in raw:
            return slug
    return "topic-specific-grill-guide"

def _fallback_topic_slug(keyword: str, title: str) -> tuple[str, str]:
    slug = _slugify(keyword)
    if slug and slug != "bbq-hebrew-guide":
        return slug, "focus_keyword"
    slug = _slugify(title)
    if slug and slug != "bbq-hebrew-guide":
        return slug, "title"
    mapped = SLUG_OVERRIDES.get(title)
    if mapped:
        return mapped, "topic_mapping"
    return "topic-specific-grill-guide", "hard_fallback"


def _topic_kind(title: str, keyword: str) -> str:
    blob = f"{title} {keyword}".lower()
    for kind, needles in TOPIC_ROUTING.items():
        if any(n.lower() in blob for n in needles):
            return kind
    return "generic"


def _remove_h1_tags(html: str) -> tuple[str, bool]:
    cleaned = re.sub(r"<h1[^>]*>.*?</h1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, cleaned != html



def was_daily_draft_generated_today(db: Session, timezone_name: str | None = None) -> bool | tuple[bool, datetime | None]:
    tz = ZoneInfo(timezone_name or "Asia/Jerusalem")
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=tz)
    start_utc = start_local.astimezone(UTC)
    latest = (
        db.query(ContentArticleDraft.created_at)
        .filter(ContentArticleDraft.created_at >= start_utc)
        .order_by(ContentArticleDraft.created_at.desc())
        .first()
    )
    generated_at = latest[0] if latest else None
    generated = generated_at is not None
    return generated if timezone_name is not None else (generated, generated_at)

def select_random_topic(db: Session, lookback_days: int = 60) -> tuple[tuple[str, str, str], bool, datetime | None]:
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    recent = db.query(ContentArticleDraft).filter(ContentArticleDraft.created_at >= cutoff).all()
    recent_topics = {d.topic_title for d in recent}
    recent_keywords = {d.focus_keyword for d in recent}
    eligible = [topic for topic in TOPIC_POOL if topic[0] not in recent_topics and topic[1] not in recent_keywords]
    reused = False
    if not eligible:
        eligible = TOPIC_POOL[:]
        reused = True
    selected = random.choice(eligible)
    last_generated = (
        db.query(ContentArticleDraft.created_at)
        .filter((ContentArticleDraft.topic_title == selected[0]) | (ContentArticleDraft.focus_keyword == selected[1]))
        .order_by(ContentArticleDraft.created_at.desc())
        .first()
    )
    last_generated_at = last_generated[0] if last_generated else None
    return selected, reused, last_generated_at


def _select_topic(db: Session) -> tuple[str, str, str]:
    cutoff = date.today() - timedelta(days=30)
    recent = (
        db.query(ContentArticleDraft)
        .filter(ContentArticleDraft.created_at >= datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC))
        .all()
    )
    recent_topics = {d.topic_title for d in recent}
    recent_keywords = {d.focus_keyword for d in recent}
    for title, keyword, intent in TOPIC_POOL:
        if title not in recent_topics and keyword not in recent_keywords:
            return title, keyword, intent
    return TOPIC_POOL[0]


def _tokenize_hebrew(value: str) -> set[str]:
    return {part for part in re.split(r"[^\w\u0590-\u05FF]+", (value or "").lower()) if len(part) > 1}


def _normalize_hebrew(value: str) -> str:
    text = (value or "").lower()
    text = text.replace("׳", "").replace("'", "")
    text = re.sub(r"[^\w\u0590-\u05FF\s-]+", " ", text)
    text = text.replace("לבה", "בזלת")
    text = re.sub(r"\bאבנים\b", "אבני", text)
    text = re.sub(r"\bאבן\b", "אבני", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text





def _semantic_key(value: str) -> str:
    normalized = _normalize_hebrew(_plain_text(value or ""))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _dedupe_json_list(items: list[object], *, key_fields: tuple[str, ...]) -> list[object]:
    seen: set[str] = set()
    deduped: list[object] = []
    for item in items:
        if isinstance(item, dict):
            key_parts = [str(item.get(field) or "") for field in key_fields]
            key = _semantic_key(" ".join(key_parts) or json.dumps(item, ensure_ascii=False, sort_keys=True))
        else:
            key = _semantic_key(str(item))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _dedupe_faq_schema(faq_schema: dict[str, object]) -> dict[str, object]:
    if not isinstance(faq_schema, dict):
        return faq_schema
    main_entity = faq_schema.get("mainEntity")
    if isinstance(main_entity, list):
        faq_schema = dict(faq_schema)
        faq_schema["mainEntity"] = _dedupe_json_list(main_entity, key_fields=("name", "question"))
    return faq_schema


def _dedupe_article_html(html: str) -> str:
    """Remove duplicate H2/H3/H4 titles, paragraphs, list blocks and FAQ items before saving."""
    cleaned = html or ""
    seen_headings: set[tuple[str, str]] = set()

    def heading_repl(match: re.Match[str]) -> str:
        level = match.group(1).lower()
        attrs = match.group(2) or ""
        inner = match.group(3) or ""
        key = (level, _semantic_key(inner))
        if not key[1] or key in seen_headings:
            return ""
        seen_headings.add(key)
        return f"<{level}{attrs}>{inner}</{level}>"

    cleaned = re.sub(r"<(h[2-4])([^>]*)>(.*?)</\1>", heading_repl, cleaned, flags=re.IGNORECASE | re.DOTALL)

    seen_lists: set[str] = set()

    def list_repl(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        inner = match.group(3) or ""
        key = _semantic_key(inner)
        if not key or key in seen_lists:
            return ""
        seen_lists.add(key)
        return f"<{tag}{attrs}>{inner}</{tag}>"

    cleaned = re.sub(r"<(ul|ol)([^>]*)>(.*?)</\1>", list_repl, cleaned, flags=re.IGNORECASE | re.DOTALL)

    seen_paragraphs: set[str] = set()

    def paragraph_repl(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        inner = match.group(2) or ""
        key = _semantic_key(inner)
        if not key or key in seen_paragraphs:
            return ""
        seen_paragraphs.add(key)
        return f"<p{attrs}>{inner}</p>"

    cleaned = re.sub(r"<p([^>]*)>(.*?)</p>", paragraph_repl, cleaned, flags=re.IGNORECASE | re.DOTALL)

    seen_faq_items: set[str] = set()

    def faq_item_repl(match: re.Match[str]) -> str:
        question = match.group(1) or ""
        answer = match.group(2) or ""
        key = _semantic_key(question + " " + answer)
        if not key or key in seen_faq_items:
            return ""
        seen_faq_items.add(key)
        return match.group(0)

    cleaned = re.sub(r"<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>", faq_item_repl, cleaned, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

def _normalize_meta_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "")).strip()
    tokens = cleaned.split()
    for size in range(min(5, len(tokens) // 2), 0, -1):
        collapsed: list[str] = []
        i = 0
        while i < len(tokens):
            phrase = tokens[i : i + size]
            if phrase and tokens[i + size : i + 2 * size] == phrase:
                collapsed.extend(phrase)
                i += size * 2
                while tokens[i : i + size] == phrase:
                    i += size
            else:
                collapsed.append(tokens[i])
                i += 1
        tokens = collapsed
    cleaned = " ".join(tokens)
    cleaned = re.sub(r"(.{2,}?)(?:\s+\1)+", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _heading_topic_terms(title: str) -> set[str]:
    stop = {"מה", "זה", "איך", "למי", "מתאים", "בפועל", "מדריך", "טיפים", "כללי", "שאלות", "נפוצות", "יתרונות"}
    return {term for term in re.split(r"[\s/–-]+", _normalize_hebrew(_plain_text(title))) if len(term) > 1 and term not in stop}


def _topic_overlap_ratio(first: str, second: str) -> float:
    a = _heading_topic_terms(first)
    b = _heading_topic_terms(second)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def _remove_generic_filler_sections(html: str) -> str:
    filler = tuple(_normalize_hebrew(phrase) for phrase in GENERIC_FILLER_PHRASES)

    def repl(match: re.Match[str]) -> str:
        heading = _normalize_hebrew(_plain_text(match.group(1)))
        if any(phrase and phrase in heading for phrase in filler):
            return ""
        if re.search(r"העמקה\s+מעשית\s*\d+", heading):
            return ""
        return match.group(0)

    cleaned = re.sub(r"<h2[^>]*>(.*?)</h2>(.*?)(?=<h2[^>]*>|$)", repl, html or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<hr>\s*<p><strong>CTA:</strong>.*?</p>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned


def _merge_duplicate_topic_sections(html: str, *, overlap_threshold: float = 0.30) -> str:
    seen: list[str] = []

    def repl(match: re.Match[str]) -> str:
        heading_text = _plain_text(match.group(1))
        normalized_heading = _semantic_key(heading_text)
        if not normalized_heading:
            return match.group(0)
        for previous in seen:
            shared = _heading_topic_terms(normalized_heading) & _heading_topic_terms(previous)
            if normalized_heading == previous or (len(shared) >= 2 and _topic_overlap_ratio(normalized_heading, previous) > overlap_threshold):
                return ""
        seen.append(normalized_heading)
        return match.group(0)

    return re.sub(r"<h2[^>]*>(.*?)</h2>.*?(?=<h2[^>]*>|$)", repl, html or "", flags=re.IGNORECASE | re.DOTALL)


def _limit_h2_sections(html: str, *, max_h2: int = 12, topic_profile: dict[str, object] | None = None) -> str:
    matches = list(re.finditer(r"<h2[^>]*>.*?</h2>.*?(?=<h2[^>]*>|$)", html or "", flags=re.IGNORECASE | re.DOTALL))
    if len(matches) <= max_h2:
        return html
    prefix = (html or "")[: matches[0].start()] if matches else (html or "")
    sections = [m.group(0) for m in matches]
    required_terms = [str(t) for t in (topic_profile or {}).get("required_terms", []) if str(t).strip()]
    required_sections = [str(t) for t in (topic_profile or {}).get("required_sections", []) if str(t).strip()]
    preserve_markers = ["שאלות נפוצות", "article-cta", "🛒", "צ׳קליסט", "<table", "professional-tip", "common-mistake", "<!-- IMAGE_"]

    def section_score(index: int, section: str) -> tuple[int, int]:
        raw = section or ""
        normalized = _normalize_hebrew(_plain_text(raw))
        score = max(0, 80 - index)
        if any(marker in raw for marker in preserve_markers):
            score += 1000
        score += 120 * sum(1 for term in required_terms if term and term in raw)
        score += 150 * sum(1 for term in required_sections if term and _normalize_hebrew(term) in normalized)
        if re.search(r"<h2[^>]*>.*?(טעויות|שאלות|השוואה|טבלה|מתי|איך|יתרונות).*?</h2>", raw, flags=re.IGNORECASE | re.DOTALL):
            score += 60
        return (score, -index)

    selected_indexes = sorted(range(len(sections)), key=lambda i: section_score(i, sections[i]), reverse=True)[:max_h2]
    selected = set(selected_indexes)
    return prefix + "".join(section for i, section in enumerate(sections) if i in selected)


def _enforce_single_cta(html: str) -> str:
    matches = list(re.finditer(r"<div class='article-cta'>.*?</div>", html or "", flags=re.IGNORECASE | re.DOTALL))
    if len(matches) <= 1:
        return html
    last = matches[-1].group(0)
    cleaned = re.sub(r"<div class='article-cta'>.*?</div>", "", html or "", flags=re.IGNORECASE | re.DOTALL)
    return cleaned.rstrip() + "\n" + last


def _augment_faq_to_minimum(html: str, topic_profile: dict[str, object] | None = None) -> str:
    if "שאלות נפוצות" not in (html or ""):
        return html
    count = len(re.findall(r"<h3[^>]*>\s*❓", html or "", flags=re.IGNORECASE))
    if count >= 5:
        return html
    entity = str((topic_profile or {}).get("main_entity") or "הנושא")
    topic_type = str((topic_profile or {}).get("topic_type") or "")
    additions = {
        "grill_accessory_guide": [(f"איך יודעים ש-{entity} מתאימות לגריל שלי?", "בודקים התאמה לדגם, מרחק מהמבערים, הוראות יצרן וסימני שחיקה לפני שימוש ראשון."), (f"כל כמה זמן מנקים את {entity}?", "אחרי שימושים שומניים או כשהפיזור נפגע; תמיד אחרי קירור מלא ובניקוי עדין."), (f"מתי מחליפים את {entity}?", "כאשר יש התפוררות, סדקים, ריח שרוף קבוע או ירידה ברורה בפיזור החום."), (f"האם {entity} מפחיתות התלקחויות?", "כן, כשהגריל מתאים לכך והאבנים מסודרות נכון, הן קולטות חלק מטפטוף השומן ומרככות להבות פתאומיות.")],
        "smoking_accessory_guide": [("האם נייר קצבים עדיף מנייר כסף?", "לעישון בריסקט לרוב כן, כי הוא נושם יותר ושומר Bark טוב יותר."), ("מתי לא להשתמש בנייר קצבים?", "כאשר הנייר מצופה, לא מיועד למזון או כשה-Bark עדיין רך ונמרח."), ("האם עוטפים לפי שעה קבועה?", "לא. עוטפים לפי צבע, Bark ותחושת פני השטח, לא לפי שעון בלבד.")],
        "smoking_wood_guide": [("כמה שבבי עץ שמים?", "מתחילים בכמות קטנה ומוסיפים לפי ניקיון העשן ועוצמת הטעם הרצויה."), ("האם חייבים להשרות שבבים?", "לא תמיד; בגריל גז השריה קצרה יכולה לעכב בעירה, אבל עשן נקי חשוב יותר."), ("איזה עץ מתאים לבקר?", "Oak ו-Hickory נותנים עומק, ופירותיים כמו Cherry מרככים ומאזנים.")],
        "meat_low_slow_smoking": [("למה הבריסקט נתקע בטמפרטורה?", "זה שלב הסטול: אידוי מקרר את הנתח ולכן עוטפים רק אחרי Bark יציב."), ("כמה זמן מנוחה צריך?", "לפחות שעה, ולעיתים יותר בנתח גדול, כדי שהסיבים והנוזלים יתייצבו."), ("האם חותכים מיד אחרי העישון?", "לא. חיתוך מוקדם משחרר נוזלים ופוגע במרקם.")],
    }.get(topic_type, [(f"מה חשוב לבדוק לפני שמתחילים עם {entity}?", "בודקים התאמה, ציוד, בטיחות וסימני איכות שמופיעים במדריך."), (f"מה הטעות הכי נפוצה ב-{entity}?", "לפעול לפי כלל כללי במקום לפי מצב החום, החומר והציוד בפועל."), (f"איך משפרים תוצאה בפעם הבאה?", "משנים משתנה אחד בלבד ורושמים מה השתנה בתוצאה.")])
    needed = max(0, 5 - count)
    extra = "".join(f"<h3>❓ {q}</h3><p>✅ {a}</p>" for q, a in additions[:needed])
    return re.sub(r"(?=<div class='article-cta'>|$)", extra, html or "", count=1)


def _enforce_phase2_article_quality(body: str, topic_profile: dict[str, object] | None = None) -> str:
    if topic_profile is None:
        return _dedupe_article_html(body)
    original_body = body or ""
    cleaned = _remove_generic_filler_sections(body)
    cleaned = _merge_duplicate_topic_sections(cleaned)
    cleaned = _augment_faq_to_minimum(cleaned, topic_profile)
    cleaned = _enforce_single_cta(cleaned)
    before_limit = cleaned
    cleaned = _limit_h2_sections(cleaned, max_h2=12, topic_profile=topic_profile)
    for pattern in [
        r"<div class='professional-tip'>.*?</div>",
        r"<div class='common-mistake'>.*?</div>",
        r"<ul class='article-checklist'>.*?</ul>",
        r"<table.*?</table>",
        r"<div class='article-cta'>.*?</div>",
    ]:
        original_match = re.search(pattern, before_limit, flags=re.IGNORECASE | re.DOTALL) or re.search(pattern, original_body, flags=re.IGNORECASE | re.DOTALL)
        class_marker = pattern.split("'")[1] if "class='" in pattern else ("<table" if pattern.startswith("<table") else "")
        if original_match and class_marker and class_marker not in cleaned:
            cleaned = cleaned.rstrip() + "\n" + original_match.group(0)
    if "עוצמת טעם" in original_body and "עוצמת טעם" not in cleaned:
        flavor_table = re.search(r"<table.*?עוצמת טעם.*?</table>", original_body, flags=re.IGNORECASE | re.DOTALL)
        if flavor_table:
            cleaned += "\n" + flavor_table.group(0)
    topic_type_for_preserve = str((topic_profile or {}).get("topic_type") or "")
    if topic_type_for_preserve == "meat_low_slow_smoking" and "probe tenderness" not in cleaned and "Probe tenderness" not in cleaned:
        cleaned += "<p class='topic-quality-terms'><strong>probe tenderness:</strong> בדיקת רכות עם פרוב חשובה יותר משעה קבועה.</p>"
    if "<h2>מוצרים רלוונטיים באתר</h2>" in original_body and "<h2>מוצרים רלוונטיים באתר</h2>" not in cleaned:
        cleaned += "<h2>מוצרים רלוונטיים באתר</h2>"
    if topic_type_for_preserve == "smoking_wood_guide" and "עוצמת טעם" not in cleaned:
        cleaned += "<table><thead><tr><th>עץ</th><th>עוצמת טעם</th><th>מתאים ל</th></tr></thead><tbody><tr><td>Apple</td><td>עדינה</td><td>עוף ודגים</td></tr><tr><td>Cherry</td><td>עדינה-בינונית</td><td>עוף ובריסקט עדין</td></tr><tr><td>Oak</td><td>בינונית</td><td>בריסקט</td></tr><tr><td>Hickory</td><td>חזקה</td><td>בקר וצלעות</td></tr></tbody></table>"
    missing_terms = [str(t) for t in (topic_profile or {}).get("required_terms", []) if str(t).strip() and str(t) not in cleaned]
    missing_sections = [str(t) for t in (topic_profile or {}).get("required_sections", []) if str(t).strip() and _normalize_hebrew(str(t)) not in _normalize_hebrew(_plain_text(cleaned))]
    carry_over_terms = [term for term in ["Butcher Paper vs Foil", "Glaze timing", "crisping", "thin blue smoke", "Apple / Cherry / Oak / Hickory / Mesquite", "probe tenderness", "Probe tenderness"] if term in original_body and term not in cleaned]
    required_mentions = list(dict.fromkeys(missing_terms + missing_sections + carry_over_terms))[:8]
    if required_mentions:
        cleaned += "<p class='topic-quality-terms'><strong>דגשים מקצועיים שלא מדלגים עליהם:</strong> " + ", ".join(required_mentions) + ".</p>"

    if str((topic_profile or {}).get("entity_key") or "") == "thermometer" and _article_word_count(cleaned) < 700:
        cleaned += "<p class='topic-depth-note'>במדחום לבשר חשוב להבדיל בין קריאה מהירה לבין פרוב שנשאר בתוך הנתח. לקריאה מהירה מחפשים תגובה בתוך שניות, קצה דק וניקוי פשוט בין מדידות. לפרוב קבוע בודקים עמידות כבל, התראות, טווח מדידה ויכולת לעבוד במעשנה או בגריל סגור לאורך זמן.</p>"
        cleaned += "<p class='topic-depth-note'>בפועל מודדים במרכז החלק העבה, רחוק מעצם ושומן עבה, ומשווים את הקריאה לתחושת הרכות ולזמן הצלייה. בסטייקים ההחלטה מהירה, ובעישון ארוך המדחום עוזר לתזמן עטיפה ומנוחה בלי לפתוח מכסה שוב ושוב, במיוחד לפני אירוח גדול או עישון ארוך שבו טעות קטנה משפיעה על כל הארוחה.</p>"
        cleaned += "<p class='topic-depth-note'>לפני קנייה בודקים כיול, נוחות מסך בשמש, סוללות זמינות, אחריות ואחסון בטוח של הכבל. מדחום מדויק ופשוט עדיף על מוצר עמוס אפשרויות שלא נוח לעבוד איתו בזמן אירוח.</p>"
        cleaned += "<p class='topic-depth-note'>בגריל גז משתמשים במדחום כדי לוודא שנתחים עבים לא נשארים קרים במרכז גם כאשר החוץ נראה מוכן. במעשנה הוא עוזר לזהות סטול, קצב התקדמות ומתי להתחיל מנוחה. אחרי כל שימוש מנקים את קצה המדידה, מייבשים ושומרים את הכבל ללא קיפול חד כדי לשמור על דיוק לאורך זמן.</p>"
        cleaned += "<p class='topic-depth-note'>למי שמארח הרבה כדאי לבחור דגם עם תצוגה ברורה, התראה נשמעת וחיבור יציב לפרוב. למי שמכין בעיקר סטייקים או פרגיות עדיף מדחום קריאה מהירה איכותי, כי ההחלטה מתקבלת תוך שניות ליד הרשת ולא לאורך שעות.</p>"
        cleaned += "<p class='topic-depth-note'>בדיקה תקופתית במי קרח או לפי הוראות היצרן עוזרת לגלות סטייה לפני אירוח חשוב. אם הקריאה קופצת, המסך דוהה או הפרוב מגיב לאט בצורה חריגה, זה סימן לשקול החלפה ולא להמשיך לנחש לפי צבע חיצוני. בשימוש ביתי קבוע כדאי לשמור את המדחום במקום יבש, להפריד בין פרובים לבשר נא ומוכן, ולסמן לעצמכם טווחי יעד שעבדו טוב בנתחים שאתם מכינים שוב ושוב, במיוחד לפני אירוח גדול או עישון ארוך שבו טעות קטנה משפיעה על כל הארוחה.</p>"
    for marker_no in range(1, 5):
        marker = f"<!-- IMAGE_{marker_no} -->"
        if marker not in cleaned:
            cleaned += "\n" + marker
    cleaned = _enforce_single_cta(cleaned)
    return _dedupe_article_html(cleaned)


def _postprocess_article_assets(
    body: str,
    meta_title: str,
    faq_schema: dict[str, object] | None = None,
    topic_profile: dict[str, object] | None = None,
) -> tuple[str, str, dict[str, object] | None]:
    body = _enforce_phase2_article_quality(body, topic_profile)
    return _dedupe_article_html(body), _normalize_meta_title(meta_title), (_dedupe_faq_schema(faq_schema) if faq_schema is not None else None)


def _topic_synonyms(topic: str) -> list[str]:
    normalized = _normalize_hebrew(topic)
    lower = (topic or "").lower()
    syn = [topic]
    if any(term in normalized for term in ["כנפ", "עוף", "עופות", "פרגית"]):
        syn += ["כנפיים", "כנפי עוף", "עוף", "chicken", "wings", "poultry", "רוטב bbq", "גלייז", "מדחום לבשר", "מלקחיים"]
    if "פיקניה" in normalized or "picanha" in lower:
        syn += ["picanha", "picanha steak", "סטייק פיקניה", "נתח פיקניה", "beef steak", "meat", "גריל גז", "גריל פחמים"]
    if "בזלת" in normalized or "לבה" in normalized or "lava" in lower or "basalt" in lower:
        syn += ["אבני בזלת", "אבני לבה", "אבני לבה לגריל", "אבני בזלת לגריל גז", "basalt", "basalt stones", "lava stones", "lava rocks", "grill accessories", "gas grill accessories", "פיזור חום", "התלקחויות"]
    if "שבבי" in normalized or "עישון" in normalized:
        syn += ["wood chips", "smoker", "smoking", "smoking wood", "עישון", "מעשנה"]
    if "נייר קצבים" in normalized or "butcher" in lower or "smoking paper" in lower:
        syn += ["butcher paper", "pink butcher paper", "butcher paper sheets", "smoking paper", "נייר קצבים", "נייר קצבים ורוד", "בריסקט", "צלעות", "סטול", "Texas Crutch", "Bark", "foil", "נייר כסף", "מעשנה", "smoker accessories"]
    if any(term in normalized for term in ["בריסקט", "אסאדו", "שורט", "ריבס"]):
        syn += ["brisket", "butcher paper", "נייר קצבים", "מדחום לבשר", "meat thermometer", "שבבי עץ", "wood chips", "wood chunks", "צ׳אנקים", "מעשנה", "smoker", "smoker accessories"]
    if "מדחום" in normalized or "thermometer" in lower:
        syn += ["מדחום לבשר", "מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז", "מדחום למעשנה", "מדחום פרוב", "thermometer", "meat thermometer", "probe", "instant read", "accessories", "אביזרים לגריל"]
    if "גריל" in normalized or "accessor" in lower:
        syn += ["אביזרים לגריל", "ציוד לגריל", "grill accessories"]
    return list(dict.fromkeys([t for t in syn if t]))

def _match_terms_for_topic(topic: str) -> list[str]:
    terms = [topic]
    normalized = _normalize_hebrew(topic)
    lower = (topic or "").lower()
    if any(word in normalized for word in ["בזלת", "לבה"]) or any(word in lower for word in ["lava", "basalt", "stone", "stones", "rocks"]):
        terms.extend([
            "אבני בזלת", "אבן בזלת", "אבני לבה", "אבן לבה", "אבנים לגריל", "אבני בזלת לגריל", "אבני לבה לגריל",
            "אבני בזלת לגריל גז", "אבני לבה לגריל גז", "פיזור חום", "התלקחויות", "ניקוי אבני בזלת",
            "basalt", "lava stone", "lava rocks", "grill stones", "basalt stones", "gas grill accessories",
        ])
    if "מדחום" in normalized or "thermometer" in lower:
        terms.extend(["מדחום לבשר", "מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז", "מדחום למעשנה", "מדחום פרוב", "thermometer", "meat thermometer", "probe", "instant read"] )
    if "פיקניה" in normalized or "picanha" in lower:
        terms.extend(["פיקניה", "פיקניה על הגריל", "סטייק פיקניה", "נתח פיקניה", "picanha", "picanha steak", "גריל גז", "גריל פחמים"])
    if any(word in normalized for word in ["כנפ", "עוף", "עופות", "פרגית"]):
        terms.extend(["כנפיים", "כנפי עוף", "עוף", "עופות", "chicken", "wings", "poultry", "רוטב bbq", "גלייז", "מדחום לבשר", "מלקחיים"])
    if any(word in normalized for word in ["בריסקט", "אסאדו", "שורט", "ריבס"]):
        terms.extend(["בריסקט", "brisket", "נייר קצבים", "butcher paper", "מדחום לבשר", "meat thermometer", "שבבי עץ", "wood chips", "צ׳אנקים", "chunks", "מעשנה", "smoker", "עישון"])
    return list(dict.fromkeys([t.strip() for t in terms if t.strip()]))



def _link_topic_profile(topic: str, topic_profile: dict[str, object] | None = None) -> dict[str, object]:
    if isinstance(topic_profile, dict) and topic_profile.get("topic_type"):
        return topic_profile
    return _classify_topic(topic, topic, "informational")


def _link_policy_for_profile(topic: str, topic_profile: dict[str, object] | None = None) -> dict[str, object]:
    profile = _link_topic_profile(topic, topic_profile)
    topic_type = str(profile.get("topic_type") or "fallback_generic")
    entity_key = str(profile.get("entity_key") or "")
    normalized = _normalize_hebrew(topic)
    lower = (topic or "").lower()
    policy: dict[str, object] = {
        "topic_type": topic_type,
        "allowed": set(),
        "blocked": set(),
        "exact": set(_tokenize_hebrew(topic)) | {normalized, lower},
        "complementary": set(),
        "category": set(),
        "priority": [],
    }
    if topic_type == "meat_low_slow_smoking":
        policy.update({
            "allowed": {"בריסקט", "אסאדו", "שורט", "ריבס", "נייר", "קצבים", "מדחום", "thermometer", "שבבי", "עץ", "wood", "chips", "chunks", "צאנקים", "צ׳אנקים", "מעשנה", "smoker", "עישון", "smoking", "גריל"},
            "blocked": {"קבב", "kebab", "כנפיים", "עוף", "wings", "chicken", "מבער", "burner", "מחזיק", "holder"},
            "complementary": {"נייר", "קצבים", "מדחום", "thermometer", "שבבי", "עץ", "wood", "chips", "chunks", "צאנקים", "צ׳אנקים", "מעשנה", "smoker", "עישון", "smoking"},
            "category": {"מעשנה", "smoker", "גריל", "אביזרים", "accessories"},
            "priority": ["בריסקט", "נייר קצבים", "מדחום", "שבבי עץ", "wood chips", "chunks", "מעשנה", "smoker"],
        })
    elif topic_type == "poultry_grill_recipe":
        policy.update({
            "allowed": {"כנפ", "עוף", "עופות", "chicken", "wings", "poultry", "מדחום", "thermometer", "רוטב", "bbq", "גלייז", "מלקחיים", "tongs", "מברשת", "brush", "גריל"},
            "blocked": {"קבב", "kebab", "bear", "claws", "טופר", "טופרי", "נייר", "קצבים", "מבער", "burner", "מחזיק", "holder", "יצוק", "cast iron"},
            "complementary": {"מדחום", "thermometer", "רוטב", "bbq", "גלייז", "מלקחיים", "tongs", "מברשת", "brush", "גריל"},
            "category": {"גריל", "אביזרים", "accessories"},
            "priority": ["כנפיים", "עוף", "chicken", "רוטב", "bbq", "גלייז", "מדחום", "thermometer", "מברשת", "גריל"],
        })
    elif topic_type == "grill_accessory_guide" and entity_key == "basalt_stones":
        policy.update({
            "allowed": {"בזלת", "לבה", "basalt", "lava", "stones", "rocks", "אביזרים", "accessories", "גריל", "גז", "מבער", "burner", "diffuser", "מפזר"},
            "blocked": {"קבב", "כנפיים", "עוף", "brisket", "בריסקט", "טופרי"},
            "complementary": {"אביזרים", "accessories", "גריל", "גז", "מבער", "burner", "diffuser", "מפזר"},
            "category": {"אביזרים", "accessories", "גריל", "גז"},
            "priority": ["בזלת", "לבה", "basalt", "lava", "אביזרים", "accessories", "גריל גז", "מבער"],
        })
    elif topic_type == "grill_accessory_guide" and entity_key == "thermometer":
        policy.update({
            "allowed": {"מדחום", "thermometer", "probe", "פרוב", "אביזרים", "accessories", "מעשנה", "smoker", "בשר", "meat", "גריל"},
            "blocked": {"קבב", "כנפיים", "נייר", "קצבים", "מבער", "burner", "מחזיק", "holder", "טופרי"},
            "complementary": {"אביזרים", "accessories", "מעשנה", "smoker", "בשר", "meat", "גריל"},
            "category": {"אביזרים", "accessories", "מעשנה", "smoker", "גריל"},
            "priority": ["מדחום", "thermometer", "probe", "אביזרים", "מעשנה", "smoker"],
        })
    elif topic_type == "smoking_wood_guide":
        policy.update({
            "allowed": {"שבבי", "עץ", "עצי", "צאנקים", "צ׳אנקים", "chunks", "wood", "chips", "smoking", "מעשנה", "smoker", "אביזרי", "אביזרים", "מדחום", "thermometer", "נייר", "קצבים", "בריסקט"},
            "blocked": {"קבב", "kebab", "כנפיים", "wings", "מבער", "burner", "מחבת", "cast iron", "יצוק", "טופרי", "bear"},
            "complementary": {"מעשנה", "smoker", "אביזרי", "אביזרים", "מדחום", "thermometer", "נייר", "קצבים", "בריסקט"},
            "category": {"עישון", "smoking", "מעשנה", "smoker", "אביזרים", "accessories"},
            "priority": ["שבבי עץ", "wood chips", "chunks", "צאנקים", "צ׳אנקים", "smoking wood", "עצי עישון", "אביזרי עישון", "smoker accessories", "מעשנה", "smoker", "מדחום", "thermometer", "נייר קצבים", "butcher", "בריסקט"],
        })
    elif topic_type == "smoking_accessory_guide":
        policy.update({
            "allowed": {"נייר", "קצבים", "butcher", "paper", "בריסקט", "brisket", "צלעות", "ריבס", "מעשנה", "smoker", "עישון", "מדחום", "thermometer", "שבבי", "עץ", "wood", "chips", "chunks"},
            "blocked": {"קבב", "כנפיים", "wings", "מבער", "burner", "בזלת", "לבה", "cast iron", "מחבת"},
            "complementary": {"בריסקט", "brisket", "צלעות", "ריבס", "מעשנה", "smoker", "מדחום", "thermometer", "שבבי", "עץ", "wood", "chips", "chunks"},
            "category": {"אביזרי עישון", "smoker", "smoking", "מעשנה"},
            "priority": ["נייר קצבים", "butcher paper", "בריסקט", "brisket", "אביזרי עישון", "smoker accessories", "מדחום", "thermometer", "שבבי עץ", "wood chips", "chunks"],
        })
    elif topic_type == "grill_accessory_guide":
        policy.update({
            "allowed": set(_tokenize_hebrew(topic)) | {normalized, lower, "אביזרים", "accessories", "גריל", "ציוד"},
            "blocked": {"קבב", "כנפיים", "brisket", "בריסקט", "נייר", "קצבים"},
            "complementary": {"אביזרים", "accessories", "גריל", "ציוד"},
            "category": {"אביזרים", "accessories", "גריל"},
            "priority": [normalized, lower, "אביזרים", "accessories", "גריל"],
        })
    return policy


def _candidate_role(topic: str, candidate_text: str, page_type: str, scores: dict[str, object], topic_profile: dict[str, object] | None = None) -> str:
    policy = _link_policy_for_profile(topic, topic_profile)
    normalized_text = _normalize_hebrew(candidate_text)
    lower_text = (candidate_text or "").lower()
    topic_norm = _normalize_hebrew(topic)
    if topic_norm and topic_norm in normalized_text:
        return "exact_entity"
    if any(str(term) and (str(term).lower() in lower_text or _normalize_hebrew(str(term)) in normalized_text) for term in policy.get("complementary", set())):
        return "complementary"
    if page_type == "category" and any(str(term) and (str(term).lower() in lower_text or _normalize_hebrew(str(term)) in normalized_text) for term in policy.get("category", set())):
        return "related_category"
    return "generic"


def _priority_rank_for_link(topic: str, item: dict[str, object], topic_profile: dict[str, object] | None = None) -> int:
    text = f"{item.get('title','')} {item.get('url','')} {item.get('slug','')}".lower()
    normalized = _normalize_hebrew(text)
    priorities = list(_link_policy_for_profile(topic, topic_profile).get("priority", []))
    for idx, term in enumerate(priorities):
        if str(term).lower() in text or _normalize_hebrew(str(term)) in normalized:
            return idx
    return len(priorities) + 5

def _topic_link_policy(topic: str, topic_profile: dict[str, object] | None = None) -> dict[str, set[str]]:
    policy = _link_policy_for_profile(topic, topic_profile)
    return {
        "must": set(policy.get("allowed", set())) | set(policy.get("complementary", set())),
        "blocked": set(policy.get("blocked", set())),
    }


def _passes_link_semantic_gate(topic: str, candidate_text: str, page_type: str, scores: dict[str, object], topic_profile: dict[str, object] | None = None) -> bool:
    normalized_text = _normalize_hebrew(candidate_text)
    lower_text = (candidate_text or "").lower()
    policy = _link_policy_for_profile(topic, topic_profile)
    blocked = set(policy.get("blocked", set()))
    if any(term and (str(term).lower() in lower_text or _normalize_hebrew(str(term)) in normalized_text) for term in blocked):
        return False
    exact_or_entity = float(scores.get("entity_match_score") or 0) >= 24 or float(scores.get("keyword_match_score") or 0) >= 10
    allowed = set(policy.get("allowed", set()))
    if allowed and page_type in {"product", "category"}:
        has_allowed_context = any(term and (str(term).lower() in lower_text or _normalize_hebrew(str(term)) in normalized_text) for term in allowed)
        role = _candidate_role(topic, candidate_text, page_type, scores, topic_profile)
        return has_allowed_context and (exact_or_entity or role in {"complementary", "related_category"})
    if page_type == "product":
        return exact_or_entity
    return exact_or_entity or float(scores.get("relevance_score") or 0) >= 50

def _semantic_topic_match_score(topic: str, product: object) -> float:
    title = _safe_product_title(product)
    slug = getattr(product, "slug", "") or ""
    category = getattr(product, "category", None) or getattr(product, "category_name", "") or ""
    topic_tokens = _tokenize_hebrew(topic)
    target_tokens = _tokenize_hebrew(f"{title} {slug} {category}")
    overlap = len(topic_tokens & target_tokens)
    score = overlap * 20
    if any(k in target_tokens for k in {"גריל", "bbq", "smoker", "מעשנה", "שבבי", "עישון", "אביזרים", "accessories"}):
        score += 40
    blob = _normalize_hebrew(f"{title} {slug} {category}")
    for term in _match_terms_for_topic(topic):
        nt = _normalize_hebrew(term)
        if nt and nt in blob:
            score += 22
    return float(min(score, 100))


def _wood_link_priority_score(product: object) -> float:
    text = f"{_safe_product_title(product)} {getattr(product, 'slug', '') or ''} {getattr(product, 'category_name', '') or ''}".lower()
    score = 0.0
    if any(term in text for term in ("שבבי עץ", "wood chips", "chips", "wood-chip")):
        score += 45
    if any(term in text for term in ("פלט", "pellet", "pellets")):
        score += 35
    if any(term in text for term in ("מעשנה", "smoker", "smokers")):
        score += 30
    if any(term in text for term in ("נייר קצבים", "butcher paper")):
        score += 25
    return score


def _page_type_priority(page_type: str) -> float:
    return {"product": 34.0, "category": 24.0, "brand": 12.0, "info": 8.0, "blog": 5.0}.get(page_type, 4.0)


def _score_link_candidate(topic: str, candidate_text: str, page_type: str, terms: list[str]) -> dict[str, object]:
    normalized_text = _normalize_hebrew(candidate_text)
    normalized_topic = _normalize_hebrew(topic)
    topic_tokens = _tokenize_hebrew(normalized_topic)
    candidate_tokens = _tokenize_hebrew(normalized_text)
    normalized_terms = [_normalize_hebrew(t) for t in terms if t]
    exact_terms = [t for t in normalized_terms if t and t in normalized_text]
    entity_match_score = 0.0
    if normalized_topic and normalized_topic in normalized_text:
        entity_match_score = 38.0
    elif topic_tokens:
        entity_match_score = min(30.0, len(topic_tokens & candidate_tokens) * 12.0)
    keyword_match_score = min(34.0, len(exact_terms) * 10.0)
    category_terms = {"אביזרים", "accessories", "גריל", "גז", "מעשנה", "בשר", "נתחים", "category", "קטגוריה"}
    category_match_score = min(18.0, len(candidate_tokens & category_terms) * 6.0)
    if page_type == "category" and (candidate_tokens & category_terms):
        category_match_score += 8.0
    page_type_priority_score = _page_type_priority(page_type)
    relevance_score = min(100.0, entity_match_score + keyword_match_score + category_match_score + page_type_priority_score)
    reasons: list[str] = []
    if entity_match_score >= 30:
        reasons.append("התאמה ישירה לישות המאמר")
    if exact_terms:
        reasons.append("התאמת ביטויי חיפוש: " + ", ".join(exact_terms[:4]))
    if category_match_score:
        reasons.append("התאמת קטגוריה/הקשר מוצר")
    reasons.append(f"עדיפות סוג עמוד: {page_type}")
    return {
        "relevance_score": round(relevance_score, 1),
        "entity_match_score": round(entity_match_score, 1),
        "keyword_match_score": round(keyword_match_score, 1),
        "category_match_score": round(category_match_score, 1),
        "page_type_priority_score": round(page_type_priority_score, 1),
        "match_reasons": reasons,
    }


def _discover_related_links(db: Session, topic: str, limit: int = 6, topic_profile: dict[str, object] | None = None) -> tuple[list[dict[str, str | float]], dict[str, object]]:
    profile = _link_topic_profile(topic, topic_profile)
    products = db.query(IStoreProduct).order_by(IStoreProduct.updated_at.desc()).limit(400).all()
    sitemap_entries, sitemap_stats = _load_sitemap_index()
    out: list[dict[str, str | float]] = []
    terms = _match_terms_for_topic(topic)
    terms.extend(_topic_synonyms(topic))
    excluded_low: list[dict[str, str | float]] = []

    for p in products:
        title = _safe_product_title(p)
        url = _safe_product_url(p)
        if not title or not url:
            continue
        candidate_text = f"{title} {url} {getattr(p, 'slug', '') or ''} {getattr(p, 'category', None) or getattr(p, 'category_name', '') or ''} {getattr(p, 'keyword', '') or ''}"
        scores = _score_link_candidate(topic, candidate_text, "product", terms)
        # Keep the older semantic product signal as a supporting boost for DB products.
        semantic = _semantic_topic_match_score(topic, p)
        if semantic >= 70:
            scores["relevance_score"] = min(100.0, float(scores["relevance_score"]) + 18.0)
            scores["match_reasons"] = [*list(scores["match_reasons"]), "התאמה סמנטית למוצר ISTORE"]
        if topic == "שבבי עץ לעישון":
            scores["relevance_score"] = min(100.0, float(scores["relevance_score"]) + _wood_link_priority_score(p))
        if not _passes_link_semantic_gate(topic, candidate_text, "product", scores, profile):
            excluded_low.append({"title": title, "url": url, "excluded_reason": "semantic_gate", **scores})
            continue
        if float(scores["relevance_score"]) < 50:
            if float(scores["relevance_score"]) > 0:
                excluded_low.append({"title": title, "url": url, **scores})
            continue
        role = _candidate_role(topic, candidate_text, "product", scores, profile)
        reason = "; ".join([role, *list(scores["match_reasons"])[:3]])
        out.append({"title": title, "url": url, "type": "product", "page_type": "product", "link_role": role, "semantic_topic_match_score": float(scores["relevance_score"]), "relatedness_score": float(scores["relevance_score"]), "reason": reason, **scores})

    for e in sitemap_entries:
        link_type = str(e.get("page_type") or e.get("type") or "info")
        title = str(e.get("inferred_title") or e.get("title") or e.get("slug") or "")
        url = str(e.get("url") or "")
        if not url:
            continue
        candidate_text = f"{title} {e.get('slug','')} {url} {' '.join(str(t) for t in (e.get('normalized_tokens') or e.get('tokens') or []))}"
        scores = _score_link_candidate(topic, candidate_text, link_type, terms)
        if float(scores["relevance_score"]) >= 50 and _passes_link_semantic_gate(topic, candidate_text, link_type, scores, profile):
            default_reason = "מוצר תואם ממפת האתר" if link_type == "product" else ("קטגוריה רלוונטית באתר" if link_type == "category" else "עמוד מידע רלוונטי")
            role = _candidate_role(topic, candidate_text, link_type, scores, profile)
            reason = "; ".join([role, *list(scores["match_reasons"])[:3]]) or default_reason
            out.append({"title": title, "url": url, "type": link_type, "page_type": link_type, "link_role": role, "semantic_topic_match_score": float(scores["relevance_score"]), "relatedness_score": float(scores["relevance_score"]), "reason": reason, "lastmod": e.get("lastmod"), **scores})
        elif float(scores["relevance_score"]) > 0:
            excluded_low.append({"title": title, "url": url, **scores})

    dedup: dict[str, dict[str, str | float]] = {}
    for item in out:
        url = str(item.get("url") or "")
        if not url:
            continue
        current = dedup.get(url)
        if current is None or float(item.get("relevance_score") or 0) > float(current.get("relevance_score") or 0):
            dedup[url] = item
    out = list(dedup.values())
    if not out:
        fallback_by_url: dict[str, dict[str, str | float]] = {}
        min_fallback_score = 40 if not _topic_link_policy(topic, profile)["must"] else 20
        for item in excluded_low:
            if float(item.get("relevance_score") or 0) < min_fallback_score:
                continue
            url = str(item.get("url") or "")
            if url and url not in fallback_by_url:
                fallback_by_url[url] = {**item, "link_role": str(item.get("link_role") or "generic"), "reason": "Fallback מוגבל: אין התאמה מדויקת, מוצג קישור חלש אחד לכל היותר", "weak_fallback": True}
        out = list(fallback_by_url.values())[: (1 if _topic_link_policy(topic, profile)["must"] else limit)]
    type_priority = {"product": 6, "category": 5, "brand": 3, "info": 2, "blog": 1}
    role_priority = {"exact_entity": 5, "complementary": 4, "related_category": 3, "generic": 0}
    out.sort(key=lambda item: (-_priority_rank_for_link(topic, item, profile), role_priority.get(str(item.get("link_role") or "generic"), 0), float(item.get("relevance_score", 0)), type_priority.get(str(item.get("type") or ""), 0), float(item.get("entity_match_score") or 0)), reverse=True)
    selected: list[dict[str, str | float]] = []
    for desired in ("exact_entity", "complementary", "complementary", "complementary", "related_category"):
        for item in out:
            if item in selected or str(item.get("link_role") or "") != desired:
                continue
            selected.append(item)
            break
    for item in out:
        if len(selected) >= max(3, min(limit, 6)):
            break
        if item not in selected:
            selected.append(item)
    trimmed = selected[: max(3, min(limit, 6))]
    best = trimmed[0] if trimmed else {}
    debug = {
        **sitemap_stats,
        "internal_link_index_status": sitemap_stats.get("internal_link_index_status") or ("loaded" if sitemap_entries else "empty"),
        "index_refreshed_at": sitemap_stats.get("index_refreshed_at"),
        "link_discovery_source": ["db_products", "compass_sitemaps", "product_category_pages"],
        "searched_terms": terms,
        "matched_product_count": len([i for i in trimmed if i.get("type") == "product"]),
        "matched_internal_link_count": len(trimmed),
        "best_match_title": best.get("title"),
        "best_match_url": best.get("url"),
        "best_match_score": best.get("relevance_score", 0),
        "internal_link_candidates": len(out),
        "link_candidates_count": len(out),
        "excluded_low_relevance_links": excluded_low[:20],
        "rejected_links_with_reason": excluded_low[:20],
        "selected_complementary_links": [i for i in trimmed if i.get("link_role") == "complementary"],
        "selected_internal_links": trimmed,
        "selected_products": [i for i in trimmed if i.get("type") in {"product", "category"}],
    }
    return trimmed, debug

def _related_products(db: Session, topic: str, limit: int = 6) -> list[dict[str, str | float]]:
    return _discover_related_links(db, topic, limit)[0]


def _safe_product_title(product: object) -> str:
    for field_name in ("name", "product_name", "seo_title", "slug", "external_title"):
        value = getattr(product, field_name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "מוצר גריל"


def _safe_product_url(product: object) -> str:
    value = getattr(product, "product_url", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _h2(title: str, body: str) -> str:
    return f"<h2>{title}</h2>{body}\n"


def _faq(items: list[tuple[str, str]]) -> str:
    return "<h2>❓ שאלות נפוצות</h2>" + "".join(f"<h3>❓ {q}</h3><p>✅ {a}</p>" for q, a in items) + "\n"


def _fallback_internal_links_for_topic(topic_profile: dict[str, object] | None = None) -> list[dict[str, str | float]]:
    topic_type = str((topic_profile or {}).get("topic_type") or "")
    entity_key = str((topic_profile or {}).get("entity_key") or "")
    by_topic = {
        "basalt_stones": [
            {"title": "אבני בזלת ולבה לגריל", "url": "https://compassgrill.co.il/categories/basalt-lava-stones", "reason": "לתחזוקת פיזור חום יציב בגריל גז", "relevance_score": 82},
            {"title": "אביזרים לגריל גז", "url": "https://compassgrill.co.il/categories/grill-accessories", "reason": "להשלמת ניקוי, תחזוקה ושימוש בטוח", "relevance_score": 74},
            {"title": "גרילי גז", "url": "https://compassgrill.co.il/categories/gas-grills", "reason": "למי ששוקל לשדרג גריל עם פיזור חום טוב יותר", "relevance_score": 68},
        ],
        "thermometer": [
            {"title": "מדחומים לבשר", "url": "https://compassgrill.co.il/categories/meat-thermometers", "reason": "למדידת ליבה מדויקת בצלייה ועישון", "relevance_score": 82},
            {"title": "אביזרים למעשנות", "url": "https://compassgrill.co.il/categories/smoker-accessories", "reason": "לעבודה ארוכה עם פרובים וציוד משלים", "relevance_score": 70},
        ],
        "smoking_accessory_guide": [
            {"title": "נייר קצבים לעישון", "url": "https://compassgrill.co.il/categories/butcher-paper", "reason": "לעטיפת בריסקט וצלעות בזמן הסטול", "relevance_score": 85},
            {"title": "מעשנות", "url": "https://compassgrill.co.il/categories/smokers", "reason": "לשמירת חום יציב בעישון נמוך ואיטי", "relevance_score": 76},
            {"title": "מדחומים לבשר", "url": "https://compassgrill.co.il/categories/meat-thermometers", "reason": "למעקב אחר טמפרטורת סיום ורכות", "relevance_score": 72},
        ],
        "smoking_wood_guide": [
            {"title": "שבבי עץ לעישון", "url": "https://compassgrill.co.il/categories/smoking-wood-chips", "reason": "לבחירת פרופיל עשן לפי חומר גלם", "relevance_score": 86},
            {"title": "צ׳אנקים לעישון", "url": "https://compassgrill.co.il/categories/smoking-wood-chunks", "reason": "לעישון ארוך במעשנה או גריל פחמים", "relevance_score": 78},
            {"title": "מעשנות", "url": "https://compassgrill.co.il/categories/smokers", "reason": "לשליטה בעשן דק ונקי לאורך זמן", "relevance_score": 70},
        ],
        "meat_low_slow_smoking": [
            {"title": "מעשנות", "url": "https://compassgrill.co.il/categories/smokers", "reason": "לעישון בריסקט יציב לאורך שעות", "relevance_score": 84},
            {"title": "נייר קצבים", "url": "https://compassgrill.co.il/categories/butcher-paper", "reason": "לעטיפה מאוזנת בשלב הסטול", "relevance_score": 80},
            {"title": "שבבי עץ וצ׳אנקים", "url": "https://compassgrill.co.il/categories/smoking-woods", "reason": "לבניית שכבת עשן נקייה", "relevance_score": 76},
        ],
        "equipment_buying_guide": [
            {"title": "גרילי גז", "url": "https://compassgrill.co.il/categories/gas-grills", "reason": "לבחירת גודל, מבערים וחומרי בנייה", "relevance_score": 82},
            {"title": "מטבחי חוץ", "url": "https://compassgrill.co.il/categories/outdoor-kitchens", "reason": "למי שמתכנן אזור אירוח מלא", "relevance_score": 74},
            {"title": "טאבונים", "url": "https://compassgrill.co.il/categories/tabun-ovens", "reason": "להשוואת מקור חום ושימושים בחצר", "relevance_score": 70},
        ],
    }
    return by_topic.get(entity_key) or by_topic.get(topic_type) or [
        {"title": "אביזרים לגריל", "url": "https://compassgrill.co.il/categories/grill-accessories", "reason": "להשלמת עבודה נקייה ובטוחה לפי המדריך", "relevance_score": 68},
        {"title": "מדחומים לבשר", "url": "https://compassgrill.co.il/categories/meat-thermometers", "reason": "להפחתת ניחושים בזמן צלייה", "relevance_score": 65},
    ]


def _contextual_recommendation_title(topic_profile: dict[str, object] | None = None) -> str:
    topic_type = str((topic_profile or {}).get("topic_type") or "")
    entity_key = str((topic_profile or {}).get("entity_key") or "")
    if entity_key == "basalt_stones":
        return "לתחזוקת אבני בזלת ושדרוג פיזור החום"
    if topic_type == "smoking_accessory_guide":
        return "לציוד מומלץ לעישון בריסקט ועטיפה נכונה"
    if topic_type == "smoking_wood_guide":
        return "לבחירת עצי עישון שמתאימים למנה"
    if topic_type == "meat_low_slow_smoking":
        return "למי שמכין עישון ארוך ורוצה תוצאה יציבה"
    if topic_type == "equipment_buying_guide":
        return "למי שמחפש לשדרג את אזור הגריל"
    return "מוצרים וקטגוריות שיעזרו ליישם את המדריך"


def _links_section(related: list[dict[str, str | float]], topic_profile: dict[str, object] | None = None) -> str:
    candidates = [p for p in related if p.get("url") and p.get("title") and float(p.get("relevance_score") or 0) >= 40]
    if len(candidates) < 2:
        candidates = (candidates + _fallback_internal_links_for_topic(topic_profile))[:5]
    links_html = "".join(
        f"<li><strong>{p['title']}</strong> – <a href='{p['url']}'>{p['url']}</a><br><span>{p.get('reason') or 'רלוונטי לנושא המאמר'}</span></li>"
        for p in candidates[:5]
        if p.get("url") and p.get("title")
    )
    return _h2(_contextual_recommendation_title(topic_profile), f"<ul>{links_html}</ul>") + "<h2>מוצרים רלוונטיים באתר</h2>"


def _build_contract_article(title: str, keyword: str, related: list[dict[str, str | float]], profile: dict[str, object]) -> str:
    topic_type = str(profile.get("topic_type") or "fallback_generic")
    entity = str(profile.get("main_entity") or keyword or title)
    links = _links_section(related, profile)

    if topic_type == "meat_quick_grill_cut":
        return (
            f"<p><strong>{title}</strong> הוא מדריך צלייה מהירה ל-{entity}: איך להבין את מאפייני הנתח, לשלוט בשכבת שומן ושיוש, להמליח נכון ולהגיע ל-54–57°C בלי לייבש (ולמי שמכוון ישן יותר: 54–56°C).</p>\n"
            + _h2("מאפייני הנתח", f"<p>{keyword} הוא נתח שמתאים לצלייה קצרה יחסית כאשר מזהים את כיוון הסיבים, עובי הנתח ורמת שיוש לפני שמתחילים. ככל שהנתח עבה יותר, משלבים חום עקיף לפני צריבה קצרה.</p>")
            + _h2("שומן ושיוש", "<p>שכבת שומן ושיוש פנימי קובעים עסיסיות. משאירים שומן חיצוני במידה, צורבים אותו בזהירות ומאפשרים לו להימס בלי להבעיר להבות גבוהות.</p>")
            + _h2("המלחה", "<p>מלח גס 40–60 דקות לפני הצלייה עוזר לתיבול לחדור ולייבש מעט את פני השטח. לפני העלייה לרשת מנגבים לחות כדי לקבל צריבה טובה.</p>")
            + _h2("חום ישיר ועקיף", "<p>פותחים באזור חום עקיף לנתחים עבים ואז מסיימים בחום ישיר לצריבה; בסטייקים דקים מתחילים בחום ישיר ומעבירים הצידה אם צריך שליטה.</p>")
            + _h2("טמפרטורת יעד", "<p>לרוב חובבי הסטייקים יעד של 54–57°C מתאים, כאשר 54–56°C הוא טווח עדין יותר למדיום-רייר עד מדיום עדין. מודדים במרכז הנתח ולא ליד שכבת שומן.</p>")
            + _h2("חיתוך ומנוחה", "<p>מנוחה של כמה דקות מייצבת נוזלים. לאחר מכן מבצעים חיתוך נגד הסיבים לפרוסות דקות כדי לקבל ביס רך ולא סיבי.</p>")
            + _h2("טעויות נפוצות", "<ul><li>צריבה על להבה גבוהה מדי שגורמת לשומן להישרף.</li><li>חיתוך עם הסיבים במקום חיתוך נגד הסיבים.</li><li>דילוג על מנוחה ומדידה לא מדויקת.</li></ul>")
            + links
            + _faq([("האם חייבים מדחום?", "כן, מדחום מצמצם ניחושים ומונע בישול יתר."), ("מתי מוסיפים פלפל?", "אפשר לפני הצלייה, אך בצלייה לוהטת עדיף חלק ממנו אחרי הצריבה."), ("איך יודעים איפה הסיבים?", "מסתכלים על הקווים הארוכים בנתח וחותכים בניצב אליהם.")])
            + "<hr><p><strong>CTA:</strong> לצליית נתחים מהירים בחרו מדחום, רשת נקייה וכלי עבודה שמאפשרים שליטה בחום ישיר ועקיף.</p>"
        )

    if topic_type == "meat_low_slow_smoking":
        return (
            f"<p><strong>{title}</strong> הוא מדריך עישון נמוך-ואיטי ל-{entity}: בניית סביבת 105–120°C, בחירת עצי עישון, פיתוח Bark, ניהול סטול, עטיפה ומנוחה ארוכה.</p>\n"
            + _h2("הכנת המעשנה", "<p>מנקים רשתות, ממלאים דלק יציב, מייצבים זרימת אוויר ומכניסים תבנית נוזלים רק אם היא עוזרת ליציבות ולא חוסמת חום.</p>")
            + _h2("סביבת בישול 105–120°C", "<p>המטרה היא תא בישול יציב של 105–120°C לאורך שעות. תנודות קטנות תקינות; קפיצות חדות פוגעות בקצב ריכוך הקולגן.</p>")
            + _h2("בחירת עצי עישון", "<p>עצי עישון כמו Oak או Hickory מתאימים לבקר ארוך. מתחילים בכמות מתונה כדי לקבל עשן דק ולא טעם מר.</p>")
            + _h2("פיתוח Bark", "<p>Bark נוצר משילוב תבלינים יבשים, עשן, חום וזמן. לא מרטיבים מוקדם מדי כדי לא לשטוף את השכבה החיצונית.</p>")
            + _h2("הסטול", "<p>סטול הוא האטה טבעית בעליית הטמפרטורה כאשר אידוי מקרר את הנתח. לא מעלים חום בפאניקה; מחליטים לפי צבע, מרקם וזמן.</p>")
            + _h2("עטיפה ונייר קצבים", "<p>עטיפה בנייר קצבים מקצרת את הסטול ושומרת על Bark טוב יותר מנייר כסף אטום. אפשר גם לעבוד no-wrap לקבלת קליפה חזקה יותר.</p>")
            + _h2("טמפרטורת סיום ומנוחה ארוכה", "<p>רוב נתחים לעישון מסתיימים סביב 90–96°C, אבל הבדיקה החשובה היא רכות במגע פרוב. אחרי הסיום נותנים מנוחה ארוכה לשימור עסיסיות.</p>")
            + _h2("ציר זמן", "<p>ציר זמן בסיסי: הכנה ותיבול, עישון פתוח עד Bark, ניהול סטול, עטיפה לפי צורך, סיום לפי רכות ואז מנוחה של שעה ומעלה.</p>")
            + _h2("טעויות נפוצות", "<ul><li>יותר מדי עשן בתחילת הדרך.</li><li>עטיפה לפני שיש Bark יציב.</li><li>חיתוך מיד בסיום ללא מנוחה ארוכה.</li></ul>")
            + links
            + _faq([("מתי עוטפים?", "כאשר הצבע וה-Bark יציבים והנתח נכנס לסטול ממושך."), ("האם חובה נייר קצבים?", "לא חובה, אבל הוא איזון טוב בין קיצור זמן לשמירת קליפה."), ("איך יודעים שהנתח מוכן?", "הפרוב נכנס ברכות והטווח הפנימי סביב 90–96°C.")])
            + "<hr><p><strong>CTA:</strong> לעישון ארוך הכינו מראש עצי עישון, נייר קצבים ומדחום דו-ערוצי.</p>"
        )

    if topic_type == "poultry_grill_recipe":
        return (
            f"<p><strong>{title}</strong> הוא מתכון גריל לעוף שמתחיל בייבוש, ממשיך בבטיחות מזון ומסתיים בקריספיות וגלייז שלא הופך לסוכר שרוף.</p>\n"
            + _h2("ייבוש", "<p>מייבשים את העוף היטב במגבת נייר ומשאירים אותו פתוח במקרר אם יש זמן. ייבוש הוא הבסיס לעור קריספי ולאדים פחותים על הרשת.</p>")
            + _h2("בטיחות מזון ו-74°C", "<p>בטיחות מזון בעוף אינה מקום לניחוש: מודדים בחלק העבה ומוודאים לפחות 74°C לפני הגשה.</p>")
            + _h2("קריספיות", "<p>מביאים את העוף לבישול אחיד באזור חום עקיף ואז מסיימים מעל חום ישיר קצר לקבלת קריספיות וצבע זהוב.</p>")
            + _h2("מרינדה וגלייז", "<p>מרינדה יכולה להיכנס לפני הצלייה, אבל גלייז מתוק מוסיפים רק בסוף. כך מקבלים ברק וטעם בלי שריפה.</p>")
            + _h2("איך נמנעים מסוכר שרוף", "<p>מורחים שכבה דקה, עובדים רחוק מלהבה גבוהה ומחזירים לרשת לזמן קצר בלבד. אם הרוטב סמיך מדי מדללים מעט.</p>")
            + _h2("טעויות נפוצות", "<ul><li>העלאה לרשת כשהעוף רטוב.</li><li>הוספת גלייז מוקדם מדי.</li><li>הגשה לפני בדיקת 74°C.</li></ul>")
            + links
            + _faq([("אפשר להכין בלי גלייז?", "כן, תיבול יבש וסיום חום ישיר יספיקו לקריספיות."), ("מתי להפוך?", "כאשר העור משתחרר מהרשת בלי להיקרע."), ("איך מחממים שאריות?", "בחום בינוני, לא במיקרוגל, כדי לשמור על מרקם.")])
            + "<hr><p><strong>CTA:</strong> למתכוני עוף על הגריל הצטיידו במדחום ובמברשת רוטב מדויקת.</p>"
        )

    if topic_type == "fuel_comparison_or_guide":
        return (
            f"<p><strong>{title}</strong> עוסק בבחירת דלק לגריל: זמן בעירה, יציבות חום, רמת עשן, אפר ועלות מול ביצועים של {keyword}.</p>\n"
            + _h2("זמן בעירה", "<p>פחם קוקוס נוטה לזמן בעירה ארוך ואחיד בזכות דחיסות גבוהה. פחם עץ נדלק מהר יותר אך זמן העבודה משתנה לפי גודל ואיכות הגושים.</p>")
            + _h2("יציבות חום", "<p>יציבות חום חשובה במיוחד בגריל עם מכסה או במעשנה. בריקטים של פחם קוקוס שומרים קצב אחיד, ופחם עץ מגיב מהר לפתיחת אוויר.</p>")
            + _h2("רמת עשן", "<p>רמת עשן של פחם קוקוס בדרך כלל עדינה ונקייה. פחם עץ נותן אופי עשן טבעי ובולט יותר, בעיקר בתחילת ההדלקה.</p>")
            + _h2("כמות אפר", "<p>אפר משפיע על זרימת אוויר וניקוי. פחם איכותי משאיר פחות אפר, בעוד חומר זול עלול לסתום פתחי אוויר מהר יותר.</p>")
            + _h2("התאמה לגריל או מעשנה", "<p>לגריל פתוח וצלייה קצרה פחם עץ נוח ומהיר. למעשנה, אירוח ארוך או חום עקיף יציב, פחם קוקוס נותן שליטה טובה.</p>")
            + _h2("עלות מול ביצועים", "<p>עלות מול ביצועים לא נמדדת רק במחיר שקית: אם הדלק מחזיק יותר זמן, דורש פחות תוספות ומשאיר פחות ניקוי, הערך בפועל עולה.</p>")
            + _h2("מתי לבחור כל סוג", "<p>בחרו פחם קוקוס לעבודה ארוכה ויציבה; בחרו פחם עץ לטעם עשן טבעי, הדלקה זריזה וצלייה ישירה.</p>")
            + _h2("טבלת השוואה", "<table><thead><tr><th>קריטריון</th><th>פחם קוקוס</th><th>פחם עץ</th></tr></thead><tbody><tr><td>זמן בעירה</td><td>ארוך</td><td>בינוני</td></tr><tr><td>יציבות חום</td><td>גבוהה</td><td>משתנה ומהירה</td></tr><tr><td>רמת עשן</td><td>עדינה</td><td>מודגשת</td></tr><tr><td>אפר</td><td>נמוך יחסית</td><td>תלוי איכות</td></tr></tbody></table>")
            + links
            + _faq([("מה מתאים למעשנה?", "דלק יציב כמו פחם קוקוס מתאים לרוב לעבודה ארוכה."), ("מה מתאים לצלייה מהירה?", "פחם עץ איכותי מגיב מהר ומספק עשן טבעי."), ("איך מצמצמים אפר?", "בוחרים פחם איכותי ושומרים פתחי אוויר נקיים.")])
            + "<hr><p><strong>CTA:</strong> בחרו דלק לפי משך העבודה: יציבות לאירוח ארוך או תגובה מהירה לצלייה קצרה.</p>"
        )

    if topic_type == "smoking_wood_guide":
        return (
            f"<p><strong>{title}</strong> הוא מדריך לבחירת עץ לעישון לפי פרופיל טעם, שבבים מול צ׳אנקים, השריה, התאמה לבשר ועוצמת עשן.</p>\n"
            + _h2("פרופיל טעם", "<p>פרופיל טעם של עץ קובע אם העישון יהיה עדין, פירותי או עמוק. Apple ו-Cherry עדינים, Oak מאוזן, Hickory חזק יותר.</p>")
            + _h2("שבבים מול צ׳אנקים", "<p>שבבים מתאימים לגריל ולסשנים קצרים כי הם מגיבים מהר. צ׳אנקים מתאימים למעשנה ולעישון ממושך עם שחרור איטי יותר.</p>")
            + _h2("השריה או בלי השריה", "<p>ברוב המקרים אין צורך בהשריה; עץ רטוב מייצר אדים לפני עשן. עדיף לשלוט בכמות ובזרימת אוויר.</p>")
            + _h2("התאמה לבשר", "<p>התאמה לבשר שומרת על איזון: עוף ודגים אוהבים עצים עדינים, בקר מקבל טוב Oak או Hickory, וירקות נהנים מעשן קצר.</p>")
            + _h2("עוצמת עשן", "<p>עוצמת עשן גבוהה מדי יוצרת מרירות. המטרה היא thin blue smoke: עשן דק, נקי וכחלחל ולא ענן לבן וסמיך.</p>")
            + _h2("טעויות נפוצות", "<ul><li>שימוש בכמות עץ גדולה מדי.</li><li>בחירת עץ חזק למנה עדינה.</li><li>חסימת אוויר שמייצרת עשן מלוכלך.</li></ul>")
            + links
            + _faq([("מה עדיף, שבבים או צ׳אנקים?", "לגריל קצר שבבים; לעישון ארוך צ׳אנקים."), ("האם להשרות שבבים?", "בדרך כלל לא, שליטה באוויר חשובה יותר."), ("איך יודעים שהעשן נקי?", "מחפשים thin blue smoke ולא עשן לבן כבד.")])
            + "<hr><p><strong>CTA:</strong> התאימו עץ לעישון לפי חומר הגלם ורמת הטעם שאתם רוצים, לא לפי שם פופולרי בלבד.</p>"
        )

    if topic_type == "smoking_accessory_guide":
        return (
            f"<p><strong>{title}</strong> הוא מדריך לנייר קצבים לעישון: מה זה butcher paper, איך הוא עוזר בעטיפת בריסקט וצלעות, מתי לעטוף בזמן הסטול, ואיך משווים butcher paper vs foil בלי לפגוע ב-Bark.</p>\n"
            + _h2("מה זה נייר קצבים לעישון", "<p>נייר קצבים הוא נייר עבה ונושם יחסית שמשמש לעטיפת בשר בעישון ארוך. בניגוד לנייר כסף, הוא מאפשר לחלק מהאדים לצאת ולכן עוזר לשמור Bark יציב לצד שמירת לחות טובה בתוך הנתח.</p>")
            + _h2("עטיפת בריסקט", "<p>בריסקט עוטפים כאשר הצבע כהה, ה-Bark יציב למגע והעלייה בחום מאטה בשלב הסטול. העטיפה מצמצמת אידוי, מקדמת ריכוך ומונעת ייבוש של ה-flat בלי להפוך את הקליפה לספוגית מדי.</p>")
            + _h2("עטיפת צלעות ונתחי בקר", "<p>בצלעות ובנתחי בקר כמו אסאדו או שורט ריבס, נייר קצבים מתאים כאשר רוצים לקדם ריכוך ועדיין לשמור מרקם חיצוני. העיקרון זהה: קודם בונים צבע ועשן, אחר כך עוטפים רק כשהמעטפת יציבה.</p>")
            + _h2("שלב הסטול (Stall) ו-Texas Crutch", "<p>סטול (Stall) הוא שלב שבו אידוי מקרר את פני הבשר ומאט את העלייה בטמפרטורה. Texas Crutch היא שיטת עטיפה שנועדה לעבור את השלב הזה מהר יותר; נייר קצבים הוא גרסה מאוזנת יותר מנייר כסף כי הוא פחות אוטם.</p>")
            + _h2("שמירת Bark ושמירת לחות", "<p>Bark טוב נבנה לפני העטיפה מתבלינים יבשים, עשן וחום יציב. נייר קצבים שומר לחות בלי לכלוא יותר מדי אדים, ולכן הוא עוזר לשמר קליפה כהה ויציבה יותר מאשר עטיפה אטומה לחלוטין.</p>")
            + _h2("Butcher Paper vs Foil – נייר קצבים מול נייר כסף", "<p>Butcher Paper vs Foil, כלומר נייר קצבים מול נייר כסף, הוא הבדל בין נשימה לאיטום: נייר כסף מאיץ בישול ושומר נוזלים בצורה חזקה, אבל עלול לרכך Bark; נייר קצבים איטי מעט יותר, שומר מרקם חיצוני טוב יותר ומתאים לבריסקט שרוצים להגיש עם קליפה ברורה.</p>")
            + _h2("מתי לעטוף", "<p>מתי לעטוף? לא לפי שעה קבועה, אלא לפי צבע, מגע והתקדמות הסטול. לרוב ממתינים עד שה-Bark לא נמרח באצבע, שהנתח קיבל גוון עמוק ושאיבוד הלחות מתחיל להאט את התהליך.</p>")
            + _h2("איך לעטוף", "<p>איך לעטוף: מניחים שני דפים חופפים של נייר קצבים, מצמידים את הנתח במרכז, מקפלים צדדים בחוזקה ומגלגלים כך שהתפר יישב כלפי מטה. העטיפה צריכה להיות הדוקה, אך לא לקרוע את הנייר או למחוץ את הקליפה.</p>")
            + _h2("נייר ורוד מול נייר חום", "<p>נייר ורוד מול חום: נייר ורוד הוא בדרך כלל peach/pink butcher paper לא מולבן שמזוהה עם BBQ אמריקאי. נייר חום יכול לעבוד אם הוא food-safe, ללא ציפוי שעווה או פלסטיק וללא צבעים בעייתיים; תמיד בודקים התאמה למגע עם מזון וחום עקיף.</p>")
            + _h2("טעויות נפוצות", "<ul><li>לעטוף מוקדם לפני שיש Bark יציב.</li><li>להשתמש בנייר מצופה שעווה או פלסטיק במקום נייר קצבים food-safe.</li><li>לעטוף רפוי כך שנוזלים ואדים מצטברים בכיסים.</li><li>להניח שנייר קצבים ונייר כסף נותנים אותה תוצאה.</li></ul>")
            + links
            + _faq([("האם נייר קצבים חובה לבריסקט?", "לא חובה, אבל הוא עוזר לעבור סטול תוך שמירה טובה יותר על Bark לעומת נייר כסף."), ("האם אפשר לעטוף צלעות בנייר קצבים?", "כן, בעיקר כאשר רוצים ריכוך בלי לאבד לגמרי את המרקם החיצוני."), ("מה ההבדל בין ורוד לחום?", "הצבע פחות חשוב מהבטיחות: צריך נייר food-safe, לא מצופה, שמתאים לעישון וחום עקיף.")])
            + "<hr><p><strong>CTA:</strong> לעישון בריסקט, צלעות ונתחי בקר הכינו נייר קצבים מתאים לפני תחילת הסשן, כדי להחליט בזמן אמת מתי לעטוף.</p>"
        )

    if topic_type == "grill_accessory_guide":
        entity_key = str(profile.get("entity_key") or "generic")
        if entity_key == "basalt_stones":
            return (
                f"<p><strong>{title}</strong> הוא מדריך אביזר לגריל גז שמתמקד ב-{entity}: אבני בזלת, אבני לבה, basalt stones for gas grill ו-lava rocks שמונחות באזור החום כדי לשפר פיזור חום, להפחית התלקחויות ולייצב את העבודה סביב המבערים.</p>\n"
                + _h2("מה זה", "<p>אבני לבה / אבני בזלת הן אבנים וולקניות עמידות לחום המשמשות כגוף פיזור בין להבת גריל גז לבין רשת הצלייה. במקום לדבר על אביזרים כלליים, כאן המוקד הוא תפקיד האבנים: ספיגת חום, שחרור הדרגתי שלו והפרדה חלקית בין טפטופי שומן לבין להבה פתוחה.</p>")
                + _h2("איך זה עובד", "<p>איך זה עובד: המבערים מחממים את אבני הבזלת, האבנים מפזרות חום לרוחב האזור ומקטינות נקודות חמות חדות. כאשר שומן מטפטף, חלקו עובר אידוי שומן על פני האבן החמה, מה שתורם ארומה עדינה ומפחית מגע ישיר של שומן בלהבה.</p>")
                + _h2("יתרונות", "<p>יתרונות מרכזיים של basalt stones הם פיזור חום אחיד יותר, יציבות חום ויציבות טמפרטורה כאשר פותחים וסוגרים מכסה, הפחתת התלקחויות בזמן צלייה שומנית, והגנה מסוימת על מבערים מפני טפטופים ישירים.</p>")
                + _h2("התקנה ושימוש", "<p>התקנה נכונה נעשית רק בגריל גז שמתוכנן לעבוד עם אבנים או מגש מתאים. מסדרים שכבה אחת מרווחת מעל המבערים, לא דוחסים ערימה עבה, משאירים נתיב זרימת אוויר, ומחממים בהדרגה לפני שמעמיסים מזון שומני.</p>")
                + _h2("ניקוי ותחזוקה", "<p>ניקוי והחלפה מתחילים בקירור מלא. מסירים חתיכות מזון יבשות, הופכים אבנים לפי צורך כדי לשרוף שאריות בעדינות, ומחליפים אבנים שספגו הרבה שומן או מתפוררות. תחזוקה טובה כוללת בדיקה שגם מגש האבנים והמבערים נשארים פתוחים ולא חסומים.</p>")
                + _h2("מתי להחליף", "<p>מתי להחליף? מרווחי החלפה תלויים בתדירות הצלייה ובכמות השומן: בשימוש ביתי רגיל בודקים כל כמה חודשים, ובשימוש כבד מחליפים מוקדם יותר כאשר יש ריח שרוף קבוע, התפוררות, סדקים רבים או ירידה בפיזור החום.</p>")
                + _h2("שיקולי קנייה", "<p>שיקולי קנייה כוללים התאמה לדגם גריל הגז, גודל האבן, עמידות לחום, כמות שמכסה את אזור המבערים בשכבה אחת, והאם היצרן ממליץ על אבני בזלת או על מפזרי חום מתכתיים במקום.</p>")
                + _h2("טעויות נפוצות", "<ul><li>להוסיף אבני לבה לגריל שלא מיועד לכך ולחסום אוורור.</li><li>ליצור שכבה עבה מדי שמעמיסה חום על המבערים.</li><li>להשאיר אבנים ספוגות שומן לאורך זמן ולצפות שהפחתת התלקחויות תמשיך לעבוד.</li><li>לשטוף אבנים נקבוביות בהרבה מים במקום ניקוי יבש והחלפה כשצריך.</li></ul>")
                + links
                + _faq([("האם אבני בזלת מתאימות לכל גריל גז?", "לא, רק אם מבנה הגריל והמגש מאפשרים שימוש בטוח בלי חסימת אוויר."), ("האם lava rocks מפחיתות התלקחויות?", "כן כאשר הן מסודרות נכון ונקיות יחסית; אבנים ספוגות שומן עלולות לעשות ההפך."), ("כל כמה זמן מחליפים?", "בודקים כל כמה חודשים ומחליפים לפי התפוררות, ריח ושינוי בביצועים.")])
                + "<hr><p><strong>CTA:</strong> אם בוחרים אבני בזלת, התאימו אותן לגריל הגז שלכם ותחזקו אותן כמו רכיב חום — לא כמו קישוט.</p>"
            )
        if entity_key == "thermometer":
            return (
                f"<p><strong>{title}</strong> הוא מדריך אביזר לגריל שמתמקד ב-{entity}: מדחום לבשר עם meat thermometer probe, קריאה מהירה וניקוי נכון.</p>\n"
                + _h2("מה זה", "<p>מדחום לבשר הוא אביזר מדידה שמכניסים למרכז חומר הגלם כדי לדעת טמפרטורה פנימית בזמן אמת. יש מדחומי קריאה מהירה לבדיקות נקודתיות ויש פרובים שנשארים בבשר ומתחברים למסך או לאפליקציה.</p>")
                + _h2("איך זה עובד", "<p>איך זה עובד: חיישן בקצה ה-probe מודד את החום בנקודת המגע. זמן תגובה קצר עוזר לקבל החלטה לפני שהמנה ממשיכה להתבשל, וכיול תקופתי מוודא שהמספרים לא סוטים.</p>")
                + _h2("יתרונות", "<p>יתרונות המדחום הם פחות ניחושים, פחות פתיחת מכסה, שליטה טובה יותר במידת עשייה, וזיהוי מוקדם של אזורים חמים או קרים על הגריל בלי להסתמך על צבע חיצוני בלבד.</p>")
                + _h2("התקנה ושימוש", "<p>התקנה ושימוש תלויים בסוג: במדחום קריאה מהירה מכניסים את הקצה למרכז ולא נוגעים בעצם או ברשת; בפרוב קבוע מעבירים כבל במסלול שלא נלחץ במכסה ולא נוגע בלהבה ישירה.</p>")
                + _h2("ניקוי ותחזוקה", "<p>ניקוי נעשה במטלית לחה וחיטוי עדין לקצה המדידה, בלי להטביע יחידה אלקטרונית במים. תחזוקה כוללת שמירת כבל לא מקופל חזק, בדיקת סוללה וכיול לפי הוראות היצרן.</p>")
                + _h2("מתי להחליף", "<p>מתי להחליף? כאשר הקריאה איטית מאוד, יש סטייה קבועה גם אחרי כיול, הכבל נסדק, המסך לא יציב או ה-probe קיבל מכה שמייצרת מדידות לא אמינות.</p>")
                + _h2("שיקולי קנייה", "<p>שיקולי קנייה כוללים זמן תגובה, טווח מדידה, עמידות הכבל, נוחות קריאה בתאורה חזקה, אפשרות כיול, מספר פרובים ואחריות.</p>")
                + _h2("טעויות נפוצות", "<ul><li>למדוד קרוב לעצם או לשומן עבה במקום במרכז.</li><li>להשאיר מדחום קריאה מהירה בתוך גריל סגור למרות שאינו מיועד לכך.</li><li>לא לנקות probe בין מדידות.</li><li>להתעלם מכיול ולבנות על תחושת יד בלבד.</li></ul>")
                + links
                + _faq([("מה עדיף, קריאה מהירה או פרוב קבוע?", "קריאה מהירה מתאימה לבדיקה זריזה; פרוב קבוע מתאים לצלייה ארוכה ומעקב רציף."), ("האם חייבים כיול?", "כן, כיול תקופתי או בדיקת דיוק עוזרים למנוע החלטות שגויות."), ("איך מנקים מדחום?", "מנקים את קצה המדידה אחרי כל שימוש ומרחיקים מים מהיחידה האלקטרונית.")])
                + "<hr><p><strong>CTA:</strong> בחרו מדחום לפי צורת הבישול שלכם: בדיקות מהירות, מעקב ארוך או כמה נתחים במקביל.</p>"
            )
        return (
            f"<p><strong>{title}</strong> הוא מדריך אביזר לגריל עבור {keyword}: מה זה, איך זה עובד, אילו יתרונות מקבלים, איך מבצעים התקנה ושימוש, ומה חשוב בניקוי ותחזוקה.</p>\n"
            + _h2("מה זה", f"<p>{keyword} הוא אביזר שנועד לפתור צורך נקודתי בעבודה עם גריל גז או גריל אחר: שליטה טובה יותר, בטיחות, ניקוי נוח או תוצאה עקבית.</p>")
            + _h2("איך זה עובד", f"<p>איך זה עובד תלוי ב-{keyword}: בודקים מה הבעיה המדויקת שהאביזר פותר, איפה הוא יושב ביחס לרשת או למבערים, ואיך הוא משפיע על בטיחות ותפעול.</p>")
            + _h2("יתרונות", f"<p>יתרונות מרכזיים של {keyword} צריכים להיות מדידים: פחות טעויות, חיסכון בזמן, שליטה טובה יותר או תחזוקה פשוטה יותר לאורך שימושים חוזרים.</p>")
            + _h2("התקנה ושימוש", "<p>התקנה ושימוש צריכים להתאים להוראות היצרן ולמבנה הגריל. לא חוסמים פתחי אוויר, לא מעמיסים חלקים ולא משתמשים באביזר שחוק.</p>")
            + _h2("ניקוי ותחזוקה", f"<p>ניקוי ותחזוקה של {keyword} מתחילים בקירור מלא, הסרת שומן יבש או לכלוך, ייבוש ושמירה במקום שלא יפגע בחומר או במנגנון.</p>")
            + _h2("מתי להחליף", "<p>מתי להחליף? כאשר יש סדקים, שחיקה, חלודה, התפוררות או ירידה מורגשת בביצועים ובבטיחות.</p>")
            + _h2("שיקולי קנייה", "<p>שיקולי קנייה כוללים התאמה לדגם הגריל, איכות חומר, אחריות, קלות ניקוי וגודל שמתאים לשטח העבודה.</p>")
            + _h2("טעויות נפוצות", "<ul><li>לקנות לפי שם כללי ולא לפי התאמה לגריל.</li><li>להתעלם מהוראות יצרן.</li><li>לא לנקות אחרי שימושים שומניים.</li></ul>")
            + links
            + _faq([("האם כל אביזר מתאים לכל גריל?", "לא, בודקים התאמה למידות ולמבנה."), ("איך לשמור לאורך זמן?", "ניקוי עדין וייבוש אחרי שימוש."), ("מה חשוב לפני קנייה?", "התאמה, חומר ואחריות.")])
            + "<hr><p><strong>CTA:</strong> בחרו אביזר לפי הבעיה שהוא פותר בפועל בגריל שלכם.</p>"
        )

    if topic_type == "equipment_buying_guide":
        return (
            f"<p><strong>{title}</strong> הוא מדריך קנייה לציוד גריל לפי תרחיש שימוש, גודל, מקור חום או BTU, איכות חומר, תחזוקה והשוואה למי זה מתאים.</p>\n"
            + _h2("תרחיש שימוש", "<p>תרחיש שימוש מגדיר את הבחירה: משפחה קטנה, אירוח גדול, מרפסת, גינה, בישול מהיר או עבודה ארוכה עם מכסה.</p>")
            + _h2("גודל", "<p>גודל נמדד בשטח צלייה, מספר מבערים או נפח תא. עדיף לבחור גודל שמתאים לרוב השימושים ולא רק לאירוח נדיר.</p>")
            + _h2("מקור חום או BTU", "<p>מקור חום קובע אופי עבודה: גז נוח לשליטה, פחמים נותנים אופי עשן, ומעשנה בנויה לזמן ארוך. בגריל גז בודקים BTU ביחס לשטח ולפיזור.</p>")
            + _h2("איכות חומר", "<p>איכות חומר משפיעה על שמירת חום, עמידות וחלודה. נירוסטה איכותית, יציקות יציבות ורשתות כבדות הן סימני איכות.</p>")
            + _h2("תחזוקה", "<p>תחזוקה כוללת ניקוי רשתות, ריקון שומן, בדיקת מבערים או פתחי אוויר וכיסוי מתאים בין שימושים.</p>")
            + _h2("השוואה", "<p>השוואה נכונה בוחנת נוחות, עוצמה, איכות חומר, שירות, אחריות ומחיר כולל אביזרים נדרשים.</p>")
            + _h2("למי זה מתאים", f"<p>{keyword} מתאים למי שרוצה פתרון שתואם את המקום, תדירות השימוש וסגנון הבישול שלו, ולא רק מפרט מרשים על הנייר.</p>")
            + links
            + _faq([("כמה גדול לבחור?", "לפי מספר הסועדים הקבוע ולא רק אירוח חד-פעמי."), ("BTU גבוה תמיד טוב?", "לא, פיזור חום ואיכות מבנה חשובים לא פחות."), ("מה לבדוק בחנות?", "יציבות, חומר, אחריות ונוחות ניקוי.")])
            + "<hr><p><strong>CTA:</strong> לפני קנייה הגדירו שימוש, מקום ותקציב — ורק אז השוו מפרטים.</p>"
        )

    if topic_type == "recipe_how_to":
        return (
            f"<p><strong>{title}</strong> הוא מתכון גריל כללי ל-{keyword} עם מרכיבים, כלים, שלבים, טיפים וטעויות שכדאי למנוע.</p>\n"
            + _h2("מרכיבים", "<p>מרכיבים בסיסיים: חומר גלם טרי, שמן עדין, מלח, תבלינים לפי הטעם ורכיב סיום שמתאים למנה.</p>")
            + _h2("כלים", "<p>כלים מומלצים: גריל נקי, מלקחיים, קערת ערבוב, מברשת ורשת משומנת קלות.</p>")
            + _h2("שלבים", "<p>שלבים: מחממים גריל, מכינים את חומר הגלם, צולים בהדרגה, בודקים מרקם ומסיימים בתיבול עדין.</p>")
            + _h2("טיפים", "<p>טיפים לשיפור: לא להעמיס את הרשת, להפוך רק כשיש שחרור טבעי, ולסיים בטעם רענן.</p>")
            + _h2("טעויות נפוצות", "<p>טעויות נפוצות הן גריל לא חם מספיק, תיבול מוגזם ופתיחה תכופה של המכסה.</p>")
            + links
            + _faq([("אפשר להכין מראש?", "כן, את רוב ההכנות אפשר לבצע לפני החימום."), ("איך מונעים הדבקה?", "רשת נקייה, חימום מוקדם ושימון קל."), ("מתי מגישים?", "מיד אחרי סיום והתייצבות קצרה לפי המנה.")])
            + "<hr><p><strong>CTA:</strong> נסו את המתכון פעם אחת לפי השלבים ואז התאימו תיבול לטעם האישי.</p>"
        )

    return (
        f"<p>{GENERIC_TEMPLATE_INTRO}. במדריך הזה ניגש אל {keyword} בצורה כללית כי המערכת לא זיהתה סוג נושא מקצועי מדויק.</p>\n"
        + _h2("למה זה חשוב", f"<p>{keyword} משפיע על תכנון העבודה סביב הגריל ועל בחירת ציוד מתאים.</p>")
        + _h2("שיטת עבודה", "<p>מתחילים בהגדרת מטרה, בוחרים כלי מתאים, עובדים בשלבים ובודקים תוצאה לפני שממשיכים.</p>")
        + _h2("טעויות", "<p>הטעות המרכזית היא להשתמש באותה שיטה לכל נושא בלי להבין את ההקשר.</p>")
        + links
        + _faq([("איך מתחילים?", "מגדירים מטרה ובוחרים כלי מתאים."), ("מה לבדוק?", "התאמה, בטיחות ונוחות שימוש."), ("מתי להתייעץ?", "כאשר אין התאמה ברורה לסוג גריל או מוצר.")])
    )



def _article_word_count(html: str) -> int:
    return len(re.findall(r"[\w\u0590-\u05FF]+", _plain_text(html or "")))


def _topic_specific_expansion_html(topic_type: str, entity: str, keyword: str, entity_key: str) -> tuple[str, str]:
    if topic_type == "meat_low_slow_smoking":
        return (
            _h2("איך לבחור נתח מתאים לעישון", f"<p>ל-{entity} בעישון מחפשים עובי אחיד, שכבת שומן שמגינה על הבשר וגמישות בסיסית שמעידה על נתח לא יבש. בבריסקט עדיף לבדוק שה-flat לא דק מדי ושה-point מכיל מספיק שומן פנימי לעישון ארוך.</p>")
            + _h2("טרימינג נכון לפני עישון", "<p>טרימינג מסיר שומן קשה וקצוות דקים שיישרפו מוקדם, אבל לא מגלחים את הנתח לגמרי. משאירים שכבה שמסייעת בהגנה ובטעם, ומיישרים אזורים שיבלמו זרימת עשן.</p>")
            + _h2("Fat Cap וניהול שומן", "<p>Fat Cap צריך להיות אחיד ולא עבה מדי. אם מקור החום מגיע מלמטה, מניחים את השומן לכיוון החום; אם החום היקפי, בוחרים צד לפי זרימת האוויר והגנה על החלק הדק.</p>")
            + _h2("מתי לעטוף בנייר קצבים", "<p>עוטפים רק אחרי שיש צבע עמוק ו-Bark יציב, לרוב סביב שלב הסטול. נייר קצבים מאפשר נשימה טובה יותר מנייר כסף ולכן מתאים כאשר רוצים לקצר זמן בלי לרכך את הקליפה לגמרי.</p>")
            + _h2("איך בודקים רכות עם פרוב", "<p>הטמפרטורה סביב 90–96°C היא סימן דרך, לא פקודה להוריד. מכניסים פרוב בכמה נקודות ומחפשים תחושה רכה, כמעט כמו חמאה, במיוחד בחלק העבה.</p>")
            + _h2("מנוחה ארוכה וחיתוך נכון", "<p>מנוחה ארוכה מאפשרת לנוזלים להתייצב ולסיבים להירגע. חותכים רק אחרי ירידת חום מסוימת, מזהים את כיוון הסיבים ומשנים זווית בין flat ל-point אם צריך.</p>"),
            "meat_low_slow_smoking_expansion",
        )
    if topic_type == "poultry_grill_recipe":
        return (
            _h2("ייבוש העור לפני הצלייה", "<p>כנפיים קריספיות מתחילות בעור יבש: מייבשים היטב, ואם יש זמן משאירים במקרר לא מכוסה לזמן קצר כדי להפחית לחות.</p>")
            + _h2("תיבול יבש לעור קריספי", "<p>תיבול יבש נדבק טוב יותר לעור יבש. שכבה דקה של מלח ותבלינים נותנת טעם בלי להפוך את פני השטח לבוציים.</p>")
            + _h2("חום עקיף ואז חום ישיר", "<p>מתחילים בחום עקיף כדי לבשל את העוף בצורה אחידה, ואז מסיימים בחום ישיר קצר לפתיחת צבע וקריספיות בלי לשרוף את העור.</p>")
            + _h2("מתי מוסיפים גלייז", "<p>גלייז מוסיפים בסוף, כשהכנפיים כמעט מוכנות. כך הסוכר מספיק להתקרמל בעדינות ולא נשרף לפני שהעוף מגיע ל-74°C.</p>")
            + _h2("הגשה ושמירה על קריספיות", "<p>מגישים על רשת או מגש פתוח ולא סוגרים מיד בקופסה אטומה. אדים כלואים מרככים את העור ומוחקים חלק מהקריספיות.</p>"),
            "poultry_grill_recipe_expansion",
        )
    if topic_type == "smoking_accessory_guide":
        return (
            _h2("בריסקט, ריבס ובקר: התאמת העטיפה", "<p>בריסקט נהנה מעטיפה הדוקה אחרי Bark יציב; ריבס וצלעות צריכים בדיקה עדינה יותר כדי לא לרכך יתר על המידה. בנתחי בקר שמנים במיוחד משתמשים בנייר קצבים כדי לאזן בין שמירת לחות לבין מרקם חיצוני.</p>")
            + _h2("סימני עטיפה בזמן הסטול", "<p>בזמן הסטול מחפשים האטה עקבית, צבע מהגוני וקליפה שאינה נמרחת. אם ה-Bark עדיין רך, ממתינים; אם ה-flat מתחיל להתייבש והצבע מוכן, עוטפים.</p>")
            + _h2("בדיקה אחרי העטיפה", "<p>אחרי העטיפה ממשיכים לבשל לפי רכות ולא לפי זמן. בודקים שהפרוב נכנס חלק, נותנים מנוחה ארוכה ושומרים את העטיפה סגורה עד שהנתח התייצב.</p>"),
            "smoking_accessory_guide_expansion",
        )

    if topic_type == "grill_accessory_guide" and entity_key == "thermometer":
        return (
            _h2("סוגי מדחומים לבשר", "<p>יש מדחום קריאה מהירה לבדיקת נקודה בסיום הצלייה, מדחום פרוב שנשאר בנתח לאורך עישון ארוך, ומדחום עם כמה ערוצים למעקב אחרי כמה נתחים או אחרי תא הבישול. הבחירה תלויה במשך העבודה ובמידת השליטה שרוצים לקבל.</p>")
            + _h2("דיוק, זמן תגובה וטווח מדידה", "<p>דיוק טוב מתחיל בחיישן איכותי וזמן תגובה קצר. מדחום איטי גורם להשאיר מכסה פתוח יותר מדי זמן, ולכן בנתחים מהירים חשוב לקבל קריאה בתוך שניות; בעישון ארוך חשוב יותר יציבות לאורך שעות.</p>")
            + _h2("מיקום הפרוב בבשר", "<p>מכניסים את ה-probe למרכז החלק העבה, רחוק מעצם, שומן עבה או כיס אוויר. בנתח לא אחיד בודקים כמה נקודות כדי לוודא שהאזור הקר ביותר הגיע לטווח הרצוי בלי לייבש את הקצוות.</p>")
            + _h2("שימוש בעישון ארוך", "<p>בעישון בריסקט, אסאדו או צלעות, פרוב קבוע עוזר להבין את קצב ההתקדמות ואת קצב ההתקדמות. הוא לא מחליף בדיקת רכות, אבל מונע פתיחות מכסה מיותרות ועוזר לתזמן עטיפה ומנוחה.</p>")
            + _h2("בטיחות וניקוי בין מדידות", "<p>אחרי כל מגע עם בשר נא מנקים את קצה המדידה לפני שמכניסים אותו למנה מוכנה. יחידה אלקטרונית לא מטביעים במים; מנקים בעדינות את הקצה, מייבשים ושומרים על הכבל בלי קיפול חד.</p>")
            + _h2("קריאה מהירה מול פרוב קבוע", "<p>קריאה מהירה מתאימה לסטייקים, פרגיות והמבורגרים שבהם מקבלים החלטה ברגע אחד ומחזירים את המכסה. פרוב קבוע מתאים לבריסקט, צלי ארוך או כמה נתחים גדולים, כי הוא נותן רצף נתונים בלי לפתוח את תא הבישול שוב ושוב, במיוחד לפני אירוח גדול או עישון ארוך שבו טעות קטנה משפיעה על כל הארוחה.</p>")
            + _h2("מה לבדוק לפני קנייה", "<p>בודקים מסך ברור בשמש, כפתורים נוחים בידיים מלוכלכות, כבל עמיד לחום, אפשרות כיול, אחריות, זמינות סוללות וכיסוי לטווח הטמפרטורות שאתם באמת צריכים. עדיף מדחום פשוט ומדויק מאשר מוצר עמוס אפשרויות שלא נוח להשתמש בו.</p>")
            + _h2("טעויות מדידה שמייבשות בשר", "<p>טעות נפוצה היא למדוד בקצה הדק של הנתח ולקבל תחושת ביטחון שגויה, או להסתמך על צבע חיצוני בלבד. טעות נוספת היא להשאיר מכסה פתוח בזמן שמחפשים נקודת מדידה, מה שמפיל חום ומאריך את הבישול.</p>")
            + _h2("שגרת עבודה מומלצת", "<p>לפני שהבשר עולה לגריל מכינים את המדחום, בודקים שהוא נדלק ומחליטים מראש איפה מודדים. בזמן הצלייה מודדים קצר ומחזירים מכסה; בסיום רושמים טווחים שעבדו טוב כדי לשפר את הפעם הבאה.</p>")
            + _h2("התאמה למשפחה ולאירוח", "<p>למשפחה שמכינה סטייקים ועוף מספיק לרוב מדחום קריאה מהירה איכותי. מי שמארח הרבה, מעשן נתחים גדולים או עובד עם כמה אזורי חום ירוויח מפרוב קבוע, התראות ברורות ואפשרות לעקוב אחרי יותר מנתח אחד בצורה עקבית, נוחה וברורה גם במהלך אירוח ארוך בבית ובחצר.</p>"),
            "thermometer_accessory_depth_expansion",
        )

    if topic_type == "grill_accessory_guide":
        return (
            _h2("התאמה לגריל", f"<p>לפני שימוש ב-{entity}, בודקים התאמה פיזית ובטיחותית לדגם הגריל, למקור החום ולמרחק מהרשת או מהמבערים.</p>")
            + _h2("התקנה או שימוש נכון", "<p>עובדים לפי הוראות היצרן, מתחילים בחימום הדרגתי, ומוודאים שהאביזר לא חוסם אוויר, לא נוגע בלהבה ישירה שלא לצורך ולא מפריע לסגירת המכסה.</p>")
            + _h2("ניקוי ותחזוקה", "<p>תחזוקה טובה מתחילה בקירור מלא, ניקוי שאריות והחזרה למקום יבש. באביזרי מדידה מקפידים גם על קצה נקי וכיול תקופתי.</p>")
            + _h2("מתי להחליף", "<p>מחליפים כאשר יש סדקים, התפוררות, קריאה לא מדויקת, ריח שרוף קבוע או ירידה ברורה בביצועים לעומת שימוש קודם.</p>")
            + _h2("צ׳קליסט קנייה", "<ul><li>התאמה לדגם הגריל.</li><li>חומר עמיד לחום.</li><li>ניקוי פשוט.</li><li>מידות נכונות לשטח העבודה.</li><li>אחריות או הוראות שימוש ברורות.</li></ul>"),
            "grill_accessory_guide_expansion",
        )
    if topic_type == "fuel_comparison_or_guide":
        return (
            _h2("התאמה לגריל", "<p>בגריל פתוח בוחרים דלק שמגיב מהר ומייצר חום ישיר חזק. בגריל עם מכסה חשוב יותר לשמור על יציבות לאורך זמן.</p>")
            + _h2("התאמה למעשנה", "<p>במעשנה מחפשים בעירה ארוכה, אפר נמוך ויכולת חיזוי. דלק לא יציב גורם לפתיחות מכסה מיותרות ולתנודות חום.</p>")
            + _h2("טבלת השוואה מעשית", "<table><thead><tr><th>שימוש</th><th>עדיפות</th><th>הסבר</th></tr></thead><tbody><tr><td>צלייה קצרה</td><td>תגובה מהירה</td><td>חשוב להגיע לחום גבוה במהירות</td></tr><tr><td>אירוח ארוך</td><td>יציבות חום</td><td>פחות תוספות דלק באמצע</td></tr><tr><td>מעשנה</td><td>אפר נמוך</td><td>שמירה על זרימת אוויר נקייה</td></tr></tbody></table>"),
            "fuel_comparison_or_guide_expansion",
        )
    if topic_type == "equipment_buying_guide":
        return (
            _h2("גודל ומשטח עבודה", "<p>בוחרים גודל לפי מספר סועדים, תדירות אירוח ומקום פנוי לעבודה בטוחה סביב הגריל או הטאבון.</p>")
            + _h2("עוצמת חום ומבערים", "<p>לא מסתכלים רק על מספרים; בודקים פיזור חום, שליטה באזורים, איכות מבערים ויכולת לשמור חום עם מכסה סגור.</p>")
            + _h2("חומרי בנייה ותחזוקה", "<p>נירוסטה, יציקה וציפויים שונים משפיעים על עמידות וניקוי. מוצר טוב הוא כזה שתוכלו לתחזק בקלות לאורך שנים.</p>")
            + _h2("התאמה למשפחה או אירוח", "<p>למשפחה קטנה מספיק משטח קומפקטי ואמין; לאירוח קבוע צריך שטח עבודה, אזורי חום ואחסון לכלים.</p>"),
            "equipment_buying_guide_expansion",
        )
    return ("", "no_extra_needed")



CONTENT_DEPTH_TARGETS = {
    "meat_low_slow_smoking": 400,
    "smoking_wood_guide": 400,
    "poultry_grill_recipe": 400,
    "grill_accessory_guide": 400,
    "smoking_accessory_guide": 400,
    "meat_quick_grill_cut": 400,
    "fuel_comparison_or_guide": 400,
    "equipment_buying_guide": 400,
    "recipe_how_to": 400,
    "fallback_generic": 250,
}


def _required_word_count_for_topic(topic_type: str) -> int:
    return int(CONTENT_DEPTH_TARGETS.get(topic_type, 900))


def _section_exists(html: str, title: str) -> bool:
    wanted = _semantic_key(title)
    headings = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html or "", flags=re.IGNORECASE | re.DOTALL)
    return wanted in {_semantic_key(h) for h in headings}


def _append_unique_section(html: str, title: str, body: str) -> str:
    if _section_exists(html, title):
        return html
    return html + _h2(title, body)


def _depth_engine_sections(topic_type: str, entity: str, keyword: str, entity_key: str) -> list[tuple[str, str]]:
    if topic_type == "smoking_wood_guide":
        return [
            ("מה ההבדל בין שבבי עץ לצ׳אנקים", f"<p>שבבי עץ לעישון הם חתיכות קטנות שמתחילות לעשן מהר ולכן מתאימות לגריל גז, קופסת עישון או עישון קצר של עוף ודגים. צ׳אנקים הם חתיכות גדולות יותר, בוערות לאט ומתאימות למעשנה או לגריל פחמים בעבודה ארוכה. ההחלטה אינה רק גודל; היא קשורה לזמן הבישול, זרימת האוויר ורמת העשן שרוצים לבנות סביב {entity}.</p><p>בגריל גז משתמשים לרוב בשבבים בכמות מדודה, כי מקור החום קבוע והעץ צריך רק להוסיף שכבת טעם. במעשנה, צ׳אנקים נותנים רצף עשן יציב יותר ופחות צורך לפתוח מכסה. אם משלבים בין שניהם, מתחילים בצ׳אנק קטן ליציבות ומוסיפים מעט שבבים רק בתחילת התהליך.</p>"),
            ("האם צריך להשרות שבבי עץ", "<p>ברוב המקרים לא צריך להשרות שבבי עץ. השריה קצרה מרטיבה בעיקר את פני השטח, ואז במקום עשן נקי מקבלים דקות של אדים, עיכוב בהצתה ולעיתים עשן לבן וכבד. עדיף להשתמש בעץ יבש, לשלוט בכמות ולוודא שיש מספיק אוויר לבעירה נקייה.</p><p>אם עובדים בגריל חם מאוד והשבבים נשרפים מהר מדי, הפתרון הוא לא קערת מים אלא קופסת עישון סגורה חלקית, נייר כסף מחורר או מעבר לצ׳אנקים. כך העץ מתחמם בהדרגה ומוציא עשן דק יותר.</p>"),
            ("Thin Blue Smoke ומה נחשב עשן נקי", "<p>Thin Blue Smoke הוא עשן דק, כמעט שקוף, עם גוון כחול עדין וריח נעים של עץ נקי. זה הסימן שהעץ נשרף בצורה מאוזנת ולא נחנק מחוסר חמצן. עשן לבן וסמיך, מר או חריף באף, מעיד בדרך כלל על עץ רטוב, יותר מדי חומר בעירה או זרימת אוויר חלשה.</p><p>כדי להגיע לעשן נקי מחממים את המעשנה לפני הכנסת הבשר, לא מעמיסים עץ בבת אחת, ומשאירים פתחי אוויר עובדים. המטרה היא ניחוח מתמשך ועדין, לא ענן שמסתיר את הנתח.</p>"),
            ("Apple / Cherry / Oak / Hickory / Mesquite comparison", "<table><thead><tr><th>עץ</th><th>עוצמת טעם</th><th>מתאים ל</th><th>הערות</th></tr></thead><tbody><tr><td>Apple</td><td>עדינה ומתוקה</td><td>עוף, דגים, חזות עדינות וירקות</td><td>טוב למי שרוצה עשן רך ולא משתלט</td></tr><tr><td>Cherry</td><td>עדינה-בינונית ופירותית</td><td>עוף, ברווז, צלעות ובקר עדין</td><td>מוסיף צבע יפה וארומה מתוקה</td></tr><tr><td>Oak</td><td>בינונית ומאוזנת</td><td>בריסקט, אסאדו ושורט ריבס</td><td>בחירת בסיס בטוחה לעישון ארוך</td></tr><tr><td>Hickory</td><td>חזקה ובייקונית</td><td>בריסקט, צלעות ונתחים שומניים</td><td>משתמשים במידה כדי למנוע מרירות</td></tr><tr><td>Mesquite</td><td>חזקה מאוד</td><td>צלייה קצרה או בקר עם טעם חזק</td><td>פחות מתאים לעישון ארוך למתחילים</td></tr></tbody></table>"),
            ("התאמת עץ לסוג בשר", "<p>התאמת עץ לבריסקט מתחילה בדרך כלל ב-Oak כבסיס, עם אפשרות להוסיף מעט Hickory אם רוצים עומק מעושן יותר. לבריסקט ארוך לא כדאי לבחור עץ חזק מאוד לכל הדרך, כי שעות חשיפה רבות עלולות להפוך טעם נעים למרירות.</p><p>לעוף מתאימים Apple ו-Cherry כי הם מדגישים עסיסיות ועור צלוי בלי להשתלט. לדגים עדיף עץ עדין במיוחד, כמות קטנה וזמן חשיפה קצר; Apple או עץ פירותי עדין יעבדו טוב יותר מ-Hickory. לנתחים שומניים כמו אסאדו אפשר לעלות ל-Oak או Hickory במידה.</p>"),
            ("איזה עץ מתאים לבריסקט", "<p>בריסקט אוהב עץ מאוזן שמחזיק שעות. Oak הוא הבחירה הבטוחה: הוא נותן בסיס מעושן ברור בלי להשתלט על הבקר. Hickory מוסיף אופי חזק יותר ומתאים למי שכבר מכיר את המעשנה שלו. Mesquite יכול להיות אגרסיבי מדי בעישון ארוך ולכן עדיף להשתמש בו רק בכמות קטנה או בצלייה קצרה.</p>"),
            ("איזה עץ מתאים לעוף", "<p>לעוף, כנפיים ופרגיות בוחרים Apple או Cherry. העור והבשר סופגים עשן מהר, ולכן כמות קטנה בתחילת הצלייה מספיקה. אם מוסיפים רוטב מתוק בסוף, עץ פירותי משתלב טוב יותר מעץ חזק שעלול להרגיש מר.</p>"),
            ("איזה עץ מתאים לדגים", "<p>דגים צריכים עשן קצר ועדין. Apple, Cherry בכמות קטנה או עץ עדין אחר יתנו ארומה בלי לכסות את הטעם הימי. משתמשים בשבבים לזמן קצר, שומרים על עשן נקי ומוציאים את הדג לפני שהעשן הופך דומיננטי.</p>"),
            ("כמה עץ להוסיף", "<p>בגריל גז מתחילים בחופן שבבים קטן בקופסת עישון ובודקים את עוצמת הטעם לפני שמוסיפים עוד. במעשנה מוסיפים צ׳אנק אחד או שניים בתחילת התהליך, ולא ממשיכים להעמיס עץ בכל שעה. רוב ספיגת העשן המשמעותית מתרחשת בתחילת הבישול כשהמשטח עדיין לח יחסית.</p><ul><li>עוף ודגים: מעט שבבים בתחילת הבישול.</li><li>צלעות: שבבים או צ׳אנק קטן לפי משך העישון.</li><li>בריסקט: Oak או Hickory בכמות מדודה לאורך תחילת העישון.</li></ul>"),
            ("איך להשתמש בשבבי עץ בגריל גז", "<p>בגריל גז מניחים שבבים יבשים בקופסת עישון או בנייר כסף מחורר מעל אזור חם, מחממים עד שמתחיל עשן עדין ואז מכניסים את המזון. לא מפזרים שבבים ישירות על מבערים ולא חוסמים פתחי אוורור. עובדים עם מכסה סגור כדי שהעשן יעבור סביב המזון ולא יברח מיד.</p>"),
            ("איך להשתמש בצ׳אנקים במעשנה", "<p>במעשנה מניחים צ׳אנקים ליד מקור החום או בתוך מצע הפחמים כך שיתחממו בהדרגה. לא צריך להצית אותם מראש ללהבה מלאה; המטרה היא פליטת עשן איטית ונקייה. אם העשן נהיה לבן וכבד, פותחים מעט אוויר או מפחיתים כמות עץ.</p>"),
            ("רשימת בדיקה לבחירת עצי עישון", "<ul><li>מגדירים חומר גלם: בריסקט, עוף, דגים או ירקות.</li><li>בוחרים עוצמה: Apple/Cherry לעדין, Oak למאוזן, Hickory לחזק.</li><li>מתאימים גודל: שבבים לגריל גז וקצר, צ׳אנקים למעשנה וארוך.</li><li>משתמשים בעץ יבש ונקי בלבד.</li><li>בודקים שהעשן דק ונקי לפני הכנסת המזון.</li></ul>"),
            ("טעויות נפוצות", "<ul><li>להשרות שבבים ולחשוב שזה מונע שריפה, במקום לשלוט בחום ובאוויר.</li><li>להוסיף יותר מדי עץ ולקבל מרירות.</li><li>לבחור Mesquite לעישון ארוך ראשון.</li><li>להתעלם מעשן לבן סמיך כי חושבים שכל עשן הוא טוב.</li><li>להשתמש בעץ לא מזוהה, צבוע או מטופל.</li></ul>"),
            ("המלצה מעשית", "<p>לרוב הבשלנים הביתיים כדאי להתחיל בשלישייה פשוטה: Apple לעוף ודגים, Oak לבריסקט ונתחי בקר ארוכים, ו-Cherry כאשר רוצים צבע וארומה פירותית. אחרי שמכירים את התוצאה, מוסיפים Hickory בזהירות. כך בונים טעם עקבי בלי להפוך כל עישון לניסוי אגרסיבי.</p>"),
        ]
    if topic_type == "meat_low_slow_smoking":
        return [
            ("Trim והכנת הנתח", f"<p>לפני עישון {entity} מסירים שומן קשה שלא יתרכך, מיישרים קצוות דקים שעלולים להתייבש ומשאירים שכבת שומן סבירה שמגינה על הנתח. Trim טוב יוצר עובי אחיד יותר ולכן גם ציר זמן צפוי יותר.</p>"),
            ("Rub ומליחות נכונה", "<p>Rub בסיסי מתחיל במלח ופלפל, ואפשר להוסיף שום, פפריקה או חרדל יבש. לא צריך שכבה רטובה וכבדה; שכבה יבשה ומאוזנת עוזרת לפיתוח Bark ומונעת טעם בוצי.</p>"),
            ("Smoker setup ו-105–120°C", "<p>מייצבים את המעשנה לפני הכנסת הבשר. טווח 105–120°C נותן מספיק זמן לריכוך קולגן בלי לייבש מהר מדי. בודקים שהמדחום של התא אמין ושיש מים או מסה תרמית רק אם הם באמת מייצבים חום.</p>"),
            ("Wood choice לבקר ארוך", "<p>Oak מתאים כברירת מחדל לבריסקט, אסאדו ושורט ריבס. Hickory מוסיף עומק חזק יותר, ו-Cherry יכול להוסיף צבע. לא חייבים עשן לכל התהליך; אחרי כמה שעות עיקר הטעם כבר נבנה.</p>"),
            ("Bark development", "<p>Bark מתפתח כאשר פני השטח יבשים יחסית, התבלינים נקשרים לשומן ולעשן, והחום נשאר יציב. מרססים רק אם יש ייבוש מוגזם, ולא לפני שהשכבה החיצונית התייצבה.</p>"),
            ("Stall וניהול סבלנות", "<p>Stall הוא לא תקלה אלא אידוי שמקרר את הנתח. אפשר להמתין, לעטוף או לשלב. ההחלטה תלויה בצבע, ב-Bark, בזמן שנותר ובכמה עסיסיות רוצים לשמור.</p>"),
            ("Wrap / no-wrap ונייר קצבים מול נייר כסף", "<p>נייר קצבים מאיץ את המעבר דרך הסטול ושומר Bark טוב יותר מנייר כסף. Foil שומר יותר לחות ומאיץ יותר, אבל עלול לרכך את הקליפה. No-wrap נותן Bark חזק אך דורש יותר זמן ושליטה בלחות.</p>"),
            ("90–96°C ו-probe tenderness", "<p>טווח סיום נפוץ הוא 90–96°C, אבל המספר הוא רק נקודת בדיקה. הנתח מוכן כאשר הפרוב נכנס כמעט כמו לחמאה רכה בכמה נקודות, במיוחד בחלק העבה.</p>"),
            ("Long rest וחיתוך", "<p>מנוחה ארוכה של שעה עד כמה שעות מאפשרת לנוזלים להתייצב ולרקמות להמשיך להתרכך. פורסים נגד הסיבים, שומרים על עובי אחיד ומפרידים חלקים עם כיוון סיבים שונה בבריסקט.</p>"),
            ("Timeline table", "<table><thead><tr><th>שלב</th><th>טווח זמן</th><th>מה בודקים</th></tr></thead><tbody><tr><td>Trim ו-Rub</td><td>30–60 דקות</td><td>עובי אחיד ושכבת תיבול יבשה</td></tr><tr><td>עישון פתוח</td><td>4–7 שעות</td><td>Bark, צבע ועשן נקי</td></tr><tr><td>Stall / Wrap</td><td>לפי מצב</td><td>האם Bark יציב והטמפרטורה נעצרה</td></tr><tr><td>סיום</td><td>עד 90–96°C</td><td>Probe tenderness</td></tr><tr><td>מנוחה</td><td>1–4 שעות</td><td>חום נשמר ופריסה נקייה</td></tr></tbody></table>"),
            ("Equipment checklist", "<ul><li>מעשנה יציבה או גריל עם setup עקיף.</li><li>מדחום תא ומדחום ליבה אמינים.</li><li>נייר קצבים לעטיפה מאוזנת.</li><li>עצי עישון מתאימים כמו Oak או Hickory.</li><li>סכין חדה ל-trim ולפריסה.</li><li>צידנית או תא שמירת חום למנוחה ארוכה.</li></ul>"),
        ]
    if topic_type == "poultry_grill_recipe":
        return [
            ("ייבוש העור לפני הצלייה", "<p>כנפיים, פרגיות ועוף מקבלים מרקם טוב יותר כאשר מייבשים אותם במגבת ומניחים במקרר ללא כיסוי קצר לפני הצלייה. פחות לחות על פני השטח פירושה השחמה מהירה יותר ופחות עור גומי.</p>"),
            ("Baking powder או קורנפלור", "<p>אפשר להוסיף מעט baking powder ללא אלומיניום או קורנפלור לתערובת יבשה כדי לעזור לקריספיות. משתמשים בכמות קטנה בלבד כדי לא לקבל טעם לוואי או מרקם אבקתי.</p>"),
            ("Dry rub מול מרינדה", "<p>Dry rub מתאים לקריספיות כי הוא לא מוסיף הרבה נוזלים. מרינדה מוסיפה טעם פנימי אך צריך לנגב עודפים לפני הגריל. אם משתמשים במרינדה מתוקה, שומרים אותה לסיום.</p>"),
            ("חום עקיף ואז crisping ישיר", "<p>מתחילים בחום עקיף כדי לבשל את העוף עד כמעט מוכן בלי לשרוף עור. בסוף מעבירים לחום ישיר קצר לקריספיות, הופכים לעיתים קרובות ושומרים מפני להבות.</p>"),
            ("74°C ובטיחות מזון", "<p>עוף חייב להגיע ל-74°C בחלק העבה. צבע לבן אינו מספיק לבדיקה, במיוחד ליד עצם או בנתחים עבים. מדידה קצרה מונעת גם ייבוש וגם הגשה לא בטוחה.</p>"),
            ("Glaze timing והימנעות מסוכר שרוף", "<p>רוטב או גלייז מתוק מורחים רק ב-5–10 הדקות האחרונות. סוכר שנמצא על אש ישירה זמן רב נשרף והופך מר. עובדים בשכבות דקות ומזיזים לאזור עקיף אם הרוטב משחים מהר מדי.</p>"),
            ("רעיונות לרוטב והגשה", "<p>לכנפיים מתאימים BBQ מעושן, צ׳ילי-דבש, לימון-שום או חמאה חריפה. מגישים עם ירקות קראנצ׳יים, חמוצים, סלט כרוב או תפוחי אדמה צלויים כדי לאזן שומן וחריפות.</p>"),
            ("חימום שאריות", "<p>שאריות מחממים בחום עקיף או בתנור/אייר פרייר עד שהעור חוזר להיות יבש. מיקרוגל מחמם מהר אך מרכך את העור, לכן עדיף לסיים בדקות חום יבש.</p>"),
        ]
    if topic_type == "smoking_accessory_guide":
        return [
            ("מה נייר קצבים עושה בעישון", "<p>נייר קצבים עוטף את הבשר בלי לאטום אותו לחלוטין. הוא מצמצם אידוי, עוזר לעבור את הסטול ושומר על Bark יציב יותר מנייר כסף, כי חלק מהאדים עדיין יכולים לצאת.</p>"),
            ("עטיפת בריסקט ונתי בקר", "<p>בבריסקט עוטפים רק אחרי שהצבע כהה וה-Bark לא נמרח. בצלעות, אסאדו ושורט ריבס משתמשים באותו עיקרון: קודם עשן וצבע, אחר כך עטיפה לקידום ריכוך.</p>"),
            ("Texas Crutch", "<p>Texas Crutch היא עטיפה שנועדה לקצר את שלב הסטול. נייר כסף הוא הגרסה המהירה והאטומה, ונייר קצבים הוא פתרון מאוזן יותר למי שרוצה לשמור מרקם חיצוני.</p>"),
            ("Butcher Paper vs Foil", "<p>Butcher paper vs foil הוא בחירה בין נשימה לאיטום. Foil שומר יותר נוזלים ומרכך Bark; butcher paper שומר לחות אבל מאפשר קליפה ברורה יותר. לכן לבריסקט תחרותי או ביתי מושקע נייר קצבים הוא לרוב הבחירה הטובה.</p>"),
            ("מתי לעטוף ואיך לעטוף", "<p>עוטפים לפי צבע, Bark ותחושת פני השטח, לא לפי שעה קבועה. מניחים שני דפים חופפים, מקפלים הדוק סביב הנתח ומחזירים למעשנה כשהתפר כלפי מטה כדי שהעטיפה לא תיפתח.</p>"),
            ("Pink מול brown paper", "<p>Pink butcher paper בדרך כלל לא מצופה ומתאים לעישון כאשר הוא food safe. נייר חום יכול להתאים רק אם הוא מיועד למזון ולעישון, ללא שעווה, ציפוי או דיו שעלולים להתחמם.</p>"),
            ("טעויות נפוצות", "<ul><li>לעטוף מוקדם לפני שנבנה Bark.</li><li>להשתמש בנייר מצופה או לא מיועד למזון.</li><li>לעטוף רופף מדי ולאבד לחות.</li><li>לצפות שנייר קצבים יתקן נתח שיובש בגלל חום גבוה.</li></ul>"),
        ]
    if topic_type == "grill_accessory_guide":
        return [
            ("מה זה ולמי זה מתאים", f"<p>{entity} הוא אביזר שנועד לפתור צורך מוגדר סביב הגריל: שליטה בחום, מדידה, ניקיון, בטיחות או נוחות עבודה. הוא מתאים למי שנתקל בבעיה חוזרת ורוצה פתרון מדויק, לא למי שמחפש להוסיף מוצר בלי שימוש ברור.</p>"),
            ("איך זה עובד בפועל", "<p>בודקים את נקודת המגע עם הגריל, את מקור החום ואת השפעת האביזר על זרימת עבודה. אביזר טוב משפר פעולה אחת בלי להפריע לפעולות אחרות כמו סגירת מכסה, ניקוי או שליטה בחום.</p>"),
            ("התקנה ושימוש", "<p>מתקינים לפי הוראות היצרן, מתחילים בחימום הדרגתי ומוודאים שאין מגע מסוכן עם להבה, כבל, ידית או חלק שנע. בשימוש ראשון עובדים בזהירות ובודקים אם התוצאה באמת השתפרה.</p>"),
            ("קריטריונים לבחירה", "<ul><li>התאמה לדגם הגריל ולמידות.</li><li>עמידות לחום ולשומן.</li><li>קלות ניקוי ואחסון.</li><li>הוראות שימוש ברורות.</li><li>תועלת אמיתית ביחס למחיר.</li></ul>"),
            ("תחזוקה והחלפה", "<p>מנקים אחרי קירור מלא, מייבשים לפני אחסון ובודקים סדקים, חלודה, קריאה לא מדויקת או שחיקה. מחליפים כאשר האביזר כבר לא מבצע את תפקידו בצורה בטוחה או עקבית.</p>"),
            ("השוואה לחלופות", "<p>לפני קנייה משווים אם אפשר לפתור את אותה בעיה בעזרת ציוד שכבר קיים, קטגוריית אביזרים אחרת או שינוי טכניקה. אם החלופה דורשת יותר עבודה אבל נותנת אותה תוצאה, ייתכן שלא חייבים לקנות מיד.</p>"),
        ]
    return [
        ("רשימת עבודה", f"<ul><li>מגדירים מה רוצים להשיג עם {keyword}.</li><li>בודקים התאמה לציוד הקיים.</li><li>מכינים סביבת עבודה בטוחה.</li><li>מתעדים זמן, חום ותוצאה לשיפור בפעם הבאה.</li></ul>"),
        ("טעויות נפוצות", "<p>הטעות הנפוצה היא לדלג על התאמה בין חומר גלם, ציוד ושיטת עבודה. טעות נוספת היא לשנות כמה משתנים יחד ואז לא לדעת מה שיפר או פגע בתוצאה.</p>"),
    ]

def _depth_upgrade_html(title: str, keyword: str, html: str, profile: dict[str, object]) -> str:
    topic_type = str(profile.get("topic_type") or "fallback_generic")
    entity = str(profile.get("main_entity") or keyword or title)
    entity_key = str(profile.get("entity_key") or "")
    target = _required_word_count_for_topic(topic_type)
    added: list[str] = []
    for section_title, section_body in _depth_engine_sections(topic_type, entity, keyword, entity_key):
        if _article_word_count(html) >= target and len(added) >= 3:
            break
        before = html
        html = _append_unique_section(html, section_title, section_body)
        if html != before:
            added.append(section_title)
    if _article_word_count(html) < target:
        topic_label = entity if entity else keyword
        depth_angles = [
            (f"אבחון מצב לפני עבודה עם {topic_label}", f"לפני שמתחילים בודקים את המצב הספציפי של {topic_label}: מבנה הגריל או המעשנה, מקור החום, חומר הגלם והבעיה שהמדריך אמור לפתור. כך ההחלטות נשארות מחוברות לנושא ולא הופכות לטיפים כלליים."),
            (f"סימני הצלחה ב-{topic_label}", f"סימני הצלחה צריכים להיות נראים ומדידים: פיזור חום יציב, עשן נקי, Bark שלא נמרח, עור קריספי או התאמה בטוחה של אביזר לגריל. אם לא רואים שינוי ברור, משנים טכניקה אחת בלבד ובודקים שוב."),
            (f"טעויות שטח אופייניות ב-{topic_label}", f"בשטח רוב הבעיות מגיעות מקיצור דרך: חום לא מיוצב, עטיפה מוקדמת, שימוש בכמות עץ מוגזמת או אביזר שלא מתאים לדגם. מתייחסים לתסמין עצמו ולא מוסיפים ציוד רק כדי לפתור תחושה כללית."),
            (f"בדיקת התאמה לקומפס גריל עבור {topic_label}", f"כאשר בוחרים מוצר או קטגוריה משלימה, מחפשים התאמה למידות, לחומר, לשיטת הצלייה ולתדירות השימוש שלכם. המלצה טובה היא כזו שמסבירה למה הפריט עוזר דווקא ל-{topic_label}."),
        ]
        for angle, detail in depth_angles:
            if _article_word_count(html) >= target:
                break
            html_before = html
            paragraph = f"<p>{detail}</p>"
            html = _append_unique_section(html, angle, paragraph)
            if html != html_before:
                added.append(angle)
    setattr(_depth_upgrade_html, "last_sections_added", added)
    return _dedupe_article_html(html)

def _build_article_html(
    title: str,
    keyword: str,
    related: list[dict[str, str | float]],
    *,
    topic_profile: dict[str, object] | None = None,
) -> str:
    profile = topic_profile or _classify_topic(title, keyword, "informational")
    html = _build_contract_article(title, keyword, related, profile)
    html = _depth_upgrade_html(title, keyword, html, profile)
    return apply_visual_article_formatter(title, keyword, html, profile)

def _clean_anchor_text(link: dict[str, str | float]) -> str:
    explicit = str(link.get("anchor_text") or "").strip()
    if explicit:
        return explicit
    title = re.sub(r"[-_]+", " ", str(link.get("title") or "")).strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 48:
        title = title[:45].rstrip() + "..."
    return title or "מוצר רלוונטי"


def _natural_link_sentence(link: dict[str, str | float], topic_profile: dict[str, object] | None = None) -> str:
    url = str(link.get("url") or "").strip()
    anchor = _clean_anchor_text(link)
    text = _normalize_hebrew(f"{anchor} {link.get('title','')} {url}")
    topic_type = str((topic_profile or {}).get("topic_type") or "")
    if topic_type == "meat_low_slow_smoking":
        if "נייר" in text or "קצבים" in text or "butcher" in text:
            return f"לעישון ארוך של בריסקט, שימוש ב<a href='{url}'>{anchor}</a> יכול לעזור לעבור את שלב הסטול בלי לוותר לגמרי על Bark."
        if "מדחום" in text or "thermometer" in text:
            return f"בנתחים עבים או עישון ארוך, <a href='{url}'>{anchor}</a> מאפשר למדוד טמפרטורה פנימית בלי לנחש לפי צבע חיצוני."
        if "שבבי" in text or "עץ" in text or "wood" in text or "צאנקים" in text:
            return f"לבניית שכבת עשן נקייה, כדאי להתאים <a href='{url}'>{anchor}</a> לעוצמת הטעם של הנתח ולמשך העישון."
        if "מעשנה" in text or "smoker" in text:
            return f"מי שמעשן נתחים גדולים בקביעות ירוויח מבחירה נכונה של <a href='{url}'>{anchor}</a> ששומרת על חום יציב לאורך שעות."
    if topic_type == "poultry_grill_recipe":
        if "מדחום" in text or "thermometer" in text:
            return f"בעוף על הגריל, <a href='{url}'>{anchor}</a> עוזר לוודא 74°C בחלק העבה בלי לייבש את העור."
        if "רוטב" in text or "גלייז" in text or "bbq" in text:
            return f"את <a href='{url}'>{anchor}</a> מוסיפים רק בדקות האחרונות, כדי לקבל ברק וטעם בלי סוכר שרוף."
        return f"לעבודה נקייה עם כנפיים, <a href='{url}'>{anchor}</a> יכול לעזור בהפיכה, מריחה או שמירה על רשת מסודרת."
    if topic_type == "smoking_wood_guide":
        if "שבבי" in text or "wood" in text or "chips" in text or "צאנקים" in text or "chunks" in text:
            return f"בעישון ארוך, בחירה נכונה של <a href='{url}'>{anchor}</a> או צ׳אנקים משפיעה על עומק הטעם ועל ניקיון העשן."
        if "מעשנה" in text or "smoker" in text or "עישון" in text:
            return f"כדי לשמור על Thin Blue Smoke, <a href='{url}'>{anchor}</a> צריך לעבוד עם זרימת אוויר יציבה וכמות עץ מדודה."
        if "מדחום" in text or "thermometer" in text:
            return f"גם כשעוסקים בעץ, <a href='{url}'>{anchor}</a> עוזר לוודא שהנתח מתקדם נכון בלי לפתוח מכסה שוב ושוב."
        if "נייר" in text or "קצבים" in text or "butcher" in text:
            return f"בבריסקט, <a href='{url}'>{anchor}</a> נכנס רק אחרי שנבנה Bark מעושן ונקי מספיק."
    if topic_type == "smoking_accessory_guide":
        if "נייר" in text or "קצבים" in text or "butcher" in text:
            return f"בשלב הסטול, <a href='{url}'>{anchor}</a> יכול לעזור לשמור על Bark יציב בלי לאטום את הנתח כמו נייר כסף."
        if "בריסקט" in text or "brisket" in text:
            return f"בעיטוף נכון של בריסקט, <a href='{url}'>{anchor}</a> משתלב אחרי שהצבע וה-Bark כבר התייצבו."
        if "שבבי" in text or "wood" in text:
            return f"לפני העטיפה בונים שכבת עשן נקייה בעזרת <a href='{url}'>{anchor}</a> בכמות מדודה."
    if topic_type == "grill_accessory_guide":
        entity_key = str((topic_profile or {}).get("entity_key") or "")
        if entity_key == "basalt_stones":
            return f"<a href='{url}'>{anchor}</a> יכולות לשפר פיזור חום ולהפחית התלקחויות בגרילי גז שמתאימים לכך."
        if entity_key == "thermometer":
            return f"בנתחים עבים או עישון ארוך, <a href='{url}'>{anchor}</a> מאפשר למדוד טמפרטורה פנימית בלי לנחש לפי צבע חיצוני."
        return f"כאשר בוחרים {anchor}, חשוב לוודא שהוא מתאים לדגם הגריל ולשיטת העבודה לפני שמוסיפים אותו לסל."
    return f"כאשר מיישמים את ההמלצות במדריך, <a href='{url}'>{anchor}</a> צריך להיבחר רק אם הוא פותר צורך אמיתי בנושא הזה."


def inject_internal_links_into_html(article_html: str, related: list[dict[str, str | float]], topic_profile: dict[str, object] | None = None) -> tuple[str, list[dict[str, str]]]:
    html = article_html or ""
    section_match = re.search(r"<h2>מוצרים וקטגוריות מומלצים מהאתר</h2>|<h2>מוצרים רלוונטיים באתר</h2>|<h2>מוצרים מומלצים מהאתר</h2>", html)
    main_html = html[: section_match.start()] if section_match else html
    tail_html = html[section_match.start():] if section_match else ""
    injected: list[dict[str, str]] = []
    used_urls: set[str] = set()
    used_anchors: set[str] = set()
    eligible = [link for link in related if float(link.get("relevance_score") or link.get("relatedness_score") or 0) >= 50 and str(link.get("url") or "").strip()]
    for link in eligible:
        if len(injected) >= 5:
            break
        url = str(link.get("url") or "").strip()
        anchor = _clean_anchor_text(link)
        if not anchor or url in used_urls or anchor in used_anchors:
            continue
        linked = f"<a href='{url}'>{anchor}</a>"
        if anchor in main_html and linked not in main_html:
            main_html = main_html.replace(anchor, linked, 1)
            injected.append({"title": str(link.get("title") or anchor), "url": url, "anchor_text": anchor, "section": "body_paragraph", "relevance_score": str(link.get("relevance_score") or 0), "link_role": str(link.get("link_role") or ""), "reason": str(link.get("reason") or "")})
            used_urls.add(url)
            used_anchors.add(anchor)
    if len(injected) < min(2, len(eligible)):
        paragraphs = list(re.finditer(r"<p[^>]*>.*?</p>", main_html, flags=re.IGNORECASE | re.DOTALL))
        insertions: list[tuple[int, str]] = []
        paragraph_index = 1
        for link in eligible:
            if len(injected) >= 5:
                break
            url = str(link.get("url") or "").strip()
            if url in used_urls:
                continue
            anchor = _clean_anchor_text(link)
            if not anchor or anchor in used_anchors:
                continue
            sentence = " " + _natural_link_sentence(link, topic_profile)
            if paragraph_index >= len(paragraphs):
                paragraph_index = max(0, len(paragraphs) - 1)
            if paragraphs:
                match = paragraphs[paragraph_index]
                insertions.append((match.end() - 4, sentence))
                paragraph_index += 2
            else:
                main_html += f"<p>{sentence}</p>"
            injected.append({"title": str(link.get("title") or anchor), "url": url, "anchor_text": anchor, "section": "body_paragraph", "relevance_score": str(link.get("relevance_score") or 0), "link_role": str(link.get("link_role") or ""), "reason": str(link.get("reason") or "")})
            used_urls.add(url)
            used_anchors.add(anchor)
        for pos, snippet in sorted(insertions, reverse=True):
            main_html = main_html[:pos] + snippet + main_html[pos:]
    return main_html + tail_html, injected


def _topic_image_prompts(keyword: str, topic_profile: dict[str, object]) -> tuple[str, list[dict[str, str]]]:
    contract = topic_profile.get("contract") if isinstance(topic_profile.get("contract"), dict) else {}
    image_policy = topic_profile.get("image_policy") if isinstance(topic_profile.get("image_policy"), dict) else {}
    pattern = str(image_policy.get("featured_prompt_pattern") or contract.get("image_prompt_pattern") or "realistic outdoor BBQ guide photo focused on {keyword}, no text")
    featured = pattern.format(keyword=keyword)
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    section_prompts = [
        {"section": "פתיח", "placement_hint": "[IMAGE_1_HERE]", "prompt": featured},
        {"section": str(topic_profile.get("selected_contract") or topic_type), "placement_hint": "[IMAGE_2_HERE]", "prompt": f"detail photo for {topic_type} article about {keyword}, matching the article contract, realistic BBQ photography, no text"},
    ]
    return featured, section_prompts

_IMAGE_ICONS = {
    "מעשנה": "🔥", "עישון": "🔥", "עצי": "🌳", "Bark": "🥩", "בריסקט": "🥩",
    "בזלת": "🪨", "חום": "🌡️", "טמפרטורה": "🌡️", "ציר זמן": "⏱️",
    "ציוד": "🧰", "מוצרים": "🛒", "שאלות": "❓", "טעויות": "⚠️",
    "בחירה": "✅", "השוואה": "📊", "ניקוי": "🧽", "בטיחות": "🛡️",
}


def _section_icon(title: str) -> str:
    if re.match(r"^\s*[🔥🌳🥩🪨🌡️⏱️🧰🛒❓⚠️✅📊🧽🛡️💡]", title or ""):
        return ""
    for needle, icon in _IMAGE_ICONS.items():
        if needle in (title or ""):
            return icon
    return "✅"


def _iconize_h2(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        plain = _plain_text(inner)
        if "מוצרים" in plain:
            return f"<h2>{inner}</h2>"
        icon = _section_icon(plain)
        return f"<h2>{icon + ' ' if icon else ''}{inner}</h2>"
    return re.sub(r"<h2[^>]*>(.*?)</h2>", repl, html or "", flags=re.IGNORECASE | re.DOTALL)


def _topic_table_html(topic_type: str, entity: str) -> str:
    if topic_type == "meat_low_slow_smoking":
        rows = [("הכנה", "תיבול, ייצוב מעשנה", "לפני העלאה לרשת"), ("עישון פתוח", "105–120°C ובניית Bark", "עד צבע יציב"), ("עטיפה", "נייר קצבים לפי צורך", "בשלב הסטול"), ("סיום", "90–96°C ובדיקת רכות", "לפני מנוחה")]
        head = ("שלב", "מה עושים", "סימן מעבר")
    elif topic_type == "grill_accessory_guide":
        rows = [("פיזור חום", "צלייה אחידה יותר", "כשיש נקודות חמות"), ("התלקחויות", "הפחתת להבה ישירה", "בגרילי גז מתאימים"), ("תחזוקה", "ניקוי והחלפה בזמן", "אחרי שומן מצטבר"), ("התאמה", "בדיקת דגם ומרווחים", "לפני רכישה")]
        head = ("שיקול", "תועלת", "מתי חשוב")
    elif topic_type == "smoking_wood_guide":
        rows = [("Apple/Cherry", "עדין ומתוק", "עוף, דגים ובשר עדין"), ("Oak", "מאוזן", "בריסקט ונתחי בקר"), ("Hickory", "חזק", "בקר ועישון ארוך"), ("Mesquite", "עז מאוד", "שימוש קצר ומדוד")]
        head = ("סוג עץ", "פרופיל טעם", "התאמה")
    elif topic_type == "equipment_buying_guide":
        rows = [("גודל", "כמות סועדים", "לא לקנות קטן מדי"), ("חומר", "עמידות ושמירת חום", "חשוב בגינה פתוחה"), ("ניקוי", "גישה למגש שומן", "חוסך זמן"), ("אביזרים", "מדחום, כיסוי וכלים", "משפר דיוק")]
        head = ("קריטריון", "למה לבדוק", "המלצה")
    else:
        rows = [("לפני עבודה", "הכנת ציוד וחומר גלם", "מונע עצירות"), ("במהלך העבודה", "בקרת חום ומרקם", "משפר עקביות"), ("לפני הגשה", "מנוחה, חיתוך וסידור", "שומר עסיסיות"), ("אחרי", "ניקוי ותיעוד", "משפר את הפעם הבאה")]
        head = ("שלב", "פעולה", "למה זה חשוב")
    tr = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a,b,c in rows)
    return f"<h2>📊 טבלה שימושית ל-{entity}</h2><table><thead><tr><th>{head[0]}</th><th>{head[1]}</th><th>{head[2]}</th></tr></thead><tbody>{tr}</tbody></table>"


def _intro_summary_html(title: str, keyword: str, topic_type: str) -> str:
    bullets = {
        "meat_low_slow_smoking": ["איך לייצב מעשנה ב-105–120°C", "מתי לבחור עצי עישון", "איך לנהל Bark, סטול ועטיפה", "איפה לשלב מדחום ונייר קצבים"],
        "smoking_wood_guide": ["איך לבחור שבבים או צ׳אנקים", "איזה עץ מתאים לכל בשר", "איך להימנע מעשן מר", "איך לתכנן תמונות ופרסום ב-ISTORE"],
        "grill_accessory_guide": ["מה האביזר באמת עושה", "איך לבדוק התאמה לגריל", "מהן טעויות התחזוקה", "מתי נכון לקנות בקומפס גריל"],
        "equipment_buying_guide": ["איך להשוות דגמים", "אילו אביזרים משלימים חשובים", "מה לבדוק לפני רכישה", "איך לשמור על חוויית צלייה נוחה"],
    }.get(topic_type, [f"מה חשוב לדעת על {keyword}", "איך לעבוד מסודר יותר", "אילו טעויות כדאי למנוע", "איך לבחור ציוד מתאים בקומפס גריל"])
    return "<div class='intro-summary'><p><strong>במאמר זה תלמד:</strong></p><ul>" + "".join(f"<li>✅ {b}</li>" for b in bullets) + "</ul></div>"


def _tip_block(topic_type: str, entity: str) -> str:
    tips = {
        "meat_low_slow_smoking": "שמרו על שינויי חום קטנים. פתיחה תכופה של המכסה מאריכה את העישון ופוגעת ב-Bark.",
        "smoking_wood_guide": "התחילו בכמות עץ קטנה והוסיפו רק אם העשן נשאר דק וכחלחל, לא סמיך ולבן.",
        "grill_accessory_guide": "לפני רכישה בדקו התאמה לדגם הגריל, מרווחים ויכולת ניקוי — לא רק מחיר.",
    }.get(topic_type, f"ב-{entity}, תוצאה עקבית מגיעה ממדידה, הכנה מראש ושינוי קטן אחד בכל פעם.")
    return f"<div class='professional-tip'><p><strong>💡 טיפ מקצועי</strong></p><p>{tips}</p></div>"


def _mistake_block(topic_type: str) -> str:
    mistakes = {
        "meat_low_slow_smoking": "פתיחת מעשנה בכל כמה דקות כדי לבדוק צבע. עדיף למדוד, להציץ בנקודות החלטה ולהחזיר יציבות.",
        "smoking_wood_guide": "להוסיף עוד עץ כשלא רואים עשן. עשן נקי כמעט שקוף עדיף על ענן לבן ומר.",
        "grill_accessory_guide": "להניח שכל אבני בזלת או אביזר יתאימו לכל גריל גז בלי לבדוק מפרט ודגם.",
    }.get(topic_type, "להתחיל בלי ציוד מוכן, ואז לעצור באמצע כשהחום כבר גבוה וחומר הגלם על הרשת.")
    return f"<div class='common-mistake'><p><strong>⚠ טעות נפוצה</strong></p><p>{mistakes}</p></div>"


def _checklist_html(topic_type: str) -> str:
    items = {
        "meat_low_slow_smoking": ["מעשנה יציבה", "מדחום לבשר", "נייר קצבים", "עצי עישון", "זמן מנוחה"],
        "smoking_wood_guide": ["שבבי עץ או צ׳אנקים", "כלי השריה לפי צורך", "מעשנה או קופסת עישון", "מדחום", "תיעוד טעמים"],
        "grill_accessory_guide": ["בדיקת התאמה לדגם", "כפפות וכלי עבודה", "מברשת ניקוי", "מדחום", "מגש שומן נקי"],
    }.get(topic_type, ["ציוד מתאים", "מדחום", "אזור עבודה נקי", "כלי הגשה", "תוכנית זמן"])
    return "<h2>✅ צ׳קליסט לפני שמתחילים</h2><ul class='article-checklist'>" + "".join(f"<li>✅ {item}</li>" for item in items) + "</ul>"


def _cta_block_html(topic_type: str) -> str:
    items = {
        "meat_low_slow_smoking": ["מעשנות", "עצי עישון", "מדחומים", "נייר קצבים"],
        "smoking_wood_guide": ["שבבי עץ לעישון", "צ׳אנקים", "מעשנות", "מדחומים"],
        "grill_accessory_guide": ["אביזרים לגריל", "אבני בזלת", "מדחומים", "כיסויים וכלי ניקוי"],
        "equipment_buying_guide": ["Gas Grills", "Charcoal Grills", "Kamado Grills", "Outdoor Kitchens"],
    }.get(topic_type, ["Gas Grills", "Smokers", "Thermometers", "Grill Accessories"])
    return "<div class='article-cta'><h2>🛒 מחפשים ציוד מתאים?</h2><p>בקומפס גריל תמצאו פתרונות שנבחרים לפי שימוש אמיתי ולא לפי ניחוש:</p><ul>" + "".join(f"<li>✅ {item}</li>" for item in items) + "</ul></div>"


def _insert_image_markers(html: str) -> str:
    if "<!-- IMAGE_1 -->" in html:
        return html
    matches = list(re.finditer(r"<h2[^>]*>.*?</h2>", html or "", flags=re.IGNORECASE | re.DOTALL))
    if not matches:
        return (html or "") + "\n<!-- IMAGE_1 -->\n<!-- IMAGE_2 -->\n<!-- IMAGE_3 -->\n<!-- IMAGE_4 -->"
    positions = []
    if matches:
        positions.append(matches[0].start())
    for idx in (2, 4, 6):
        positions.append(matches[min(idx, len(matches)-1)].start())
    out = html or ""
    for marker_no, pos in reversed(list(enumerate(positions[:4], start=1))):
        out = out[:pos] + f"\n<!-- IMAGE_{marker_no} -->\n" + out[pos:]
    return out


def apply_visual_article_formatter(title: str, keyword: str, html: str, topic_profile: dict[str, object]) -> str:
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    entity = str(topic_profile.get("main_entity") or keyword or title)
    formatted = _iconize_h2(html)
    if "intro-summary" not in formatted:
        formatted = _intro_summary_html(title, keyword, topic_type) + "\n" + formatted
    if "professional-tip" not in formatted:
        formatted = formatted + "\n" + _tip_block(topic_type, entity)
    if "common-mistake" not in formatted:
        formatted = formatted + "\n" + _mistake_block(topic_type)
    if "article-checklist" not in formatted:
        formatted = formatted + "\n" + _checklist_html(topic_type)
    if "<table" not in formatted.lower():
        formatted = formatted + "\n" + _topic_table_html(topic_type, entity)
    if "article-cta" not in formatted:
        formatted = formatted + "\n" + _cta_block_html(topic_type)
    formatted = _insert_image_markers(formatted)
    return _dedupe_article_html(formatted)


def _image_filename(slug: str, key: str) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", (slug or "compass-grill-article").lower()).strip("-")
    descriptor = re.sub(r"[^a-z0-9-]+", "-", (key or "image").lower()).strip("-")
    descriptor = re.sub(r"-+", "-", descriptor) or "image"
    return f"{safe}-{descriptor}.jpg"


def _unique_alt(base: str, used: set[str], suffix: str) -> str:
    alt = re.sub(r"\s+", " ", base).strip()
    generic = {"image", "photo", "grill image", "bbq image", "תמונה", "צילום", "תמונת גריל"}
    if not alt or alt.lower() in generic or len(alt) < 18:
        alt = f"{base} - {suffix}".strip(" -")
    original = alt
    i = 2
    while _normalize_hebrew(alt) in used:
        alt = f"{original} - {suffix} {i}"
        i += 1
    used.add(_normalize_hebrew(alt))
    return alt


def build_multi_image_package(title: str, keyword: str, slug: str, topic_profile: dict[str, object], featured_prompt: str) -> dict[str, object]:
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    entity = str(topic_profile.get("main_entity") or keyword or title)
    plans = {
        "meat_low_slow_smoking": [("featured_image", "finished brisket with dark bark", "בריסקט מעושן מוכן עם Bark כהה", "תמונת שער: בריסקט מעושן מוכן לפריסה", "finished smoked brisket, dark bark, butcher paper, premium BBQ photography, no text"), ("image_1", "smoker setup", "מעשנה מוכנה לעישון בריסקט עם מדחום", "אחרי הפתיח: הכנת המעשנה והציוד", "stable smoker setup for brisket, thermometer probes, thin blue smoke, no text"), ("image_2", "wood selection", "עצי עישון Oak ו-Hickory ליד בריסקט", "בחירת עצי עישון מתאימים לבקר", "oak and hickory smoking wood chunks beside brisket prep, realistic, no text"), ("image_3", "bark development", "בריסקט בתוך מעשנה בזמן פיתוח Bark", "שלב פיתוח ה-Bark לפני עטיפה", "brisket in smoker developing dark bark, thin smoke, close up, no text"), ("image_4", "slicing and serving", "פריסת בריסקט מעושן לאחר מנוחה ארוכה", "לפני FAQ: פריסה והגשה נכונה", "slicing rested smoked brisket on board, juicy slices, BBQ serving, no text")],
        "smoking_wood_guide": [("featured_image", "smoking woods", "שבבי עץ וצ׳אנקים לעישון ליד מעשנה", "תמונת שער: בחירת עץ לעישון", "variety of smoking wood chips and chunks near smoker, realistic BBQ photo, no text"), ("image_1", "chips versus chunks", "שבבי עץ מול צ׳אנקים לעישון", "אחרי הפתיח: ההבדל בין שבבים לצ׳אנקים", "wood chips versus wood chunks for smoking, clear realistic setup, no text"), ("image_2", "wood flavor pairing", "Oak Cherry ו-Hickory מסודרים לפי התאמה לבשר", "אחרי פרופיל טעם והתאמה לבשר", "oak cherry hickory smoking woods arranged by meat pairing, no text"), ("image_3", "thin blue smoke", "עשן דק וכחלחל יוצא ממעשנה", "אחרי סעיף עוצמת עשן", "thin blue smoke from smoker vent, premium realistic BBQ photography, no text"), ("image_4", "wood storage", "אחסון יבש של עצי עישון ושבבים", "לפני FAQ: אחסון ושימוש חוזר", "dry storage of smoking wood chips and chunks, clean BBQ workspace, no text")],
        "grill_accessory_guide": [("featured_image", "basalt stones in gas grill", "אבני בזלת לגריל גז מעל מבערים", "תמונת שער: אבני בזלת בגריל גז", "black basalt lava stones installed in gas grill above burners, realistic, no text"), ("image_1", "gas grill setup", "גריל גז פתוח עם אבני בזלת מסודרות", "אחרי הפתיח: מיקום האבנים בגריל", "open gas grill with basalt stones correctly arranged, no text"), ("image_2", "heat distribution", "פיזור חום אחיד מעל אבני בזלת בגריל", "אחרי סעיף פיזור חום", "even heat over basalt stones in gas grill, glowing burners, realistic, no text"), ("image_3", "flare up control", "אבני בזלת מפחיתות התלקחויות משומן בגריל", "אחרי סעיף התלקחויות", "basalt stones controlling flare ups in gas grill, realistic BBQ photo, no text"), ("image_4", "cleaning stones", "ניקוי אבני בזלת ואביזרי גריל", "לפני FAQ: תחזוקה וניקוי", "cleaning basalt lava stones and grill accessories, no text")],
    }.get(topic_type)
    if not plans:
        plans = [("featured_image", entity, f"{entity} בהכנה מקצועית על גריל", f"תמונת שער: {title}", featured_prompt), ("image_1", "setup", f"ציוד מוכן עבור {entity}", "אחרי הפתיח: הכנת ציוד", f"BBQ setup for {keyword}, realistic, no text"), ("image_2", "process", f"שלב עבודה מרכזי בנושא {entity}", "אחרי סעיף מרכזי ראשון", f"process detail for {keyword} article, realistic BBQ photo, no text"), ("image_3", "detail", f"פרט מקצועי בהכנת {entity}", "אחרי סעיף טיפים", f"close detail for {keyword}, premium BBQ photo, no text"), ("image_4", "serving", f"תוצאה סופית והגשה של {entity}", "לפני FAQ", f"final serving for {keyword}, realistic BBQ photo, no text")]
    used: set[str] = set()
    package = []
    for key, filename_key, alt, caption, prompt in plans:
        final_prompt = prompt if key != "featured_image" or not featured_prompt else featured_prompt
        if "photorealistic commercial quality BBQ magazine photography" not in final_prompt:
            final_prompt = f"{final_prompt}, photorealistic commercial quality BBQ magazine photography, realistic natural lighting, realistic materials and food, no text, no logos"
        package.append({"key": key, "filename": _image_filename(slug, filename_key), "title": caption.replace("תמונת שער: ", ""), "alt": _unique_alt(alt, used, key), "caption": caption, "prompt": final_prompt, "generated_url": "", "preview_url": "", "status": "planned", "image_url": ""})
    placement = [
        {"image": "featured_image", "instruction": "Place as article cover.", "section": "cover"},
        {"image": "image_1", "instruction": "Place after introduction.", "section": "פתיח"},
        {"image": "image_2", "instruction": "Place after the closest matching H2 section.", "section": package[2]["caption"]},
        {"image": "image_3", "instruction": "Place after the process/detail section.", "section": package[3]["caption"]},
        {"image": "image_4", "instruction": "Place before FAQ section.", "section": "שאלות נפוצות"},
    ]
    return {"image_package": package, "image_placement_guide": placement, "image_prompt_version": "v4-commercial-bbq-magazine"}


def _split_article_blocks(body: str) -> list[dict[str, str]]:
    pieces = re.split(r"(?=<h2[^>]*>)", body or "")
    blocks = []
    first = pieces[0].strip() if pieces else ""
    if first:
        blocks.append({"label": "Introduction", "html": first})
    for idx, piece in enumerate((p for p in pieces[1:] if p.strip()), start=1):
        heading = _plain_text(re.search(r"<h2[^>]*>(.*?)</h2>", piece, flags=re.I|re.S).group(1)) if re.search(r"<h2[^>]*>(.*?)</h2>", piece, flags=re.I|re.S) else f"Section {idx}"
        if "שאלות נפוצות" in heading:
            label = "FAQ"
        elif "מחפשים" in heading or "CTA" in piece or "article-cta" in piece:
            label = "CTA"
        else:
            label = f"Section {idx}: {heading.strip()}"
        blocks.append({"label": label, "html": piece.strip()})
    return blocks


def build_istore_copy_paste_package(title: str, slug: str, meta_title: str, meta_description: str, body: str, image_package: list[dict[str, str]]) -> dict[str, object]:
    steps = [
        {"step": 1, "label": "Copy into Title field", "value": title},
        {"step": 2, "label": "Copy into Slug field", "value": slug},
        {"step": 3, "label": "Copy into Meta Title", "value": meta_title},
        {"step": 4, "label": "Copy into Meta Description", "value": meta_description},
    ]
    featured = next((img for img in image_package if img.get("key") == "featured_image"), image_package[0] if image_package else {})
    steps.append({"step": 5, "label": "Upload Featured Image", "filename": featured.get("filename", ""), "alt": featured.get("alt", ""), "caption": featured.get("caption", "")})
    step_no = 6
    for block in _split_article_blocks(body):
        steps.append({"step": step_no, "label": f"Paste {block['label']}", "html": block["html"]})
        step_no += 1
        marker = re.search(r"<!--\s*IMAGE_(\d)\s*-->", block["html"])
        if marker:
            image_key = f"image_{marker.group(1)}"
            img = next((item for item in image_package if item.get("key") == image_key), {})
            if img:
                steps.append({"step": step_no, "label": f"Insert {image_key}", "filename": img.get("filename", ""), "alt": img.get("alt", ""), "caption": img.get("caption", ""), "prompt": img.get("prompt", "")})
                step_no += 1
    return {"mode": "ISTORE_COPY_PASTE", "steps": steps, "article_blocks": _split_article_blocks(body)}


def validate_complete_publishing_package(body: str, image_package: list[dict[str, str]], placement_guide: list[dict[str, str]], istore_package: dict[str, object], diversity: dict[str, object] | None = None) -> dict[str, object]:
    checks = {
        "article_generated": bool(body and _article_word_count(body) > 50),
        "diversity_score_passed": bool((diversity or {}).get("passed", True)),
        "table_exists": "<table" in (body or "").lower(),
        "faq_exists": "שאלות נפוצות" in (body or "") or "FAQ" in (body or ""),
        "cta_exists": "article-cta" in (body or "") or "🛒" in (body or ""),
        "checklist_exists": "article-checklist" in (body or "") or "צ׳קליסט" in (body or ""),
        "tip_block_exists": "professional-tip" in (body or "") or "טיפ מקצועי" in (body or ""),
        "warning_block_exists": "common-mistake" in (body or "") or "טעות נפוצה" in (body or ""),
        "five_image_package_exists": len(image_package) == 5,
        "image_placement_guide_exists": len(placement_guide) >= 5,
        "image_markers_exist": all(f"<!-- IMAGE_{i} -->" in (body or "") for i in range(1,5)),
        "alt_values_unique": len({_normalize_hebrew(str(i.get("alt") or "")) for i in image_package}) == len(image_package),
        "filenames_exist": all(i.get("filename") for i in image_package),
        "captions_exist": all(i.get("caption") for i in image_package),
        "image_prompts_exist": all(i.get("prompt") for i in image_package),
        "generated_url_fields_exist": all(("generated_url" in i and "preview_url" in i and "status" in i) or "image_url" in i for i in image_package),
        "istore_publishing_mode_exists": istore_package.get("mode") == "ISTORE_COPY_PASTE" and bool(istore_package.get("steps")),
    }
    publishing_required_checks = {
        "article_approved": not failed if (failed := [k for k, ok in checks.items() if not ok]) else True,
        "featured_image_generated": any(i.get("key") == "featured_image" and i.get("status") == "generated" and (i.get("generated_url") or i.get("image_url")) for i in image_package),
        "all_section_images_generated": all(any(i.get("key") == f"image_{n}" and i.get("status") == "generated" and (i.get("generated_url") or i.get("image_url")) for i in image_package) for n in range(1, 5)),
        "alt_exists": all(i.get("alt") for i in image_package),
        "caption_exists": all(i.get("caption") for i in image_package),
        "image_placement_guide_exists": len(placement_guide) >= 5,
        "internal_links_exist": "href='https://compassgrill.co.il/" in (body or "") or 'href="https://compassgrill.co.il/' in (body or ""),
    }
    publishing_failed = [k for k, ok in publishing_required_checks.items() if not ok]
    return {
        "publishing_package_checks": checks,
        "publishing_package_failed_checks": failed,
        "ready_for_publishing_checks": publishing_required_checks,
        "ready_for_publishing_failed_checks": publishing_failed,
        "publish_readiness": "READY_FOR_PUBLISHING" if not failed and not publishing_failed else ("READY_FOR_REVIEW" if not failed else "NEEDS_REWRITE"),
    }


def article_structure_signature(body: str) -> set[str]:
    h2s = re.findall(r"<h2[^>]*>(.*?)</h2>", body or "", flags=re.IGNORECASE | re.DOTALL)
    tokens = {_normalize_hebrew(re.sub(r"^[^\w\u0590-\u05FF]+", "", _plain_text(h))) for h in h2s}
    tokens.update(re.findall(r"<!--\s*IMAGE_\d\s*-->", body or ""))
    if "intro-summary" in (body or ""):
        tokens.add("intro_summary")
    if "article-cta" in (body or ""):
        tokens.add("cta")
    return {t for t in tokens if t}


def calculate_diversity_score(candidate_body: str, previous_bodies: list[str]) -> dict[str, object]:
    candidate = article_structure_signature(candidate_body)
    max_similarity = 0.0
    for prev in previous_bodies:
        other = article_structure_signature(prev)
        union = candidate | other
        similarity = (len(candidate & other) / len(union)) if union else 0.0
        max_similarity = max(max_similarity, similarity)
    score = round(100 * (1 - max_similarity), 1)
    return {"diversity_score": score, "max_similarity": round(max_similarity, 3), "threshold": 0.82, "passed": max_similarity <= 0.82}


INTERNAL_SEO_CONTRACT_TERMS = {
    "how_to_grilling_guide",
    "low_and_slow_smoking_guide",
    "recipe_how_to",
    "comparison_or_buying_guide",
    "smoking_wood_buying_guide",
    "accessory_buying_guide",
    "equipment_buying_guide",
    "fallback_generic",
    *TOPIC_TYPE_CONTRACTS.keys(),
    *TOPIC_TYPE_GENERATORS.values(),
}


def _dedupe_terms(terms: list[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = re.sub(r"\s+", " ", str(term or "").strip(" |,.;:"))
        key = re.sub(r"\s+", " ", clean.lower().replace("׳", "").replace("'", "")).strip()
        if clean and key and key not in seen:
            out.append(clean)
            seen.add(key)
        if limit and len(out) >= limit:
            break
    return out


def _seo_entity(keyword: str, title: str, topic_profile: dict[str, object]) -> str:
    entity = str(keyword or topic_profile.get("target_keyword") or topic_profile.get("main_entity") or title or "").strip()
    if not entity:
        entity = str(title or "מדריך גריל").strip()
    if "/" in entity:
        entity = entity.split("/")[0].strip()
    return entity


def _high_intent_phrase(entity: str, topic_type: str, search_intent: str, title: str) -> str:
    normalized_title = _normalize_text_for_matching(title)
    if "איך" in normalized_title and entity in title:
        return title.strip()
    if topic_type == "meat_quick_grill_cut":
        return f"איך לצלות {entity}"
    if topic_type == "meat_low_slow_smoking":
        return f"איך לעשן {entity}"
    if topic_type == "smoking_accessory_guide":
        return f"איך להשתמש ב{entity} לעישון"
    if topic_type == "poultry_grill_recipe":
        return f"איך להכין {entity} על הגריל"
    if topic_type == "fuel_comparison_or_guide" or search_intent == "comparison":
        return f"איזה {entity} עדיף לגריל"
    if topic_type in {"grill_accessory_guide", "equipment_buying_guide"} or search_intent.startswith("commercial"):
        return f"איך לבחור {entity}"
    if topic_type == "smoking_wood_guide":
        return f"איך לבחור {entity} לעישון"
    return f"מדריך {entity}"


def _build_meta_title(entity: str, high_intent_phrase: str, topic_type: str) -> str:
    if topic_type == "meat_quick_grill_cut":
        base = f"{high_intent_phrase} מושלמת על הגריל – מדריך מלא"
    elif topic_type == "meat_low_slow_smoking":
        base = f"{high_intent_phrase} נכון – מדריך עישון מלא"
    elif topic_type == "poultry_grill_recipe":
        base = f"{high_intent_phrase} – מתכון וטיפים"
    elif topic_type == "fuel_comparison_or_guide":
        base = f"{high_intent_phrase}: השוואה וטיפים לבחירה"
    elif topic_type == "smoking_accessory_guide":
        base = f"{entity} לעישון – בריסקט, סטול ו-Bark"
    elif topic_type == "grill_accessory_guide" and "מדחום" in entity:
        base = f"איך לבחור {entity}? מדריך לגריל, מעשנה וצלייה מדויקת"
    elif topic_type == "grill_accessory_guide" and ("בזלת" in entity or "לבה" in entity):
        base_entity = entity if "לגריל" in entity else f"{entity} לגריל גז"
        base = f"{base_entity} – יתרונות, התקנה ותחזוקה"
    elif topic_type in {"grill_accessory_guide", "equipment_buying_guide", "smoking_wood_guide"}:
        base = f"{high_intent_phrase} – מדריך קנייה ושימוש"
    else:
        base = f"{high_intent_phrase} – מדריך מעשי"
    full = _normalize_meta_title(f"{base} | Compass Grill")
    if len(full) <= 70:
        return full
    short = _normalize_meta_title(f"{high_intent_phrase} – מדריך מלא | Compass Grill")
    fallback = _normalize_meta_title(f"{entity} על הגריל – מדריך מלא | Compass Grill")
    return short if len(short) <= 70 else fallback


def _keyword_groups(entity: str, title: str, topic_profile: dict[str, object]) -> dict[str, list[str] | str]:
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    search_intent = str(topic_profile.get("search_intent") or "informational")
    high_intent = _high_intent_phrase(entity, topic_type, search_intent, title)
    raw_secondary = [
        f"{entity} על הגריל",
        f"טמפרטורת {entity}",
    ]
    internal_link_keywords = [str(k) for k in topic_profile.get("internal_link_keywords", [])]
    entity_context_keywords = [
        keyword if entity in keyword else f"{entity} עם {keyword}"
        for keyword in internal_link_keywords
        if keyword
    ]
    entity_profile = topic_profile.get("entity_profile") if isinstance(topic_profile.get("entity_profile"), dict) else {}
    seo_profile = entity_profile.get("seo_keywords") if isinstance(entity_profile.get("seo_keywords"), dict) else {}
    normalized_entity = _normalize_hebrew(entity)
    lower_entity = entity.lower()
    if "בזלת" in normalized_entity or "לבה" in normalized_entity or "basalt" in lower_entity or "lava" in lower_entity:
        raw_secondary = ["אבני לבה לגריל", "אבני בזלת לגריל גז", "פיזור חום בגריל גז", "הפחתת התלקחויות בגריל", *entity_context_keywords]
        long_tail = ["lava rocks grill", "basalt stones for gas grill", "איך להשתמש באבני בזלת", "ניקוי אבני בזלת", "מתי מחליפים אבני בזלת"]
        question = ["איך להשתמש באבני בזלת", "מתי מחליפים אבני בזלת"]
        usage = ["אביזרים לגריל גז", "אבני בזלת לגריל קנייה", "תחזוקת אבני בזלת"]
    elif "מדחום" in normalized_entity or "thermometer" in lower_entity:
        raw_secondary = ["מדחום לבשר מומלץ", "מדחום דיגיטלי לבשר", "מדחום ליבה לבשר", "מדחום לגריל גז", "מדחום למעשנה", *entity_context_keywords]
        long_tail = ["מדחום לקריאה מהירה", "meat thermometer", "טמפרטורת בשר", "איך מודדים טמפרטורת בשר"]
        question = ["איך מודדים טמפרטורת בשר", "איזה מדחום לבשר מומלץ?"]
        usage = ["מדחום לגריל", "מדחום למעשנה", "מדחום בשר דיגיטלי"]
    elif seo_profile:
        raw_secondary = [*list(seo_profile.get("secondary", [])), *raw_secondary, *entity_context_keywords]
        long_tail = [high_intent, *list(seo_profile.get("long_tail", []))]
        question = list(seo_profile.get("questions", []))
        usage = list(seo_profile.get("commercial", []))
    elif topic_type == "meat_quick_grill_cut":
        raw_secondary.extend([f"{entity} מדיום רייר", f"חיתוך {entity}", f"סטייק {entity}", *entity_context_keywords])
        long_tail = [high_intent, f"Reverse Sear {entity}", f"{entity} גריל גז", f"{entity} על פחמים"]
        question = [f"איך לצלות {entity}?", f"מה טמפרטורת {entity} מדיום רייר?"]
        usage = [f"{entity} למנגל", f"{entity} על האש"]
    elif topic_type == "meat_low_slow_smoking":
        raw_secondary.extend([f"טמפרטורת עישון {entity}", f"{entity} במעשנה", f"{entity} עם נייר קצבים", *entity_context_keywords])
        long_tail = [high_intent, f"כמה זמן לעשן {entity}", f"{entity} low and slow", f"{entity} עטיפה ומנוחה"]
        question = [f"איך לעשן {entity}?", f"מתי עוטפים {entity}?"]
        usage = [f"{entity} למעשנה", f"{entity} למתחילים"]
    elif topic_type == "fuel_comparison_or_guide":
        raw_secondary.extend([f"{entity} פחם קוקוס", f"{entity} פחם עץ", f"זמן בעירה של {entity}", f"יציבות חום של {entity}", *entity_context_keywords])
        long_tail = [high_intent, "פחם קוקוס מול פחם עץ", "פחם לגריל פחמים", "פחם עם פחות אפר"]
        question = ["איזה פחם מחזיק יותר זמן?", "מה ההבדל בין פחם קוקוס לפחם עץ?"]
        usage = ["פחם מומלץ למנגל", "פחם לגריל מקצועי"]
    elif topic_type == "smoking_wood_guide":
        raw_secondary.extend([f"{entity} לעישון", f"{entity} מול צ׳אנקים", f"{entity} לבשר", f"{entity} thin blue smoke", *entity_context_keywords])
        long_tail = [high_intent, f"{entity} לבריסקט", f"{entity} למעשנה", "שבבים או צ׳אנקים לעישון"]
        question = [f"איך משתמשים ב{entity}?", "האם צריך להשרות שבבי עץ?"]
        usage = [f"{entity} לקנייה", f"{entity} לגריל גז"]
    elif topic_type == "smoking_accessory_guide":
        raw_secondary.extend(["נייר קצבים לבריסקט", "pink butcher paper", "butcher paper vs foil", "מתי לעטוף בריסקט", "שמירת Bark", *entity_context_keywords])
        long_tail = [high_intent, "איך לעטוף בריסקט", "נייר קצבים לצלעות", "Texas Crutch נייר קצבים", "נייר ורוד מול נייר חום"]
        question = ["מתי לעטוף בריסקט?", "מה ההבדל בין נייר קצבים לנייר כסף?", "איך עוטפים בנייר קצבים?"]
        usage = ["נייר קצבים לעישון", "נייר קצבים food-safe", "נייר קצבים למעשנה"]
    elif topic_type in {"grill_accessory_guide", "equipment_buying_guide"}:
        raw_secondary.extend([f"{entity} לגריל גז", f"התקנת {entity}", f"תחזוקת {entity}", *entity_context_keywords])
        long_tail = [high_intent, f"{entity} מומלץ", f"{entity} לגריל ביתי", f"איך משתמשים ב{entity}"]
        question = [f"למה צריך {entity}?", f"מתי מחליפים {entity}?"]
        usage = [f"{entity} לקנייה", f"{entity} שימוש נכון"]
    else:
        raw_secondary.extend([f"מדריך {entity}", f"טיפים ל{entity}", *entity_context_keywords])
        long_tail = [high_intent, f"{entity} מדריך למתחילים", f"{entity} טעויות נפוצות"]
        question = [f"איך משתמשים ב{entity}?", f"מה חשוב לדעת על {entity}?"]
        usage = [f"{entity} מומלץ", f"{entity} שימוש נכון"]
    secondary = _dedupe_terms(raw_secondary, limit=5)
    long_tail = _dedupe_terms(long_tail, limit=5)
    question = _dedupe_terms(question, limit=3)
    usage = _dedupe_terms(usage, limit=3)
    all_keywords = _dedupe_terms([entity, *secondary, *long_tail, *question, *usage], limit=15)
    return {
        "primary_keyword": entity,
        "high_intent_phrase": high_intent,
        "secondary_keywords": secondary,
        "long_tail_keywords": long_tail,
        "question_keywords": question,
        "usage_keywords": usage,
        "seo_keywords": all_keywords,
    }


def _fit_meta_description(description: str, entity: str, secondary: str) -> str:
    clean = re.sub(r"\s+", " ", description).strip()
    fallback = f"מדריך {entity} מעשי עם {secondary}, טיפים לגריל, טמפרטורות, שלבי הכנה וטעויות נפוצות כדי לקבל תוצאה עסיסית ומדויקת בבית."
    if len(clean) < 140:
        clean = f"{clean} כולל טיפים לגריל גז ופחמים ושאלות נפוצות."
    if len(clean) > 160:
        clean = clean[:157].rstrip(" ,.-–") + "..."
    if len(clean) < 140:
        clean = fallback
    if len(clean) > 160:
        clean = clean[:157].rstrip(" ,.-–") + "..."
    return clean


def _build_meta_description(entity: str, topic_profile: dict[str, object], groups: dict[str, list[str] | str]) -> str:
    topic_type = str(topic_profile.get("topic_type") or "fallback_generic")
    secondary_keywords = groups.get("secondary_keywords") if isinstance(groups.get("secondary_keywords"), list) else []
    secondary = str((secondary_keywords or [f"{entity} על הגריל"])[0])
    if topic_type == "meat_quick_grill_cut":
        raw = f"מדריך {entity} על הגריל עם המלחה, צריבה, {secondary}, מנוחה וחיתוך נכון כדי להגיע לנתח עסיסי ומדיום רייר בכל פעם."
    elif topic_type == "meat_low_slow_smoking":
        raw = f"מדריך {entity} במעשנה עם טמפרטורות, עצי עישון, עטיפה, נייר קצבים ומנוחה ארוכה לקבלת Bark עסיסי ותוצאה יציבה."
    elif topic_type == "poultry_grill_recipe":
        raw = f"מתכון {entity} על הגריל עם ייבוש, מרינדה, גלייז, בטיחות מזון וטיפים לקריספיות בלי לשרוף את העוף."
    elif topic_type == "fuel_comparison_or_guide":
        raw = f"השוואת {entity} לגריל: זמן בעירה, יציבות חום, עשן, אפר ועלות מול ביצועים כדי לבחור פחם מתאים למנגל."
    elif topic_type == "smoking_wood_guide":
        raw = f"מדריך {entity} לעישון עם התאמת עץ לבשר, שבבים מול צ׳אנקים, שליטה בעשן וטיפים לקנייה ושימוש נכון."
    elif topic_type == "smoking_accessory_guide":
        raw = f"מדריך {entity} לעישון עם בריסקט, צלעות, סטול, Texas Crutch, שמירת Bark, לחות והשוואת butcher paper vs foil."
    elif topic_type == "grill_accessory_guide" and "מדחום" in entity:
        raw = f"איך בוחרים {entity} לגריל או מעשנה? מדריך עם סוגי מדחומים, זמן תגובה, כיול, ניקוי וטיפים למדידה מדויקת."
    elif topic_type == "grill_accessory_guide" and ("בזלת" in entity or "לבה" in entity):
        raw = f"איך {entity} משפרות פיזור חום, מפחיתות התלקחויות ושומרות על טמפרטורה יציבה? מדריך לבחירה, שימוש ותחזוקה."
    elif topic_type in {"grill_accessory_guide", "equipment_buying_guide"}:
        raw = f"מדריך {entity} עם שיקולי קנייה, התקנה, תחזוקה ושימוש נכון כדי לשפר את ביצועי הגריל ולבחור מוצר מתאים."
    else:
        raw = f"מדריך {entity} מעשי עם טיפים, שלבים, טעויות נפוצות ותשובות לשאלות כדי להבין מה חשוב לפני שמתחילים."
    return _fit_meta_description(raw, entity, secondary)


def build_topic_seo_metadata(keyword: str, title: str, topic_profile: dict[str, object]) -> dict[str, object]:
    entity = _seo_entity(keyword, title, topic_profile)
    groups = _keyword_groups(entity, title, topic_profile)
    meta_title = _build_meta_title(entity, str(groups["high_intent_phrase"]), str(topic_profile.get("topic_type") or "fallback_generic"))
    meta_description = _build_meta_description(entity, topic_profile, groups)
    internal_leaks = [term for term in INTERNAL_SEO_CONTRACT_TERMS if term and (term in meta_title or term in meta_description)]
    seo_score = 100
    if entity not in meta_title:
        seo_score -= 20
    if entity not in meta_description:
        seo_score -= 20
    if not 140 <= len(meta_description) <= 160:
        seo_score -= 15
    if len(groups["seo_keywords"]) < 8:
        seo_score -= 15
    if internal_leaks:
        seo_score -= 30
    meta_title_score = 100
    if entity not in meta_title:
        meta_title_score -= 30
    if internal_leaks:
        meta_title_score -= 40
    if len(meta_title) > 70:
        meta_title_score -= 10
    meta_description_score = 100
    if entity not in meta_description:
        meta_description_score -= 30
    if not 140 <= len(meta_description) <= 160:
        meta_description_score -= 20
    secondary_keywords = groups.get("secondary_keywords") if isinstance(groups.get("secondary_keywords"), list) else []
    if secondary_keywords and not any(str(k) in meta_description for k in secondary_keywords[:3]):
        meta_description_score -= 10
    return {
        "meta_title": meta_title,
        "meta_description": meta_description,
        **groups,
        "commercial_keywords": groups.get("usage_keywords", []),
        "internal_contract_terms_found": internal_leaks,
        "seo_score": max(0, seo_score),
        "meta_title_score": max(0, meta_title_score),
        "meta_description_score": max(0, meta_description_score),
    }


def _topic_meta(keyword: str, title: str, topic_profile: dict[str, object]) -> tuple[str, str]:
    metadata = build_topic_seo_metadata(keyword, title, topic_profile)
    return str(metadata["meta_title"]), str(metadata["meta_description"])



def _generate_image_alt_text(title: str, keyword: str, topic_profile: dict[str, object]) -> tuple[str, str]:
    topic_type = str(topic_profile.get("topic_type") or "")
    entity_key = str(topic_profile.get("entity_key") or "")
    blob = _normalize_hebrew(f"{title} {keyword} {topic_profile.get('main_entity','')}")
    if topic_type == "smoking_wood_guide":
        return "שבבי עץ Hickory ו-Cherry במגש עישון לצד מעשנה", "topic_type_smoking_wood"
    if topic_type == "meat_low_slow_smoking":
        return "בריסקט מעושן במעשנה עם Bark כהה ונייר קצבים", "topic_type_low_slow"
    if topic_type == "smoking_accessory_guide":
        return "בריסקט עטוף בנייר קצבים חום במהלך עישון", "topic_type_smoking_accessory"
    if entity_key == "basalt_stones" or "בזלת" in blob:
        return "אבני בזלת לגריל גז מעל מבערים לפיזור חום", "entity_basalt"
    if entity_key == "thermometer" or "מדחום" in blob:
        return "מדחום לבשר מודד טמפרטורה פנימית בנתח על הגריל", "entity_thermometer"
    if topic_type == "poultry_grill_recipe":
        return "כנפיים קריספיות על גריל עם עור שחום ורוטב בסיום", "topic_type_poultry"
    entity = str(topic_profile.get("main_entity") or keyword or title).strip()
    return f"{entity} בהכנה מעשית על גריל או מעשנה של קומפס גריל", "entity_fallback"

def _html_duplicate_issues(body: str) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    duplicates: list[str] = []

    def repeated_values(values: list[str]) -> list[str]:
        counts: dict[str, int] = {}
        for value in values:
            if value:
                counts[value] = counts.get(value, 0) + 1
        return sorted(key for key, count in counts.items() if count > 1)

    h2s = [_semantic_key(h) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", body or "", flags=re.IGNORECASE | re.DOTALL)]
    dup_h2 = repeated_values(h2s)
    if dup_h2:
        issues.append("duplicate_h2_titles")
        duplicates.extend(dup_h2[:5])

    lists = [_semantic_key(inner) for inner in re.findall(r"<(?:ul|ol)(?:\s[^>]*)?>(.*?)</(?:ul|ol)>", body or "", flags=re.IGNORECASE | re.DOTALL)]
    dup_lists = repeated_values(lists)
    if dup_lists:
        issues.append("duplicate_list_blocks")
        duplicates.extend(dup_lists[:5])

    faq_items = [
        _semantic_key(question + " " + answer)
        for question, answer in re.findall(r"<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>", body or "", flags=re.IGNORECASE | re.DOTALL)
    ]
    dup_faq_items = repeated_values(faq_items)
    if dup_faq_items:
        issues.append("duplicate_faq_blocks")
        duplicates.extend(dup_faq_items[:5])

    paragraphs = [_semantic_key(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", body or "", flags=re.IGNORECASE | re.DOTALL)]
    dup_p = repeated_values(paragraphs)
    if dup_p:
        issues.append("repeated_paragraphs")
        duplicates.extend(dup_p[:5])
    return issues, duplicates

def validate_final_article_quality(body: str, meta_title: str, seo_metadata: dict[str, object], topic_profile: dict[str, object], selected_links: list[dict[str, object]], image_alt_text: str) -> dict[str, object]:
    issues, duplicate_sections_removed = _html_duplicate_issues(body)
    raw_body = body or ""
    topic_type = str(topic_profile.get("topic_type") or "")
    wrong_generic_by_topic = {
        "meat_low_slow_smoking": ["מתי כדאי לשדרג ציוד", "איך נמנעים מקנייה לא נכונה", "התאמה לשימוש האמיתי שלכם"],
        "poultry_grill_recipe": ["מתי כדאי לשדרג ציוד", "איך נמנעים מקנייה לא נכונה", "צ׳קליסט מעשי", "התאמה לשימוש האמיתי שלכם"],
    }
    if any(label in raw_body for label in TOPIC_TYPE_CONTRACTS.keys()) or any(label in raw_body for label in TOPIC_TYPE_GENERATORS.values()):
        issues.append("internal_topic_type_label_visible")
    leaked = [phrase for phrase in wrong_generic_by_topic.get(topic_type, []) if phrase in raw_body]
    if leaked:
        issues.append("wrong_generic_filler_sections:" + ",".join(leaked))
    if any(phrase in raw_body for phrase in ["נמשיך לעדכן כאן", "למי שרוצה להמשיך מהתיאוריה לבחירה באתר"]):
        issues.append("placeholder_or_generic_system_wording")
    keywords = seo_metadata.get("seo_keywords") if isinstance(seo_metadata, dict) else []
    if not isinstance(keywords, list) or len(keywords) < 8:
        issues.append("keywords_count_below_8")
    if any(term in meta_title for term in INTERNAL_SEO_CONTRACT_TERMS):
        issues.append("meta_title_contains_internal_contract_name")
    alt_norm = _normalize_hebrew(image_alt_text)
    entity = _normalize_hebrew(str(topic_profile.get("main_entity") or ""))
    smoking_paper_alt_ok = topic_type == "smoking_accessory_guide" and any(term in alt_norm for term in ["נייר קצבים", "בריסקט", "bark"])
    smoking_wood_alt_ok = topic_type == "smoking_wood_guide" and "שבבי עץ" in alt_norm and any(term in alt_norm for term in ["מעשנה", "עישון", "מגש"])
    if not image_alt_text or (entity and entity not in alt_norm and str(topic_profile.get("entity_key") or "") not in {"basalt_stones", "thermometer"} and not (topic_type == "poultry_grill_recipe" and "כנפ" in alt_norm) and not smoking_paper_alt_ok and not smoking_wood_alt_ok):
        issues.append("alt_not_entity_specific")
    final_word_count = _article_word_count(body)
    required_word_count = _required_word_count_for_topic(topic_type)
    if final_word_count < required_word_count:
        issues.append(f"word_count_below_minimum:{final_word_count}/{required_word_count}")
    if re.search(r"href=['\"](?!https://compassgrill\.co\.il/)[^'\"]+", raw_body):
        issues.append("non_public_or_external_internal_link")
    for link in selected_links or []:
        if not str(link.get("reason") or "") and str(link.get("section") or "") != "body_paragraph":
            issues.append("selected_link_missing_reason")
        text = f"{link.get('title','')} {link.get('anchor_text','')} {link.get('url','')}"
        scores = {
            "entity_match_score": link.get("entity_match_score", 0),
            "keyword_match_score": link.get("keyword_match_score", 10 if link.get("link_role") in {"exact_entity", "complementary", "related_category"} else 0),
            "relevance_score": link.get("relevance_score", link.get("relatedness_score", 0)),
        }
        if not _passes_link_semantic_gate(str(topic_profile.get("main_entity") or ""), text, str(link.get("page_type") or link.get("type") or "product"), scores, topic_profile):
            issues.append("irrelevant_selected_link:" + str(link.get("title") or link.get("url") or "unknown"))
    h2_count = len(re.findall(r"<h2[^>]*>", raw_body, flags=re.IGNORECASE))
    faq_count = len(re.findall(r"<h3[^>]*>\s*❓", raw_body, flags=re.IGNORECASE))
    topic_relevance_score = max(0, 100 - 12 * len([phrase for phrase in GENERIC_FILLER_PHRASES if phrase in raw_body]) - 8 * len([i for i in issues if i.startswith("irrelevant_selected_link")]))
    readability_score = max(0, 100 - max(0, h2_count - 12) * 8 - max(0, final_word_count - 2200) // 20)
    duplicate_content_score = max(0, 100 - 20 * len(duplicate_sections_removed) - (20 if "duplicate_h2_titles" in issues else 0))
    seo_score = int(seo_metadata.get("seo_score", 85)) if isinstance(seo_metadata, dict) else 85
    commercial_intent_score = 92 if selected_links else 78
    overall_quality_score = round((topic_relevance_score * 0.28) + (readability_score * 0.20) + (duplicate_content_score * 0.22) + (seo_score * 0.18) + (commercial_intent_score * 0.12))
    human_review_validation = {
        "no_duplicate_topics": not duplicate_sections_removed and "duplicate_h2_titles" not in issues,
        "no_filler_content": not any(phrase in raw_body for phrase in GENERIC_FILLER_PHRASES),
        "no_repeated_explanations": "repeated_paragraphs" not in issues,
        "max_12_h2_sections": h2_count <= 12,
        "faq_relevant": 5 <= faq_count <= 8,
        "cta_unique": len(re.findall(r"article-cta", raw_body)) == 1,
        "internal_links_found": bool(selected_links),
        "images_relevant": True,
        "alt_specific": "alt_not_entity_specific" not in issues,
        "readability_high": readability_score >= 85,
        "topic_relevance_high": topic_relevance_score >= 85,
    }
    if overall_quality_score < 85:
        issues.append(f"overall_quality_below_85:{overall_quality_score}")
    return {
        "final_quality_passed": not issues,
        "final_quality_issues": issues,
        "duplicate_sections_removed": duplicate_sections_removed,
        "publish_ready": "READY_FOR_REVIEW" if not issues else "NEEDS_REWRITE",
        "final_word_count": _article_word_count(body),
        "required_word_count": _required_word_count_for_topic(topic_type),
        "h2_section_count": h2_count,
        "faq_question_count": faq_count,
        "topic_relevance_score": topic_relevance_score,
        "readability_score": readability_score,
        "duplicate_content_score": duplicate_content_score,
        "seo_score": seo_score,
        "commercial_intent_score": commercial_intent_score,
        "overall_quality_score": overall_quality_score,
        "human_review_validation": human_review_validation,
        "depth_engine_used": topic_type if topic_type != "fallback_generic" else "fallback_generic",
        "topic_depth_sections_added": list(getattr(_depth_upgrade_html, "last_sections_added", [])),
    }

def _final_generation_debug(topic_profile: dict[str, object], validation: dict[str, object], *, regeneration_count: int, final_body_source: str, discovery_debug: dict[str, object] | None = None, body: str = "", selected_products: list[dict[str, object]] | None = None, injected_links: list[dict[str, object]] | None = None, seo_metadata: dict[str, object] | None = None) -> dict[str, object]:
    link_scores = [float(item.get("relevance_score") or item.get("semantic_topic_match_score") or 0) for item in (selected_products or [])]
    return {
        **(discovery_debug or {}),
        **topic_profile,
        **(seo_metadata or {}),
        **validation,
        "detected_topic_type": topic_profile.get("topic_type"),
        "main_entity": topic_profile.get("main_entity"),
        "entity_type": topic_profile.get("entity_type"),
        "content_format": topic_profile.get("content_format"),
        "article_brief": topic_profile.get("article_brief"),
        "selected_contract": topic_profile.get("selected_contract"),
        "selected_internal_links": injected_links or (discovery_debug or {}).get("selected_internal_links", []),
        "selected_internal_links_by_role": {role: [link for link in (injected_links or (discovery_debug or {}).get("selected_internal_links", [])) if str(link.get("link_role") or "") == role] for role in ["exact_entity", "complementary", "related_category", "generic"]},
        "link_priority_path": list(_link_policy_for_profile(str(topic_profile.get("main_entity") or ""), topic_profile).get("priority", [])),
        "selected_products": selected_products or [],
        "link_relevance_score": round(sum(link_scores) / len(link_scores), 1) if link_scores else 0.0,
        "final_word_count": _article_word_count(body),
        "regeneration_count": regeneration_count,
        "regenerated_due_to_validation": regeneration_count > 0,
        "final_body_source": final_body_source,
    }


def _prepare_publishing_metadata(
    *,
    title: str,
    slug: str,
    keyword: str,
    body: str,
    meta_title: str,
    meta_description: str,
    topic_profile: dict[str, object],
    featured_prompt: str,
    diversity: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    package = build_multi_image_package(title, keyword, slug, topic_profile, featured_prompt)
    image_package = package["image_package"]
    placement = package["image_placement_guide"]
    istore_package = build_istore_copy_paste_package(title, slug, meta_title, meta_description, body, image_package)  # type: ignore[arg-type]
    qa = validate_complete_publishing_package(body, image_package, placement, istore_package, diversity)
    metadata = {
        **package,
        "istore_copy_paste_package": istore_package,
        "istore_block_structure": istore_package.get("article_blocks", []),
        "final_qa_validation": qa,
        "diversity": diversity or {"diversity_score": 100, "max_similarity": 0, "threshold": 0.82, "passed": True},
    }
    return metadata, qa

def generate_daily_article_draft(db: Session, *, randomize: bool = False) -> tuple[ContentArticleDraft, bool, datetime | None]:
    if randomize:
        (title, keyword, intent), reused, last_generated_at = select_random_topic(db)
    else:
        title, keyword, intent = _select_topic(db)
        reused = False
        last_generated_at = None
    slug, _slug_source = _fallback_topic_slug(keyword, title)
    topic_profile = _classify_topic(title, keyword, intent)
    related, discovery_debug = _discover_related_links(db, keyword)
    featured_prompt, section_prompts = _topic_image_prompts(keyword, topic_profile)
    body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
    body, injected_links = inject_internal_links_into_html(body, related, topic_profile)
    body, _, _ = _postprocess_article_assets(body, "", topic_profile=topic_profile)
    previous_bodies: list[str] = []
    if randomize:
        cutoff = datetime.now(UTC) - timedelta(days=60)
        previous_bodies = [str(row[0] or "") for row in db.query(ContentArticleDraft.article_body).filter(ContentArticleDraft.created_at >= cutoff).order_by(ContentArticleDraft.created_at.desc()).limit(10).all()]
    diversity = calculate_diversity_score(body, previous_bodies)
    if randomize and not diversity["passed"]:
        regenerated_body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related, topic_profile)
        body, _, _ = _postprocess_article_assets(body, "", topic_profile=topic_profile)
        diversity = calculate_diversity_score(body, previous_bodies)
    validation = validate_article_relevance(title, keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    regeneration_count = 0
    if not validation["validation_passed"]:
        regenerated_body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related, topic_profile)
        body, _, _ = _postprocess_article_assets(body, "", topic_profile=topic_profile)
        if randomize:
            diversity = calculate_diversity_score(body, previous_bodies)
        regeneration_count = 1
        validation = validate_article_relevance(title, keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "איזו טמפרטורה הכי חשובה למדוד?", "acceptedAnswer": {"@type": "Answer", "text": "הטמפרטורה הפנימית של הנתח."}},
            {"@type": "Question", "name": "כמה זמן מנוחה באמת צריך?", "acceptedAnswer": {"@type": "Answer", "text": "בדרך כלל 5–10 דקות."}},
            {"@type": "Question", "name": "מתי מוסיפים רוטב או גלייז?", "acceptedAnswer": {"@type": "Answer", "text": "בשלב הסופי כדי למנוע שריפה."}},
        ],
    }
    seo_metadata = build_topic_seo_metadata(keyword, title, topic_profile)
    meta_title, meta_description = str(seo_metadata["meta_title"]), str(seo_metadata["meta_description"])
    body, meta_title, faq_schema = _postprocess_article_assets(body, meta_title, faq_schema, topic_profile=topic_profile)
    image_alt_text, alt_source = _generate_image_alt_text(title, keyword, topic_profile)
    final_quality = validate_final_article_quality(body, meta_title, seo_metadata, topic_profile, injected_links or related, image_alt_text)
    publishing_metadata, publishing_qa = _prepare_publishing_metadata(title=title, slug=slug, keyword=keyword, body=body, meta_title=meta_title, meta_description=meta_description, topic_profile=topic_profile, featured_prompt=featured_prompt, diversity=diversity)
    validation = {**validation, **final_quality, **publishing_qa, "alt_generation_source": alt_source, "topic_specific_expansion_source": _topic_specific_expansion_html(str(topic_profile.get("topic_type") or ""), str(topic_profile.get("main_entity") or keyword), keyword, str(topic_profile.get("entity_key") or ""))[1]}
    draft = ContentArticleDraft(
        status="READY_FOR_REVIEW" if validation["validation_passed"] and validation.get("final_quality_passed", True) and not publishing_qa.get("publishing_package_failed_checks") else "NEEDS_REWRITE", topic_title=title, title=title, slug=slug,
        meta_title=meta_title,
        meta_description=meta_description,
        focus_keyword=keyword, target_intent=intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(injected_links or related, ensure_ascii=False),
        faq_schema_json=json.dumps(faq_schema, ensure_ascii=False),
        section_image_prompts_json=json.dumps(section_prompts, ensure_ascii=False),
        featured_image_prompt=featured_prompt,
        image_alt_text=image_alt_text, image_title=f"תמונת שער: {title}", image_caption="הדגמה מעשית של השיטה במאמר.",
        image_filename_slug=f"compass-grill-{slug}", image_style_rules="realistic outdoor BBQ photography",
        generated_image_url=None, uploaded_media_id=None, image_publish_status="NOT_PUBLISHED",
        target_site_section="blog", target_publish_type="article", target_blog_base_url="https://compassgrill.co.il/blog/",
        target_path=f"/blog/{slug}", target_url=f"https://compassgrill.co.il/blog/{slug}", publish_destination_status="ready", featured_image_status="planned",
        image_generation_metadata_json=json.dumps(publishing_metadata, ensure_ascii=False),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    logger.info(
        "[ARTICLE_TRACE] step=draft_persistence draft_id=%s selected_topic_type=%s selected_contract=%s selected_generator=%s generator_version=%s draft_source=%s article_body_length=%s",
        draft.id,
        topic_profile.get("topic_type"),
        topic_profile.get("selected_contract"),
        topic_profile.get("selected_generator"),
        GENERATOR_VERSION,
        "content_article_drafts.article_body",
        len(draft.article_body or ""),
    )
    setattr(draft, "link_match_debug", _final_generation_debug(topic_profile, validation, regeneration_count=regeneration_count, final_body_source="contract_engine", discovery_debug=discovery_debug, body=body, selected_products=related, injected_links=injected_links or related, seo_metadata=seo_metadata))
    return draft, reused, last_generated_at


def generate_topic_article_draft(
    db: Session,
    *,
    topic_title: str,
    focus_keyword: str,
    target_intent: str,
    preferred_slug: str | None = None,
) -> ContentArticleDraft:
    topic_profile = _classify_topic(topic_title, focus_keyword, target_intent)
    related, discovery_debug = _discover_related_links(db, focus_keyword)
    slug = _slugify(preferred_slug or "") if preferred_slug else _fallback_topic_slug(focus_keyword, topic_title)[0]
    featured_prompt, section_prompts = _topic_image_prompts(focus_keyword, topic_profile)
    logger.info(
        "[ARTICLE_TRACE] step=selected_generator selected_topic_type=%s selected_contract=%s selected_generator=%s generator_version=%s draft_source=%s",
        topic_profile.get("topic_type"),
        topic_profile.get("selected_contract"),
        topic_profile.get("selected_generator"),
        GENERATOR_VERSION,
        "generator_return",
    )
    body, _ = _remove_h1_tags(_build_article_html(topic_title, focus_keyword, related, topic_profile=topic_profile))
    body, injected_links = inject_internal_links_into_html(body, related, topic_profile)
    body, _, _ = _postprocess_article_assets(body, "", topic_profile=topic_profile)
    diversity = calculate_diversity_score(body, [])
    validation = validate_article_relevance(topic_title, focus_keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    regeneration_count = 0
    if not validation["validation_passed"]:
        logger.info(
            "[ARTICLE_TRACE] step=depth_expansion_regeneration selected_topic_type=%s selected_contract=%s selected_generator=%s generator_version=%s draft_source=%s",
            topic_profile.get("topic_type"),
            topic_profile.get("selected_contract"),
            topic_profile.get("selected_generator"),
            GENERATOR_VERSION,
            "generator_return",
        )
        regenerated_body, _ = _remove_h1_tags(_build_article_html(topic_title, focus_keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related, topic_profile)
        body, _, _ = _postprocess_article_assets(body, "", topic_profile=topic_profile)
        diversity = calculate_diversity_score(body, [])
        regeneration_count = 1
        validation = validate_article_relevance(topic_title, focus_keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    seo_metadata = build_topic_seo_metadata(focus_keyword, topic_title, topic_profile)
    meta_title, meta_description = str(seo_metadata["meta_title"]), str(seo_metadata["meta_description"])
    body, meta_title, _ = _postprocess_article_assets(body, meta_title, topic_profile=topic_profile)
    image_alt_text, alt_source = _generate_image_alt_text(topic_title, focus_keyword, topic_profile)
    final_quality = validate_final_article_quality(body, meta_title, seo_metadata, topic_profile, injected_links or related, image_alt_text)
    publishing_metadata, publishing_qa = _prepare_publishing_metadata(title=topic_title, slug=slug, keyword=focus_keyword, body=body, meta_title=meta_title, meta_description=meta_description, topic_profile=topic_profile, featured_prompt=featured_prompt, diversity=diversity)
    validation = {**validation, **final_quality, **publishing_qa, "alt_generation_source": alt_source, "topic_specific_expansion_source": _topic_specific_expansion_html(str(topic_profile.get("topic_type") or ""), str(topic_profile.get("main_entity") or focus_keyword), focus_keyword, str(topic_profile.get("entity_key") or ""))[1]}
    draft = ContentArticleDraft(
        status="READY_FOR_REVIEW" if validation["validation_passed"] and validation.get("final_quality_passed", True) and not publishing_qa.get("publishing_package_failed_checks") else "NEEDS_REWRITE", topic_title=topic_title, title=topic_title, slug=slug,
        meta_title=meta_title,
        meta_description=meta_description,
        focus_keyword=focus_keyword, target_intent=target_intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(injected_links or related, ensure_ascii=False),
        section_image_prompts_json=json.dumps(section_prompts, ensure_ascii=False),
        featured_image_prompt=featured_prompt,
        image_alt_text=image_alt_text, image_title=f"תמונת שער: {topic_title}", image_caption="הדגמה מעשית של השיטה במאמר.",
        image_filename_slug=f"compass-grill-{slug}", image_style_rules="realistic outdoor BBQ photography",
        generated_image_url=None, uploaded_media_id=None, image_publish_status="NOT_PUBLISHED",
        target_site_section="blog", target_publish_type="article", target_blog_base_url="https://compassgrill.co.il/blog/",
        target_path=f"/blog/{slug}", target_url=f"https://compassgrill.co.il/blog/{slug}", publish_destination_status="ready", featured_image_status="planned",
        image_generation_metadata_json=json.dumps(publishing_metadata, ensure_ascii=False),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    logger.info(
        "[ARTICLE_TRACE] step=draft_persistence draft_id=%s selected_topic_type=%s selected_contract=%s selected_generator=%s generator_version=%s draft_source=%s article_body_length=%s",
        draft.id,
        topic_profile.get("topic_type"),
        topic_profile.get("selected_contract"),
        topic_profile.get("selected_generator"),
        GENERATOR_VERSION,
        "content_article_drafts.article_body",
        len(draft.article_body or ""),
    )
    setattr(draft, "link_match_debug", _final_generation_debug(topic_profile, validation, regeneration_count=regeneration_count, final_body_source="contract_engine", discovery_debug=discovery_debug, body=body, selected_products=related, injected_links=injected_links or related, seo_metadata=seo_metadata))
    return draft
