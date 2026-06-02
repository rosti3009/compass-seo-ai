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

TOPIC_TYPE_CONTRACTS: dict[str, dict[str, object]] = {
    "meat_cut_guide": {
        "required_sections": ["הכנת הנתח", "טמפרטורת יעד", "חיתוך", "שאלות נפוצות"],
        "required_terms": ["שכבת שומן", "מלח גס", "54–56°C", "חיתוך נגד הסיבים"],
        "forbidden_terms": ["74°C", "פחם קוקוס", "גלייז כנפיים"],
        "meta_pattern": "{keyword}: מדריך נתח מדויק עם הכנה, טמפרטורה וחיתוך נכון.",
        "excerpt_pattern": "מדריך ממוקד ל-{keyword}: הכנה, טמפרטורת יעד, טעויות וחיתוך נכון.",
        "image_prompt_pattern": "{keyword} steak with visible fat cap on grill, realistic BBQ photography, no text",
        "internal_link_keywords": ["מדחום לבשר", "מלח גס", "גריל פחמים"],
    },
    "poultry_recipe": {
        "required_sections": ["ייבוש", "חום ישיר מול עקיף", "טמפרטורת יעד", "גלייז", "FAQ"],
        "required_terms": ["ייבוש", "74°C", "גלייז", "חום עקיף"],
        "forbidden_terms": ["פיקניה", "מדיום רייר", "שכבת שומן בקר"],
        "meta_pattern": "{keyword}: מתכון עוף/כנפיים עם שיטה, טמפרטורת בטיחות וטעויות.",
        "excerpt_pattern": "מתכון מעשי ל-{keyword} עם שלבים, כלים, טמפרטורת יעד ו-FAQ.",
        "image_prompt_pattern": "crispy grilled chicken wings, realistic BBQ photography, no text",
        "internal_link_keywords": ["רוטב BBQ", "מדחום לבשר", "גריל גז"],
    },
    "fuel_comparison": {
        "required_sections": ["מה ההבדל", "יתרונות", "חסרונות", "למי מתאים כל אחד", "טבלת השוואה", "המלצה מעשית"],
        "required_terms": ["פחם קוקוס", "פחם עץ", "זמן בעירה", "יציבות חום", "עשן", "אפר"],
        "forbidden_terms": ["74°C", "54–57°C", "גלייז", "מנוחה של סטייק", "טמפרטורת יעד פנימית"],
        "meta_pattern": "{keyword}: השוואה מעשית בין סוגי פחם לפי זמן בעירה, עשן ואפר.",
        "excerpt_pattern": "השוואה בין פחם קוקוס לפחם עץ: יתרונות, חסרונות, טבלת הבדלים והמלצה.",
        "image_prompt_pattern": "coconut charcoal briquettes next to natural lump wood charcoal, BBQ fuel comparison, no meat, no text",
        "internal_link_keywords": ["פחם קוקוס", "פחם עץ", "מדליק פחמים"],
    },
    "smoking_accessory_guide": {
        "required_sections": ["סוגי שבבי", "עוצמת עשן", "כמה שבבים", "טעויות"],
        "required_terms": ["עשן", "עישון", "סוג העץ", "thin blue smoke"],
        "forbidden_terms": ["74°C", "גלייז כנפיים"],
        "meta_pattern": "{keyword}: מדריך עישון עם סוגים, שימוש נכון וטעויות נפוצות.",
        "excerpt_pattern": "איך לבחור ולהשתמש ב-{keyword} לעישון נקי ומדויק.",
        "image_prompt_pattern": "wood chips in smoker box with thin blue smoke, realistic BBQ photo, no text",
        "internal_link_keywords": ["שבבי עץ", "מעשנה", "נייר קצבים"],
    },
    "grill_accessory_guide": {
        "required_sections": ["מה זה", "יתרונות", "איך לבחור", "ניקוי", "שאלות נפוצות"],
        "required_terms": ["פיזור חום", "גריל גז", "התלקחויות", "ניקוי והחלפה"],
        "forbidden_terms": ["טמפ' פנימית של בשר", "טמפרטורה פנימית של בשר", "גלייז", "מדיום רייר", "74°C"],
        "meta_pattern": "{keyword}: מדריך אביזר לגריל עם יתרונות, בחירה, שימוש ותחזוקה.",
        "excerpt_pattern": "מה {keyword} עושה, איך בוחרים, מתי משתמשים ואיך מתחזקים.",
        "image_prompt_pattern": "black basalt lava stones inside a gas grill, no meat, realistic BBQ photo, no text",
        "internal_link_keywords": ["אביזרים לגריל", "גריל גז", "אבני בזלת"],
    },
    "equipment_buying_guide": {
        "required_sections": ["מה זה", "יתרונות", "איך לבחור", "מתי לקנות", "CTA"],
        "required_terms": ["אחריות", "גודל", "חומר", "תקציב"],
        "forbidden_terms": ["74°C", "גלייז"],
        "meta_pattern": "{keyword}: מדריך קנייה עם יתרונות, בחירה והתאמה לשימוש.",
        "excerpt_pattern": "מדריך קנייה ל-{keyword}: מה לבדוק, למי מתאים ומתי לבחור.",
        "image_prompt_pattern": "realistic BBQ equipment buying guide photo focused on {keyword}, no text",
        "internal_link_keywords": ["גריל גז", "טאבון", "אביזרים"],
    },
    "recipe_how_to": {
        "required_sections": ["שלב-אחר-שלב", "טעויות", "כלים", "שאלות נפוצות"],
        "required_terms": ["שלבים", "כלים", "טעויות", "FAQ"],
        "forbidden_terms": [],
        "meta_pattern": "{keyword}: מדריך איך-לעשות עם שלבים, כלים וטעויות נפוצות.",
        "excerpt_pattern": "שיטת עבודה ל-{keyword} עם שלבים ברורים, כלים ו-FAQ.",
        "image_prompt_pattern": "realistic outdoor grill how-to photo focused on {keyword}, no text",
        "internal_link_keywords": ["אביזרים לגריל", "מדחום", "גריל"],
    },
    "fallback_generic": {
        "required_sections": ["למה זה חשוב", "שיטת עבודה", "טעויות", "שאלות נפוצות"],
        "required_terms": [],
        "forbidden_terms": [],
        "meta_pattern": "{keyword}: מדריך מעשי בעברית עם טיפים, שלבים ו-FAQ.",
        "excerpt_pattern": "מדריך מעשי ל-{keyword} עם הסברים וטיפים לשימוש נכון.",
        "image_prompt_pattern": "realistic outdoor BBQ guide photo focused on {keyword}, no text",
        "internal_link_keywords": ["גריל", "אביזרים"],
    },
}

