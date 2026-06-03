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
GENERATOR_VERSION = "v2-topic-specific-2026-05-25"

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
    "butcher_paper": ["נייר קצבים"],
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


def _infer_type_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/products" in path:
        return "product"
    if "/categories" in path or "/category" in path:
        return "category"
    if "/brands" in path or "/brand" in path:
        return "brand"
    if "/blog" in path:
        return "blog"
    return "info"


def _slug_from_url(url: str) -> str:
    return (urlparse(url).path.strip("/").split("/")[-1] or "").lower()


def _title_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip()


def _load_sitemap_index(force_refresh: bool = False) -> tuple[list[dict[str, object]], dict[str, object]]:
    now = time.time()
    if not force_refresh and _INTERNAL_LINK_INDEX_CACHE["entries"] and now - float(_INTERNAL_LINK_INDEX_CACHE["loaded_at"]) < int(_INTERNAL_LINK_INDEX_CACHE["ttl_seconds"]):
        return _INTERNAL_LINK_INDEX_CACHE["entries"], _INTERNAL_LINK_INDEX_CACHE["stats"]

    entries: list[dict[str, object]] = []
    stats = {"sitemap_loaded_count": 0, "products_loaded_count": 0, "categories_loaded_count": 0}
    for sitemap_url in SITEMAP_SOURCES:
        try:
            xml = requests.get(sitemap_url, timeout=20).text
            locs = re.findall(r"<loc>(.*?)</loc>", xml, flags=re.IGNORECASE)
            lastmods = re.findall(r"<lastmod>(.*?)</lastmod>", xml, flags=re.IGNORECASE)
            is_index = "<sitemapindex" in xml.lower()
            urls = [u.strip() for u in locs if u.strip().startswith("http") and not u.strip().endswith(".xml")] if is_index else [u.strip() for u in locs if u.strip().startswith("http")]
            for i, u in enumerate(urls):
                typ = _infer_type_from_url(u)
                slug = _slug_from_url(u)
                title = _title_from_slug(slug)
                blob = _normalize_hebrew(f"{title} {slug} {u}")
                entries.append({"url": u, "slug": slug, "title": title, "type": typ, "tokens": _tokenize_hebrew(blob), "lastmod": lastmods[i] if i < len(lastmods) else None})
                if typ == "product":
                    stats["products_loaded_count"] += 1
                if typ == "category":
                    stats["categories_loaded_count"] += 1
            stats["sitemap_loaded_count"] += 1
        except Exception:
            logger.exception("Failed loading sitemap %s", sitemap_url)

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
    ("grill_accessory_guide", ["אבני בזלת", "בזלת", "אבני לבה", "מדחום", "נייר קצבים", "כפפות", "מלקחיים", "מברשת", "basalt", "lava rocks", "lava stones", "thermometer", "accessory"], "accessory", "commercial_informational"),
    ("equipment_buying_guide", ["גריל גז", "גריל פחמים", "מעשנה", "טאבון", "מטבח חוץ", "gas grill"], "equipment", "commercial"),
]

ACCESSORY_ENTITY_PROFILES: dict[str, dict[str, object]] = {
    "basalt_stones": {
        "canonical_entity": "אבני בזלת / אבני לבה",
        "match_terms": ["אבני בזלת", "בזלת", "אבני לבה", "lava rocks", "lava stones", "basalt stones", "basalt"],
        "required_terms": ["אבני לבה", "אבני בזלת", "lava rocks", "פיזור חום", "הפחתת התלקחויות", "מבערים", "אידוי שומן", "יציבות טמפרטורה", "מרווחי החלפה", "טעויות נפוצות"],
        "internal_link_keywords": ["אבני בזלת", "אבני לבה", "גריל גז", "מבערים", "אביזרים לגריל"],
        "image_prompt_pattern": "black basalt lava stones and lava rocks / basalt stones arranged above gas grill burners, realistic grill accessory guide, heat distribution and grease vaporization context, no thermometer, no meat temperature reading, no text",
    },
    "thermometer": {
        "canonical_entity": "מדחום לבשר",
        "match_terms": ["מדחום לבשר", "מדחום", "thermometer", "meat thermometer", "probe"],
        "required_terms": ["מדחום", "probe", "קריאה מהירה", "כיול", "טמפרטורה פנימית", "זמן תגובה", "ניקוי", "טעויות נפוצות"],
        "internal_link_keywords": ["מדחום לבשר", "אביזרים לגריל", "גריל גז"],
        "image_prompt_pattern": "digital meat thermometer probe used as a grill accessory beside a gas grill, instant-read display visible without numbers, clean food-safe maintenance context, no unrelated heat-distribution stones, no text",
    },
}