RECOGNIZED_TOPIC_TYPES = {k for k in TOPIC_TYPE_CONTRACTS if k != "fallback_generic"}


def _contract_for(topic_type: str) -> dict[str, object]:
    return TOPIC_TYPE_CONTRACTS.get(topic_type, TOPIC_TYPE_CONTRACTS["fallback_generic"])


def _classify_topic(topic_title: str, focus_keyword: str, target_intent: str) -> dict[str, object]:
    blob = f"{topic_title} {focus_keyword}".lower()
    intent = target_intent or "informational"
    topic_type = "fallback_generic"
    selected_generator = "generic_fallback"
    generator_source = "fallback"
    fallback_reason = "no_specialized_topic_match"
    product_type = "general"
    content_type = "guide"
    related_keywords = [focus_keyword]

    if "פיקניה" in blob:
        topic_type, product_type, content_type = "meat_cut_guide", "picanha", "grilling_guide"
        intent = "how-to"
        selected_generator = "picanha_specialized"
        generator_source, fallback_reason = "specialized", ""
        related_keywords = ["פיקניה", "שכבת שומן", "מלח גס", "שיפוד ברזילאי", "חיתוך נגד הסיבים"]
    elif "כנפיים" in blob and "קריספ" in blob:
        topic_type, product_type, content_type = "poultry_recipe", "wings", "how_to_recipe"
        intent = "how-to"
        selected_generator = "crispy_wings_specialized"
        generator_source, fallback_reason = "specialized", ""
        related_keywords = ["כנפיים קריספיות", "ייבוש", "בייקינג פאודר", "חום עקיף", "74°C"]
    elif "פחם" in blob:
        topic_type, product_type, content_type = "fuel_comparison", "charcoal", "comparison"
        intent = "comparison"
        selected_generator = "charcoal_comparison_specialized"
        generator_source, fallback_reason = "specialized", ""
        related_keywords = ["פחם קוקוס", "פחם עץ", "זמן בעירה", "יציבות חום", "עשן", "אפר"]
    elif "בזלת" in blob or "לבה" in blob:
        topic_type, product_type, content_type = "grill_accessory_guide", "basalt_stones", "commercial_informational"
        intent = "commercial_informational"
        selected_generator = "basalt_stones_specialized"
        generator_source, fallback_reason = "specialized", ""
        related_keywords = ["אבני בזלת לגריל", "פיזור חום", "הפחתת התלקחויות", "גריל גז"]
    elif "שבבי עץ" in blob or "עישון" in blob:
        topic_type, product_type, content_type = "smoking_accessory_guide", "wood_chips", "commercial_informational"
        selected_generator = "wood_chips_specialized"
        generator_source, fallback_reason = "specialized", ""
        related_keywords = ["שבבי עץ לעישון", "עשן", "סוג העץ", "thin blue smoke"]
    elif any(term in blob for term in ["גריל גז", "טאבון", "מדחום"]):
        topic_type, product_type, content_type = "equipment_buying_guide", "equipment", "commercial_informational"
        selected_generator = "equipment_buying_specialized"
        generator_source, fallback_reason = "specialized", ""
    elif any(term in blob for term in ["נייר קצבים", "איך "]):
        topic_type, product_type, content_type = "recipe_how_to", "how_to", "how_to"
        intent = target_intent or "how-to"
        selected_generator = "how_to_specialized"
        generator_source, fallback_reason = "specialized", ""

    contract = _contract_for(topic_type)
    return {
        "topic_type": topic_type,
        "product_type": product_type,
        "content_type": content_type,
        "search_intent": intent,
        "target_keyword": focus_keyword,
        "related_keywords": related_keywords,
        "required_sections": list(contract.get("required_sections", [])),
        "forbidden_sections": [],
        "selected_generator": selected_generator,
        "generator_source": generator_source,
        "fallback_reason": fallback_reason,
        "required_terms": list(contract.get("required_terms", [])),
        "forbidden_terms": list(contract.get("forbidden_terms", [])),
        "selected_contract": topic_type,
        "contract": contract,
    }