TOPIC_TYPE_GENERATORS = {
    "meat_quick_grill_cut": "contract_meat_quick_grill_cut",
    "meat_low_slow_smoking": "contract_meat_low_slow_smoking",
    "poultry_grill_recipe": "contract_poultry_grill_recipe",
    "fuel_comparison_or_guide": "contract_fuel_comparison_or_guide",
    "smoking_wood_guide": "contract_smoking_wood_guide",
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
    validation_passed = not missing_required_terms and not missing_required_sections and not forbidden_terms_found and not intent_missing and not generic_leakage and image_prompt_relevant
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




def _topic_synonyms(topic: str) -> list[str]:
    normalized = _normalize_hebrew(topic)
    syn = [topic]
    if "פיקניה" in normalized or "picanha" in normalized:
        syn += ["picanha", "steak", "meat"]
    if "בזלת" in normalized or "לבה" in normalized:
        syn += ["basalt", "lava stones", "grill accessories", "gas grill accessories"]
    if "שבבי" in normalized or "עישון" in normalized:
        syn += ["wood chips", "smoker", "smoking wood", "עישון"]
    if "מדחום" in normalized:
        syn += ["thermometer", "accessories", "מדחום"]
    return list(dict.fromkeys(syn))

def _match_terms_for_topic(topic: str) -> list[str]:
    terms = [topic]
    normalized = _normalize_hebrew(topic)
    if any(word in normalized for word in ["בזלת", "לבה", "lava", "basalt", "stone", "stones", "rocks"]):
        terms.extend([
            "אבני בזלת", "אבן בזלת", "אבני לבה", "אבן לבה", "אבנים לגריל", "אבני בזלת לגריל",
            "basalt", "lava stone", "lava rocks", "grill stones", "basalt stones",
        ])
    return list(dict.fromkeys([t.strip() for t in terms if t.strip()]))


def _semantic_topic_match_score(topic: str, product: object) -> float:
    title = _safe_product_title(product)
    slug = getattr(product, "slug", "") or ""
    category = getattr(product, "category_name", "") or ""
    topic_tokens = _tokenize_hebrew(topic)
    target_tokens = _tokenize_hebrew(f"{title} {slug} {category}")
    overlap = len(topic_tokens & target_tokens)
    score = overlap * 20
    if any(k in target_tokens for k in {"גריל", "bbq", "smoker", "מעשנה", "שבבי", "עישון"}):
        score += 40
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


def _discover_related_links(db: Session, topic: str, limit: int = 6) -> tuple[list[dict[str, str | float]], dict[str, object]]:
    products = db.query(IStoreProduct).order_by(IStoreProduct.updated_at.desc()).limit(400).all()
    sitemap_entries, sitemap_stats = _load_sitemap_index()
    out: list[dict[str, str | float]] = []
    terms = _match_terms_for_topic(topic)
    terms.extend(_topic_synonyms(topic))
    normalized_terms = [_normalize_hebrew(t) for t in terms]
    excluded_low: list[dict[str, str | float]] = []
    for p in products:
        title = _safe_product_title(p)
        url = _safe_product_url(p)
        if not title or not url:
            continue
        score = _semantic_topic_match_score(topic, p)
        blob = _normalize_hebrew(f"{title} {url} {getattr(p, 'slug', '') or ''} {getattr(p, 'category_name', '') or ''}")
        exact_hits = sum(1 for t in normalized_terms if t and t in blob)
        if exact_hits:
            score = min(100.0, score + (18 * exact_hits))
        if any(t in blob for t in ["אבני בזלת", "אבני לבה", "lava", "basalt", "rocks", "stones"]):
            score = min(100.0, score + 30)
        if topic == "שבבי עץ לעישון":
            score = min(100.0, score + _wood_link_priority_score(p))
        if score < 40:
            if score > 0:
                excluded_low.append({"title": title, "url": url, "relevance_score": score})
            continue
        out.append({"title": title, "url": url, "semantic_topic_match_score": score, "relatedness_score": score, "relevance_score": score})
    for e in sitemap_entries:
        blob = _normalize_hebrew(f"{e.get('title','')} {e.get('slug','')} {e.get('url','')}")
        tokens = set(e.get("tokens") or set())
        term_overlap = len(tokens & set(_tokenize_hebrew(_normalize_hebrew(topic))))
        score = term_overlap * 22
        if any(t in blob for t in normalized_terms):
            score += 30
        if e.get("type") in {"product","category"}:
            score += 15
        if score >= 40:
            out.append({"title": e.get("title") or e.get("slug"), "url": e.get("url"), "type": e.get("type"), "semantic_topic_match_score": float(min(score,100)), "relatedness_score": float(min(score,100)), "relevance_score": float(min(score,100)), "reason": "sitemap topic match"})
        elif score > 0:
            excluded_low.append({"title": e.get("title"), "url": e.get("url"), "relevance_score": float(score)})

    dedup={}
    for item in out:
        dedup[item.get("url","")] = item
    out=list(dedup.values())
    out.sort(key=lambda item: float(item.get("semantic_topic_match_score", 0)), reverse=True)
    trimmed = out[: max(3, min(limit, 6))]
    best = trimmed[0] if trimmed else {}
    debug = {
        **sitemap_stats,
        "link_discovery_source": ["db_products", "latest_crawl_cache", "sitemap_urls", "product_category_pages"],
        "searched_terms": terms,
        "matched_product_count": len(trimmed),
        "matched_internal_link_count": len(trimmed),
        "best_match_title": best.get("title"),
        "best_match_url": best.get("url"),
        "best_match_score": best.get("semantic_topic_match_score", 0),
        "internal_link_candidates": len(out),
        "excluded_low_relevance_links": excluded_low[:20],
        "selected_internal_links": [i.get("url") for i in trimmed],
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
    return "<h2>שאלות נפוצות</h2>" + "".join(f"<h3>{q}</h3><p>{a}</p>" for q, a in items) + "\n"


def _links_section(related: list[dict[str, str | float]]) -> str:
    heading = "מוצרים רלוונטיים באתר" if related else "קישורים פנימיים ומוצרים משלימים"
    links_html = "".join([f"<li><a href='{p['url']}'>{p['title']}</a></li>" for p in related[:4] if p.get("url") and p.get("title")])
    return _h2(heading, f"<ul>{links_html}</ul>" if links_html else "<p>כרגע אין קישורים פנימיים רלוונטיים להצגה.</p>")


def _build_contract_article(title: str, keyword: str, related: list[dict[str, str | float]], profile: dict[str, object]) -> str:
    topic_type = str(profile.get("topic_type") or "fallback_generic")
    entity = str(profile.get("main_entity") or keyword or title)
    links = _links_section(related)

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


def _build_article_html(
    title: str,
    keyword: str,
    related: list[dict[str, str | float]],
    *,
    topic_profile: dict[str, object] | None = None,
) -> str:
    profile = topic_profile or _classify_topic(title, keyword, "informational")
    return _build_contract_article(title, keyword, related, profile)

def inject_internal_links_into_html(article_html: str, related: list[dict[str, str | float]]) -> tuple[str, list[dict[str, str]]]:
    html = article_html or ""
    injected: list[dict[str, str]] = []
    used_urls: set[str] = set()
    for link in related:
        if len(injected) >= 6:
            break
        score = float(link.get("relevance_score") or 0)
        url = str(link.get("url") or "").strip()
        anchor = str(link.get("anchor_text") or link.get("title") or "").strip()
        if score < 40 or not url or not anchor or url in used_urls:
            continue
        linked = f"<a href='{url}'>{anchor}</a>"
        if anchor in html and linked not in html:
            html = html.replace(anchor, linked, 1)
            injected.append({"url": url, "anchor_text": anchor, "section": "body_paragraph"})
            used_urls.add(url)
    return html, injected



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


def _topic_meta(keyword: str, title: str, topic_profile: dict[str, object]) -> tuple[str, str]:
    contract = topic_profile.get("contract") if isinstance(topic_profile.get("contract"), dict) else {}
    meta_pattern = str(contract.get("meta_pattern") or "{keyword}: מדריך מעשי בעברית עם טיפים, שלבים ו-FAQ.")
    meta_title = f"{keyword}: {str(topic_profile.get('content_format') or 'מדריך')} | Compass Grill"[:65]
    return meta_title, meta_pattern.format(keyword=keyword)[:160]


def _final_generation_debug(topic_profile: dict[str, object], validation: dict[str, object], *, regeneration_count: int, final_body_source: str, discovery_debug: dict[str, object] | None = None) -> dict[str, object]:
    return {
        **(discovery_debug or {}),
        **topic_profile,
        **validation,
        "detected_topic_type": topic_profile.get("topic_type"),
        "main_entity": topic_profile.get("main_entity"),
        "entity_type": topic_profile.get("entity_type"),
        "content_format": topic_profile.get("content_format"),
        "article_brief": topic_profile.get("article_brief"),
        "selected_contract": topic_profile.get("selected_contract"),
        "regeneration_count": regeneration_count,
        "regenerated_due_to_validation": regeneration_count > 0,
        "final_body_source": final_body_source,
    }

def generate_daily_article_draft(db: Session, *, randomize: bool = False) -> tuple[ContentArticleDraft, bool, datetime | None]:
    if randomize:
        (title, keyword, intent), reused, last_generated_at = select_random_topic(db)
    else:
        title, keyword, intent = _select_topic(db)
        reused = False
        last_generated_at = None
    related = _related_products(db, keyword)
    slug, _slug_source = _fallback_topic_slug(keyword, title)
    topic_profile = _classify_topic(title, keyword, intent)
    featured_prompt, section_prompts = _topic_image_prompts(keyword, topic_profile)
    body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
    body, injected_links = inject_internal_links_into_html(body, related)
    validation = validate_article_relevance(title, keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    regeneration_count = 0
    if not validation["validation_passed"]:
        regenerated_body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related)
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
    meta_title, meta_description = _topic_meta(keyword, title, topic_profile)
    draft = ContentArticleDraft(
        status="READY_FOR_REVIEW" if validation["validation_passed"] else "CONTENT_DRAFT", topic_title=title, title=title, slug=slug,
        meta_title=meta_title,
        meta_description=meta_description,
        focus_keyword=keyword, target_intent=intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(injected_links or related, ensure_ascii=False),
        faq_schema_json=json.dumps(faq_schema, ensure_ascii=False),
        section_image_prompts_json=json.dumps(section_prompts, ensure_ascii=False),
        featured_image_prompt=featured_prompt,
        image_alt_text=f"{title} - הדגמה על גריל", image_title=f"תמונת שער: {title}", image_caption="הדגמה מעשית של השיטה במאמר.",
        image_filename_slug=f"compass-grill-{slug}", image_style_rules="realistic outdoor BBQ photography",
        generated_image_url=None, uploaded_media_id=None, image_publish_status="NOT_PUBLISHED",
        target_site_section="blog", target_publish_type="article", target_blog_base_url="https://compassgrill.co.il/blog/",
        target_path=f"/blog/{slug}", target_url=f"https://compassgrill.co.il/blog/{slug}", publish_destination_status="ready", featured_image_status="planned",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    setattr(draft, "link_match_debug", _final_generation_debug(topic_profile, validation, regeneration_count=regeneration_count, final_body_source="contract_engine"))
    return draft, reused, last_generated_at


def generate_topic_article_draft(
    db: Session,
    *,
    topic_title: str,
    focus_keyword: str,
    target_intent: str,
    preferred_slug: str | None = None,
) -> ContentArticleDraft:
    related, discovery_debug = _discover_related_links(db, focus_keyword)
    topic_profile = _classify_topic(topic_title, focus_keyword, target_intent)
    slug = _slugify(preferred_slug or "") if preferred_slug else _fallback_topic_slug(focus_keyword, topic_title)[0]
    featured_prompt, section_prompts = _topic_image_prompts(focus_keyword, topic_profile)
    body, _ = _remove_h1_tags(_build_article_html(topic_title, focus_keyword, related, topic_profile=topic_profile))
    body, injected_links = inject_internal_links_into_html(body, related)
    validation = validate_article_relevance(topic_title, focus_keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    regeneration_count = 0
    if not validation["validation_passed"]:
        regenerated_body, _ = _remove_h1_tags(_build_article_html(topic_title, focus_keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related)
        regeneration_count = 1
        validation = validate_article_relevance(topic_title, focus_keyword, body, topic_profile, image_prompt=featured_prompt, internal_links=injected_links or related)
    meta_title, meta_description = _topic_meta(focus_keyword, topic_title, topic_profile)
    draft = ContentArticleDraft(
        status="READY_FOR_REVIEW" if validation["validation_passed"] else "CONTENT_DRAFT", topic_title=topic_title, title=topic_title, slug=slug,
        meta_title=meta_title,
        meta_description=meta_description,
        focus_keyword=focus_keyword, target_intent=target_intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(injected_links or related, ensure_ascii=False),
        section_image_prompts_json=json.dumps(section_prompts, ensure_ascii=False),
        featured_image_prompt=featured_prompt,
        image_alt_text=f"{topic_title} - הדגמה על גריל", image_title=f"תמונת שער: {topic_title}", image_caption="הדגמה מעשית של השיטה במאמר.",
        image_filename_slug=f"compass-grill-{slug}", image_style_rules="realistic outdoor BBQ photography",
        generated_image_url=None, uploaded_media_id=None, image_publish_status="NOT_PUBLISHED",
        target_site_section="blog", target_publish_type="article", target_blog_base_url="https://compassgrill.co.il/blog/",
        target_path=f"/blog/{slug}", target_url=f"https://compassgrill.co.il/blog/{slug}", publish_destination_status="ready", featured_image_status="planned",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    setattr(draft, "link_match_debug", _final_generation_debug(topic_profile, validation, regeneration_count=regeneration_count, final_body_source="contract_engine", discovery_debug=discovery_debug))
    return draft