def _plain_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or " ")


def _first_paragraph_text(html: str) -> str:
    match = re.search(r"<p[^>]*>(.*?)</p>", html or "", flags=re.IGNORECASE | re.DOTALL)
    return _plain_text(match.group(1) if match else (html or "")[:350])


def _meaningful_title_terms(title: str, keyword: str) -> list[str]:
    stop = {"איך", "או", "על", "עם", "של", "מול", "מדריך", "מלא", "לגריל", "גריל", "ההבדל", "בין"}
    terms = []
    for term in re.split(r"[\s/–-]+", f"{title} {keyword}"):
        term = term.strip(" :|,.")
        if len(term) > 1 and term not in stop and term not in terms:
            terms.append(term)
    return terms


def validate_article_relevance(title: str, keyword: str, body: str, topic_profile: dict[str, object]) -> dict[str, object]:
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
        intent_missing = [t for t in ["מה ההבדל", "יתרונות", "חסרונות", "למי מתאים", "טבלת השוואה", "המלצה מעשית"] if _normalize_hebrew(t) not in plain]
    elif search_intent == "how-to":
        intent_missing = [t for t in ["שלב", "טעויות", "כלים", "שאלות נפוצות"] if _normalize_hebrew(t) not in plain]
    elif search_intent == "commercial_informational":
        intent_missing = [t for t in ["מה זה", "יתרונות", "איך לבחור", "מתי", "CTA"] if _normalize_hebrew(t) not in plain]

    score = 100
    score -= 8 * len(missing_intro_terms)
    score -= 9 * len(missing_required_terms)
    score -= 7 * len(missing_required_sections)
    score -= 8 * len(intent_missing)
    score -= 18 * len(forbidden_terms_found)
    if topic_type in RECOGNIZED_TOPIC_TYPES and topic_profile.get("generator_source") != "specialized":
        score -= 35
    score = max(0, min(100, score))
    validation_passed = score >= 80 and not forbidden_terms_found and not missing_required_terms and not missing_required_sections and not intent_missing and len(missing_intro_terms) <= 1
    return {
        "title_body_relevance_score": float(score),
        "detected_topic_type": topic_type,
        "selected_contract": topic_profile.get("selected_contract") or topic_type,
        "selected_generator": topic_profile.get("selected_generator"),
        "generator_source": topic_profile.get("generator_source"),
        "search_intent": search_intent,
        "validation_passed": validation_passed,
        "missing_intro_terms": missing_intro_terms,
        "missing_required_terms": missing_required_terms,
        "missing_required_sections": missing_required_sections,
        "missing_intent_requirements": intent_missing,
        "forbidden_terms_found": forbidden_terms_found,
        "regenerated_due_to_validation": False,
    }

def _today_in_timezone(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()


def was_daily_draft_generated_today(db: Session, timezone: str = "Asia/Jerusalem") -> bool:
    today = _today_in_timezone(timezone)
    start_local = datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    end_local = (datetime.combine(today, datetime.max.time(), tzinfo=ZoneInfo(timezone))).astimezone(UTC)
    return db.query(ContentArticleDraft).filter(ContentArticleDraft.created_at >= start_local, ContentArticleDraft.created_at <= end_local).first() is not None


def _slugify(text: str) -> str:
    slug = SLUG_OVERRIDES.get(text)
    if slug:
        return slug
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return normalized or "bbq-hebrew-guide"


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

# ... keep rest unchanged by importing from existing file snippets

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


def _build_article_html(
    title: str,
    keyword: str,
    related: list[dict[str, str | float]],
    *,
    topic_profile: dict[str, object] | None = None,
) -> str:
    profile = topic_profile or _classify_topic(title, keyword, "")
    if profile["selected_generator"] == "picanha_specialized":
        return (
            "<p><strong>פיקניה על הגריל</strong> דורשת עבודה מדויקת עם שכבת השומן, חום דו-אזורי ומדחום ליבה כדי להגיע לטעם ברזילאי אמיתי.</p>"
            "<h2>כלים ושיטת עבודה שלב-אחר-שלב</h2><p>מכינים גריל עם אזור ישיר ועקיף, מדחום ליבה, מלקחיים, סכין חדה וקרש חיתוך; מתחילים בהכנה, עוברים לצלייה מבוקרת ומסיימים בחיתוך נכון.</p>"
            "<h2>הכנת הנתח: שכבת שומן, מלח גס ותזמון</h2><p>משאירים שכבת שומן בעובי אחיד, חורצים קלות את השומן וממליחים במלח גס 20–40 דקות לפני צלייה.</p>"
            "<h2>שתי שיטות שעובדות: צריבה הפוכה או שיפוד ברזילאי</h2><p>אפשר להתחיל בחום עקיף ואז צריבה חזקה בסוף (Reverse Sear), או להשחיל לקשת ברזילאית ולצלות בסיבובים קצרים מעל חום ישיר.</p>"
            "<h2>טמפרטורת יעד לפיקניה</h2><p>מורידים מהאש ב-54–56°C למדיום רייר ומאפשרים עליה קלה במנוחה.</p>"
            "<h2>ניהול התלקחויות מהשומן</h2><p>שכבת השומן מטפטפת במהירות: עובדים עם אזור קר לבקרת להבות ומסובבים את הנתח במקום להזיז כל הזמן.</p>"
            "<h2>חיתוך נכון ומנוחה לפני הגשה</h2><p>נותנים מנוחה 7–10 דקות ומבצעים חיתוך נגד הסיבים לפרוסות עסיסיות ורכות.</p>"
            "<h2>טעויות נפוצות בפיקניה</h2><p>הסרת כל שכבת השומן, חיתוך עם הסיבים או צלייה ללא מדחום גורמים לנתח יבש ופחות עסיסי.</p>"
            "<h2>שאלות נפוצות על פיקניה</h2><h3>אפשר בלי מדחום?</h3><p>אפשר, אבל פחות עקבי. במדחום ליבה מקבלים דיוק בכל צלייה.</p><h3>כמה עבה לפרוס?</h3><p>פרוסות בעובי בינוני שומרות איזון בין עסיסיות לצריבה.</p>"
            "<p><strong>CTA:</strong> רוצים תוצאה יציבה בכל צלייה? הוסיפו מדחום איכותי ומלח גס לעמדת העבודה.</p>"
        )
    if _topic_kind(title, keyword) == "wings":
        return (
            "<p>כנפיים קריספיות על הגריל מכינים בשליטה על ייבוש, חום עקיף, חום ישיר ותזמון גלייז. הנה שיטה מדויקת שעובדת.</p>\n"
            "<h2>כלים ושיטה שלב-אחר-שלב</h2><p>מכינים גריל עם אזור חום עקיף וישיר, מדחום ליבה, מלקחיים, קערת תיבול ורשת נקייה; עובדים בשלבים של ייבוש, בישול עקיף, קריספינג וגלייז בסוף.</p>\n"
            "<h2>ייבוש לפני הצלייה הוא המפתח לעור קריספי</h2><p>יבשו את הכנפיים היטב עם נייר סופג והשאירו 30–120 דקות חשופות במקרר לייבוש פני שטח.</p>\n"
            "<h2>שיטת בייקינג פאודר / קורנפלור (אופציונלי)</h2><p>ערבבו מעט בייקינג פאודר ללא אלומיניום או קורנפלור בתיבול היבש לשיפור קריספיות העור.</p>\n"
            "<h2>חום ישיר מול עקיף</h2><p>התחילו באזור חום עקיף כדי לבשל אחיד, ואז העבירו לחום ישיר קצר לצריבה וקריספינג.</p>\n"
            "<h2>טמפרטורת יעד פנימית בטוחה</h2><p>הכנפיים חייבות להגיע לפחות ל-74°C בחלק העבה ליד העצם.</p>\n"
            "<h2>שלב הקריספינג בסוף</h2><p>בסיום, 1–2 דקות לכל צד מעל אש ישירה לקבלת עור זהוב-חום וקריספי.</p>\n"
            "<h2>רוטב/גלייז רק בסוף</h2><p>סוכרים נשרפים מהר. מוסיפים גלייז רק בדקות האחרונות כדי להימנע מטעם מר ושרוף.</p>\n"
            "<h2>טעויות נפוצות בכנפיים</h2><p>דילוג על ייבוש, הוספת גלייז מוקדם מדי או עבודה בלי מדחום פוגעים בקריספיות ובבטיחות.</p>\n"
            "<h2>שאלות נפוצות</h2><h3>איך להימנע מסוכר שרוף?</h3><p>מדללים גלייז מתוק, מברישים שכבה דקה, ומרחיקים מהלהבה הגבוהה.</p>\n"
            "<h3>כמה מנוחה צריך?</h3><p>מנוחה קצרה בלבד: 2–3 דקות. כנפיים לא צריכות מנוחה ארוכה כמו סטייק.</p>\n"
            "<h3>FAQ לכנפיים</h3><p>האם אפשר בלי בייקינג פאודר? כן. פשוט להאריך ייבוש וקריספינג ישיר.</p>\n"
        )
    if profile["selected_generator"] == "charcoal_comparison_specialized":
        links_html = "".join([f"<li><a href='{p['url']}'>{p['title']}</a></li>" for p in related[:4]])
        return (
            "<p><strong>פחם / פחם קוקוס</strong> הוא נושא של בחירת דלק לגריל: פחם קוקוס מול פחם עץ משפיעים אחרת על זמן בעירה, יציבות חום, עשן וכמות אפר.</p>"
            "<h2>מה ההבדל בין פחם קוקוס לפחם עץ</h2><p>פחם קוקוס מיוצר לרוב כבריקטים צפופים מסיבי קוקוס, ולכן הוא נשרף לאט ואחיד יותר. פחם עץ טבעי עשוי גושי עץ מפוחמים, נדלק מהר יותר ונותן אופי עשן טבעי ומגוון.</p>"
            "<h2>יתרונות של פחם קוקוס ושל פחם עץ</h2><ul><li><strong>פחם קוקוס:</strong> זמן בעירה ארוך, יציבות חום טובה ופחות אפר בסיום.</li><li><strong>פחם עץ:</strong> הדלקה מהירה, תגובת חום זריזה וארומת עשן טבעית שמתאימה לצלייה קצרה.</li></ul>"
            "<h2>חסרונות שחשוב להכיר</h2><ul><li><strong>פחם קוקוס:</strong> לעיתים נדלק לאט יותר ודורש ארובה או מדליק איכותי.</li><li><strong>פחם עץ:</strong> זמן בעירה קצר יותר, יותר שינויי חום ולעיתים יותר אפר לפי איכות הייצור.</li></ul>"
            "<h2>למי מתאים כל אחד</h2><p>פחם קוקוס מתאים למי שרוצה יציבות חום לאורך זמן, בישול עקיף או אירוח ארוך. פחם עץ מתאים למי שמחפש תגובה מהירה, עשן טבעי וצלייה ישירה של נתחים קצרים, ירקות או שיפודים.</p>"
            "<h2>טבלת השוואה: פחם קוקוס מול פחם עץ</h2><table><thead><tr><th>קריטריון</th><th>פחם קוקוס</th><th>פחם עץ</th></tr></thead><tbody><tr><td>זמן בעירה</td><td>ארוך ויציב</td><td>בינוני ותלוי בגודל הגושים</td></tr><tr><td>יציבות חום</td><td>גבוהה ומתאימה לצלייה ארוכה</td><td>משתנה אך מגיבה מהר לפתיחת אוויר</td></tr><tr><td>עשן</td><td>עדין ונקי יחסית</td><td>מודגש וטבעי יותר</td></tr><tr><td>אפר</td><td>בדרך כלל פחות אפר</td><td>יותר אפר לפי איכות הפחם</td></tr></tbody></table>"
            "<h2>המלצה מעשית לבחירה</h2><p>לאירוח ארוך, עבודה עם מכסה סגור או צורך בחום יציב – בחרו פחם קוקוס. לצלייה קצרה, חום מהיר וטעם עשן בולט – בחרו פחם עץ איכותי. מי שמחזיק את שניהם יכול להתחיל בפחם עץ להדלקה מהירה ולהוסיף פחם קוקוס לשמירה על יציבות.</p>"
            "<h2>שאלות נפוצות</h2><h3>מה מפיק פחות אפר?</h3><p>ברוב המקרים פחם קוקוס איכותי מפיק פחות אפר מפחם עץ פשוט.</p><h3>מה עדיף לגריל פתוח?</h3><p>לצלייה קצרה בגריל פתוח פחם עץ נוח ומהיר; לגריל עם מכסה וזמן ארוך פחם קוקוס יציב יותר.</p>"
            "<h2>מוצרים רלוונטיים באתר</h2>" + (f"<ul>{links_html}</ul>" if links_html else "<p>כרגע אין קישורים פנימיים רלוונטיים להצגה.</p>")
        )
    if keyword == "שבבי עץ לעישון":
        links_html = "".join([f"<li><a href='{p['url']}'>{p['title']}</a></li>" for p in related[:4]])
        return (
            "<p>שבבי עץ לעישון משנים לחלוטין את תוצאת הברביקיו: סוג העץ, כמות העשן והטמפרטורה קובעים עומק טעם, צבע ואיזון מרירות.</p>\n"
            "<h2>Hickory, Oak, Apple, Mesquite – מה ההבדל בטעם?</h2>\n"
            "<p><strong>Hickory</strong> נותן עשן חזק, אגוזי ובייקוני; <strong>Oak</strong> מאוזן ומתאים לבישול ארוך; <strong>Apple wood</strong> מתקתק ועדין; <strong>Mesquite</strong> עוצמתי, אדמתי ומהיר.</p>\n"
            "<h2>סוגי שבבי עץ לעישון והטעמים שלהם</h2>\n"
            "<ul><li><strong>Hickory</strong> – חזק, עמוק ובייקוני; מתאים לבקר ולכתף.</li><li><strong>Oak</strong> – בינוני ומאוזן; עובד מצוין לבקר, טלה וירקות.</li><li><strong>Apple</strong> – עדין-מתקתק; מושלם לעוף, דגים וירקות.</li><li><strong>Mesquite</strong> – עוצמתי מאוד ואדמתי; מתאים לסטייקים קצרים בלבד.</li><li><strong>Cherry</strong> – פירותי ועדין, מוסיף צבע אדמדם יפה לעוף וחזיר.</li></ul>\n"
            "<h2>עוצמת עשן והתאמת עץ לסוג בשר</h2>\n"
            "<p>Brisket וצלעות בקר מסתדרים עם Hickory/Oak. עוף והודו נהנים מ-Apple. Mesquite מתאים לסטייקים קצרים, ובמינון נמוך בלבד בבישול ארוך.</p>\n"
            "<h2>איזה שבבי עץ מתאימים לכל סוג בשר?</h2>\n"
            "<ul><li><strong>בקר:</strong> Hickory + Oak לעומק עשן בינוני-חזק.</li><li><strong>עוף:</strong> Apple או Cherry לעשן עדין שלא משתלט.</li><li><strong>דגים:</strong> Apple בלבד או Oak עדין מאוד.</li><li><strong>ירקות:</strong> Oak עדין או Cherry למתיקות קלה.</li></ul>\n"
            "<h2>מיתוס ההשריה: האם צריך להשרות שבבים?</h2>\n"
            "<p>ברוב המעשנות אין צורך להשרות שבבים. השריה מייצרת בעיקר אדים, לא עשן נקי. עדיף שבב יבש וזרימת אוויר יציבה לקבלת thin blue smoke.</p>\n"
            "<h2>טמפרטורות מעשנה מומלצות</h2>\n"
            "<p>עישון קלאסי: 107–135°C. עוף: 135–160°C לסיום עור פריך. ניטור פנימי חשוב יותר מטמפ' תא בלבד, עם מדחום דיגיטלי כפול.</p>\n"
            "<h2>כמה שבבים מוסיפים ומתי?</h2>\n"
            "<p>מוסיפים חופן קטן (כ-1/2 כוס) כל 30–45 דקות בתחילת הבישול, במיוחד ב-60–90 הדקות הראשונות. עשן סמיך ולבן מצביע על שריפה לא נקייה; יעד הוא thin blue smoke, דק וכחלחל.</p>\n"
            "<h3>טעויות נפוצות</h3><p>שימוש יתר ב-Mesquite, פתיחת מכסה תכופה, והוספת שבבים רטובים גורמים למרירות (bitter smoke) ולחוסר יציבות תרמית.</p>\n"
            "<h3>טיפ מקצועי</h3><p>לתוצאה מאוזנת ערבבו Oak עם Apple ביחס 70/30 לבקר ארוך, ו-Apple בלבד לעוף ודגים.</p>\n"
            "<h3>הבדלי טעם בין עצים</h3><p>Hickory מדגיש עומק ועוצמה, Oak מאזן, Apple מוסיף מתיקות עדינה, Cherry פירותי, ו-Mesquite מתאים למינון קצר ומדויק.</p>\n"
            "<h2>מוצרים משלימים</h2>\n"
            + (f"<ul>{links_html}</ul>\n" if links_html else "<p>כרגע אין קישורים פנימיים רלוונטיים להצגה.</p>\n")
        )
    if profile["selected_generator"] == "basalt_stones_specialized":
        links_html = "".join([f"<li><a href='{p['url']}'>{p['title']}</a></li>" for p in related[:4]])
        return (
            "<p><strong>אבני בזלת לגריל</strong> הן אבני חום לגריל גז שמטרתן לשפר פיזור חום, לצמצם התלקחויות ולעזור לצלייה יציבה יותר.</p>"
            "<h2>מה זה אבני בזלת לגריל</h2><p>אבני בזלת יושבות באזור החום של גריל גז, אוגרות אנרגיה ומחזירות אותה בצורה אחידה יותר אל הרשת.</p>"
            "<h2>יתרונות: פיזור חום והפחתת התלקחויות</h2><p>האבנים מאזנות נקודות חמות, משפרות פיזור חום ושומן מטפטף עליהן במקום ישירות למבער, ולכן יש פחות התלקחויות וקפיצות חום.</p>"
            "<h2>איך לבחור אבני בזלת לגריל גז</h2><p>בודקים התאמה להוראות היצרן, גודל שמתאים למגש החום, שכבה שאינה חוסמת אוויר ואיכות אבנים שלא מתפוררות מהר.</p>"
            "<h2>מתי להשתמש ומתי לקנות סט חדש</h2><p>משתמשים בהן כשגריל גז מאבד יציבות חום או כשיש יותר מדי התלקחויות. קונים או מחליפים כשהאבנים סדוקות, מתפוררות או מלאות שומן שרוף.</p>"
            "<h2>מיקום מעל המבערים/מתחת לרשת לפי מבנה הגריל</h2><p>בגרילים מסוימים מניחים מעל המבערים ובאחרים מתחת לרשת נשיאת החום. שומרים מעבר אוויר ולא מעמיסים שכבה צפופה מדי.</p>"
            "<h2>ניקוי והחלפה</h2><p>ניקוי והחלפה עושים רק אחרי קירור מלא: מנקים שומן יבש, מחליפים אבנים סדוקות ושומרים על שכבה אחידה בכל תא חום.</p>"
            "<h2>שאלות נפוצות</h2><h3>כל כמה זמן מחליפים?</h3><p>תלוי בתדירות שימוש; כשיש סדקים רבים או ירידת ביצועים מורגשת.</p><h3>זה מתאים לכל גריל?</h3><p>רק לדגמים שתומכים באבני בזלת/לבה לפי היצרן.</p>"
            "<h2>מוצרים רלוונטיים באתר</h2>"
            + (f"<ul>{links_html}</ul>" if links_html else "<p>כרגע אין קישורים פנימיים רלוונטיים להצגה.</p>")
            + "<p><strong>CTA:</strong> רוצים צלייה יציבה יותר בגריל גז? התאימו סט אבני בזלת למבנה הגריל שלכם.</p>"
        )
    links_html = "".join([f"<li><a href='{p['url']}'>{p['title']}</a></li>" for p in related[:4]])
    required_terms = [str(term) for term in profile.get("required_terms", []) if isinstance(term, str) and term.strip()]
    related_keywords = [str(term) for term in profile.get("related_keywords", []) if isinstance(term, str) and term.strip()]
    topical_focus_line = " · ".join(dict.fromkeys((required_terms + related_keywords)[:6]))
    parts = [
        f"<p>{title} הוא נושא שמכריע אם תקבלו תוצאה בינונית או מנה שמרגישה כמו מסעדת בשרים מקצועית. במדריך הזה תקבלו שיטה ברורה, מדידה וישימה בבית.</p>\n",
        f"<p><strong>מיקוד מקצועי:</strong> {topical_focus_line}</p>\n" if topical_focus_line else "",
        "<h2>למה הנושא הזה חשוב באמת</h2>\n",
        f"<p>כשעובדים נכון עם {keyword}, מקבלים שליטה בטמפרטורה, מרקם יציב וטעם עמוק יותר. הטעויות הקטנות קורות בדיוק בנקודות של חום, זמן ומנוחה – ושם רוב התוצאות נופלות.</p>\n",
        "<h2>ציוד ומוצרים שכדאי להכין מראש</h2>\n",
        "<ul><li><strong>מדחום דיגיטלי</strong> למדידת טמפ' פנימית מדויקת.</li><li><strong>גריל עם אזור ישיר ועקיף</strong> לניהול חום נכון.</li><li><strong>רשת נקייה ומשומנת</strong> כדי למנוע הדבקות וקריעה.</li><li><strong>מלקחיים ארוכים</strong> להפיכה בטוחה בלי איבוד נוזלים.</li></ul>\n",
        "<h2>שיטת עבודה שלב-אחר-שלב</h2>\n",
        "<p><strong>שלב 1:</strong> חימום מוקדם 15–20 דקות עד אזור חם של 230–260°C ואזור עקיף של 160–190°C.</p>\n",
        "<p><strong>שלב 2:</strong> ייבוש עדין של חומר הגלם ותיבול מאוזן 20–40 דקות לפני הצלייה.</p>\n",
        "<p><strong>שלב 3:</strong> סגירה מהירה 2–4 דקות לכל צד לקבלת צבע וקריספיות.</p>\n",
        "<p><strong>שלב 4:</strong> העברה לאזור עקיף עד טמפ' יעד פנימית (למשל 74°C לעוף, 54–57°C למדיום-רייר בקר).</p>\n",
        "<p><strong>שלב 5:</strong> מנוחה 5–10 דקות לפני הגשה כדי לשמור על עסיסיות.</p>\n",
        "<h2>טעויות נפוצות ואיך להימנע מהן</h2>\n",
        "<ul><li>הפיכה מוקדמת מדי – יוצרת קריעה במקום צריבה.</li><li>עבודה בלי מדחום – גורמת לבישול יתר.</li><li>חוסר מנוחה – מוציא נוזלים לצלחת במקום לביס.</li><li>מתיקות גבוהה מוקדם מדי – גלייז נשרף.</li></ul>\n",
        "<h2>טיפים מקצועיים לשדרוג</h2>\n",
        "<p>עבדו בשיטת שתי שכבות תיבול: שכבה יבשה לפני חום ושכבת סיום עדינה אחרי מנוחה. הוסיפו עשן רק בתחילת הבישול (8–15 דקות) כדי למנוע מרירות. שמרו על מכסה סגור ככל האפשר ליציבות תרמית.</p>\n",
        "<h2>קישורים פנימיים ומוצרים משלימים</h2>\n",
        f"<ul>{links_html}</ul>\n" if links_html else "<p>כרגע אין קישורים פנימיים רלוונטיים להצגה.</p>\n",
        "<h2>שאלות נפוצות</h2>\n",
        "<h3>איזו טמפרטורה הכי חשובה למדוד?</h3><p>הטמפרטורה הפנימית של הנתח. זו המדידה היחידה שמבטיחה תוצאה עקבית.</p>\n",
        "<h3>כמה זמן מנוחה באמת צריך?</h3><p>בדרך כלל 5–10 דקות לנתחים רגילים ו-15 דקות לנתחים גדולים יותר.</p>\n",
        "<h3>מתי מוסיפים רוטב או גלייז?</h3><p>רק בשלב הסופי של הצלייה כדי למנוע שריפה של סוכרים.</p>\n",
        (
            "<h2>טרמינולוגיה שחייבים להכיר בנושא הזה</h2>\n<ul>"
            + "".join([f"<li><strong>{term}</strong>: נקודת בדיקה מעשית לביצוע נכון.</li>" for term in required_terms[:6]])
            + "</ul>\n"
        ) if required_terms else "",
        "<hr><p>רוצים לשדרג את הצלייה כבר בארוחה הקרובה? בחרו מוצר אחד מתאים מהרשימה, נסו את השיטה במדויק ותראו הבדל כבר מהסבב הראשון.</p>",
    ]
    body = "".join(parts)
    if related:
        related_html = "".join([f"<li><a href=\"{p['url']}\">{p['title']}</a></li>" for p in related[:4]])
        body += f"\n<h2>מוצרים רלוונטיים באתר</h2>\n<ul>{related_html}</ul>"
    return body


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
    body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
    body, injected_links = inject_internal_links_into_html(body, related)
    validation = validate_article_relevance(title, keyword, body, topic_profile)
    if not validation["validation_passed"]:
        regenerated_body, _ = _remove_h1_tags(_build_article_html(title, keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related)
        validation = validate_article_relevance(title, keyword, body, topic_profile)
        validation["regenerated_due_to_validation"] = True
    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "איזו טמפרטורה הכי חשובה למדוד?", "acceptedAnswer": {"@type": "Answer", "text": "הטמפרטורה הפנימית של הנתח."}},
            {"@type": "Question", "name": "כמה זמן מנוחה באמת צריך?", "acceptedAnswer": {"@type": "Answer", "text": "בדרך כלל 5–10 דקות."}},
            {"@type": "Question", "name": "מתי מוסיפים רוטב או גלייז?", "acceptedAnswer": {"@type": "Answer", "text": "בשלב הסופי כדי למנוע שריפה."}},
        ],
    }
    kind = _topic_kind(title, keyword)
    section_prompts = [
        {"section": "פתיח", "placement_hint": "[IMAGE_1_HERE]", "prompt": f"realistic outdoor grill photo about {keyword}, no text, no logos"},
    ]
    featured_prompt = {
        "wings": "crispy chicken wings on grill grates, golden brown skin, BBQ glaze on side, light smoke, realistic outdoor grill photography, no text, no logos",
        "basalt": "realistic close-up of black basalt lava stones inside a gas grill, glowing heat, steak grilling above, outdoor BBQ, natural light, ultra realistic, no text, no logos",
        "wood_chips": "wood chips in smoker box with thin blue smoke inside grill smoker, meat in background, realistic BBQ photography, no text, no logos",
        "charcoal": "coconut charcoal briquettes next to natural lump wood charcoal, BBQ fuel comparison, no meat, realistic photography, no text, no logos",
    }.get(kind, f"realistic outdoor grill photography focused on {keyword}, no text, no logos")
    draft = ContentArticleDraft(
        status="CONTENT_DRAFT", topic_title=title, title=title, slug=slug,
        meta_title=f"{title} | Compass Grill",
        meta_description=f"{title} - מדריך מעשי בעברית עם שלבים, טמפרטורות, טעויות נפוצות וטיפים מקצועיים.",
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
    setattr(draft, "link_match_debug", {**topic_profile, **validation})
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
    body, _ = _remove_h1_tags(_build_article_html(topic_title, focus_keyword, related, topic_profile=topic_profile))
    body, injected_links = inject_internal_links_into_html(body, related)
    validation = validate_article_relevance(topic_title, focus_keyword, body, topic_profile)
    if not validation["validation_passed"]:
        regenerated_body, _ = _remove_h1_tags(_build_article_html(topic_title, focus_keyword, related, topic_profile=topic_profile))
        body, injected_links = inject_internal_links_into_html(regenerated_body, related)
        validation = validate_article_relevance(topic_title, focus_keyword, body, topic_profile)
        validation["regenerated_due_to_validation"] = True
    section_prompts = [
        {"section": "פתיח", "placement_hint": "[IMAGE_1_HERE]", "prompt": "Close-up of different wood chip types by texture and color (Hickory, Oak, Apple, Mesquite, Cherry), physically separated piles, no text in image, realistic studio lighting"},
        {"section": "שלב-אחר-שלב", "placement_hint": "[IMAGE_2_HERE]", "prompt": "Smoker box filled with wood chips producing thin blue smoke inside a grill smoker chamber, realistic BBQ photo, no text"},
    ]
    kind = _topic_kind(topic_title, focus_keyword)
    prompt_map = {
        "wings": "crispy chicken wings on grill grates, golden brown skin, BBQ glaze on side, light smoke, realistic outdoor grill photography, no text, no logos",
        "basalt": "realistic close-up of black basalt lava stones inside a gas grill, glowing heat, steak grilling above, outdoor BBQ, natural light, ultra realistic, no text, no logos",
        "wood_chips": "wood chips in smoker box with thin blue smoke inside grill smoker, meat in background, realistic BBQ photography, no text, no logos",
        "picanha": "picanha steak with fat cap on brazilian skewer over charcoal grill, reverse-sear style, realistic BBQ photography, no text, no logos",
        "charcoal": "coconut charcoal briquettes next to natural lump wood charcoal, BBQ fuel comparison, no meat, realistic photography, no text, no logos",
    }
    contract_prompt = str((topic_profile.get("contract") or {}).get("image_prompt_pattern", "")) if isinstance(topic_profile.get("contract"), dict) else ""
    featured_prompt = prompt_map.get(kind) or (contract_prompt.format(keyword=focus_keyword) if contract_prompt else "realistic outdoor grill photography focused on the specific topic ingredient/tool, no text, no logos")
    contract = topic_profile.get("contract") if isinstance(topic_profile.get("contract"), dict) else {}
    meta_title = f"{focus_keyword} | Compass Grill"
    meta_pattern = str(contract.get("meta_pattern") or "{keyword}: מדריך ממוקד לפי כוונת חיפוש עם שלבים מעשיים, FAQ וכלים מתאימים.")
    meta_description = meta_pattern.format(keyword=focus_keyword)[:160]
    draft = ContentArticleDraft(
        status="CONTENT_DRAFT", topic_title=topic_title, title=topic_title, slug=slug,
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
    setattr(draft, "link_match_debug", {**discovery_debug, **topic_profile, **validation, "detected_topic_type": topic_profile["topic_type"], "forbidden_terms_removed": []})
    return draft
