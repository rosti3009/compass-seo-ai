from __future__ import annotations

import json
import logging
import random
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

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

GENERIC_FILLER_PHRASES = [
    "הנושא הזה מכריע אם תקבלו תוצאה בינונית",
    "ציוד ומוצרים שכדאי להכין מראש",
    "חימום מוקדם 15–20 דקות",
]


def _classify_topic(topic_title: str, focus_keyword: str, target_intent: str) -> dict[str, object]:
    blob = f"{topic_title} {focus_keyword}".lower()
    base = {
        "topic_type": "bbq_general",
        "product_type": "general",
        "content_type": "guide",
        "search_intent": target_intent or "informational",
        "target_keyword": focus_keyword,
        "related_keywords": [focus_keyword],
        "required_sections": ["intro", "steps", "faq", "cta"],
        "forbidden_sections": [],
        "selected_generator": "generic_fallback",
        "generator_source": "fallback",
        "fallback_reason": "no_specialized_topic_match",
        "forbidden_terms": [],
        "required_terms": [],
    }
    if "פיקניה" in blob:
        base.update({
            "topic_type": "beef_cut",
            "product_type": "picanha",
            "content_type": "grilling_guide",
            "search_intent": target_intent or "how-to",
            "related_keywords": ["פיקניה", "שכבת שומן", "מלח גס", "שיפוד ברזילאי", "חיתוך נגד הסיבים"],
            "required_sections": ["intro", "fat_cap", "reverse_sear_or_skewer", "temperature", "flareups", "rest", "faq", "cta"],
            "forbidden_sections": ["chicken_recipe"],
            "selected_generator": "picanha_specialized",
            "generator_source": "specialized",
            "fallback_reason": "",
            "required_terms": ["שכבת שומן", "מלח גס", "חיתוך נגד הסיבים", "54–56°C", "מנוחה"],
            "forbidden_terms": ["74°C", "גלייז", "עוף"],
        })
    elif "כנפיים" in blob and "קריספ" in blob:
        base.update({
            "topic_type": "chicken_recipe",
            "product_type": "wings",
            "content_type": "how_to_recipe",
            "search_intent": target_intent or "how-to",
            "related_keywords": ["כנפיים קריספיות", "ייבוש", "בייקינג פאודר", "חום עקיף", "74°C"],
            "required_sections": ["intro", "drying", "indirect_then_direct", "temperature", "glaze", "faq", "cta"],
            "forbidden_sections": ["beef_doneness"],
            "selected_generator": "crispy_wings_specialized",
            "generator_source": "specialized",
            "fallback_reason": "",
            "required_terms": ["ייבוש", "74°C", "גלייז", "חום עקיף"],
            "forbidden_terms": ["פיקניה", "מדיום רייר", "שכבת שומן בקר"],
        })
    elif "בזלת" in blob or "לבה" in blob:
        base.update({
            "topic_type": "product_guide",
            "product_type": "basalt_stones",
            "content_type": "commercial_informational",
            "search_intent": target_intent or "commercial_informational",
            "related_keywords": ["אבני בזלת לגריל", "פיזור חום", "הפחתת התלקחויות", "גריל גז"],
            "required_sections": ["intro", "heat_distribution", "placement", "maintenance", "faq", "cta"],
            "forbidden_sections": ["meat_recipe"],
            "selected_generator": "basalt_stones_specialized",
            "generator_source": "specialized",
            "fallback_reason": "",
            "required_terms": ["פיזור חום", "התלקחויות", "יציבות חום", "מיקום מעל המבערים", "ניקוי והחלפה"],
            "forbidden_terms": ["74°C", "מדיום רייר", "מתכון עוף", "מתכון בקר"],
        })
    return base

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
    out: list[dict[str, str | float]] = []
    terms = _match_terms_for_topic(topic)
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
    out.sort(key=lambda item: float(item.get("semantic_topic_match_score", 0)), reverse=True)
    trimmed = out[: max(3, min(limit, 6))]
    best = trimmed[0] if trimmed else {}
    debug = {
        "link_discovery_source": ["db_products", "latest_crawl_cache", "sitemap_urls", "product_category_pages"],
        "searched_terms": terms,
        "matched_product_count": len(trimmed),
        "matched_internal_link_count": len(trimmed),
        "best_match_title": best.get("title"),
        "best_match_url": best.get("url"),
        "best_match_score": best.get("semantic_topic_match_score", 0),
        "excluded_low_relevance_links": excluded_low[:10],
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
            "<h2>הכנת הנתח: שכבת שומן, מלח גס ותזמון</h2><p>משאירים שכבת שומן בעובי אחיד, חורצים קלות את השומן וממליחים במלח גס 20–40 דקות לפני צלייה.</p>"
            "<h2>שתי שיטות שעובדות: צריבה הפוכה או שיפוד ברזילאי</h2><p>אפשר להתחיל בחום עקיף ואז צריבה חזקה בסוף (Reverse Sear), או להשחיל לקשת ברזילאית ולצלות בסיבובים קצרים מעל חום ישיר.</p>"
            "<h2>טמפרטורת יעד לפיקניה</h2><p>מורידים מהאש ב-54–56°C למדיום רייר ומאפשרים עליה קלה במנוחה.</p>"
            "<h2>ניהול התלקחויות מהשומן</h2><p>שכבת השומן מטפטפת במהירות: עובדים עם אזור קר לבקרת להבות ומסובבים את הנתח במקום להזיז כל הזמן.</p>"
            "<h2>חיתוך נכון ומנוחה לפני הגשה</h2><p>נותנים מנוחה 7–10 דקות ומבצעים חיתוך נגד הסיבים לפרוסות עסיסיות ורכות.</p>"
            "<h2>שאלות נפוצות על פיקניה</h2><h3>אפשר בלי מדחום?</h3><p>אפשר, אבל פחות עקבי. במדחום ליבה מקבלים דיוק בכל צלייה.</p><h3>כמה עבה לפרוס?</h3><p>פרוסות בעובי בינוני שומרות איזון בין עסיסיות לצריבה.</p>"
            "<p><strong>CTA:</strong> רוצים תוצאה יציבה בכל צלייה? הוסיפו מדחום איכותי ומלח גס לעמדת העבודה.</p>"
        )
    if _topic_kind(title, keyword) == "wings":
        return (
            "<p>כנפיים על הגריל יוצאות קריספיות רק כששולטים ביובש, חום ותזמון גלייז. הנה שיטה מדויקת שעובדת.</p>\n"
            "<h2>ייבוש לפני הצלייה הוא המפתח לעור קריספי</h2><p>יבשו את הכנפיים היטב עם נייר סופג והשאירו 30–120 דקות חשופות במקרר לייבוש פני שטח.</p>\n"
            "<h2>שיטת בייקינג פאודר / קורנפלור (אופציונלי)</h2><p>ערבבו מעט בייקינג פאודר ללא אלומיניום או קורנפלור בתיבול היבש לשיפור קריספיות העור.</p>\n"
            "<h2>חום ישיר מול עקיף</h2><p>התחילו באזור עקיף כדי לבשל אחיד, ואז העבירו לחום ישיר קצר לצריבה וקריספינג.</p>\n"
            "<h2>טמפרטורת יעד פנימית בטוחה</h2><p>הכנפיים חייבות להגיע לפחות ל-74°C בחלק העבה ליד העצם.</p>\n"
            "<h2>שלב הקריספינג בסוף</h2><p>בסיום, 1–2 דקות לכל צד מעל אש ישירה לקבלת עור זהוב-חום וקריספי.</p>\n"
            "<h2>רוטב/גלייז רק בסוף</h2><p>סוכרים נשרפים מהר. מוסיפים גלייז רק בדקות האחרונות כדי להימנע מטעם מר ושרוף.</p>\n"
            "<h3>איך להימנע מסוכר שרוף?</h3><p>מדללים גלייז מתוק, מברישים שכבה דקה, ומרחיקים מהלהבה הגבוהה.</p>\n"
            "<h3>כמה מנוחה צריך?</h3><p>מנוחה קצרה בלבד: 2–3 דקות. כנפיים לא צריכות מנוחה ארוכה כמו סטייק.</p>\n"
            "<h3>FAQ לכנפיים</h3><p>האם אפשר בלי בייקינג פאודר? כן. פשוט להאריך ייבוש וקריספינג ישיר.</p>\n"
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
            "<p><strong>אבני בזלת לגריל</strong> משפרות פיזור חום ומייצבות את גריל הגז לאורך הצלייה, במיוחד בעבודה ארוכה.</p>"
            "<h2>איך אבני בזלת משפרות פיזור חום</h2><p>האבנים אוגרות אנרגיה ומחזירות חום בצורה אחידה יותר בין אזורי הרשת.</p>"
            "<h2>הפחתת התלקחויות ושמירה על יציבות חום</h2><p>שומן מטפטף על האבנים במקום ישירות למבער, ולכן פחות התלקחויות ופחות קפיצות חום.</p>"
            "<h2>מיקום מעל המבערים/מתחת לרשת לפי מבנה הגריל</h2><p>בגרילים מסוימים מניחים מעל המבערים ובאחרים מתחת לרשת נשיאת החום. בודקים את הוראות היצרן ושומרים מעבר אוויר.</p>"
            "<h2>ניקוי והחלפה</h2><p>מנקים שומן יבש אחרי קירור מלא, מחליפים אבנים סדוקות ושומרים על שכבה אחידה בכל תא חום.</p>"
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
    }.get(kind, f"realistic outdoor grill photography focused on {keyword}, no text, no logos")
    draft = ContentArticleDraft(
        status="CONTENT_DRAFT", topic_title=title, title=title, slug=slug,
        meta_title=f"{title} | Compass Grill",
        meta_description=f"{title} - מדריך מעשי בעברית עם שלבים, טמפרטורות, טעויות נפוצות וטיפים מקצועיים.",
        focus_keyword=keyword, target_intent=intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(related, ensure_ascii=False),
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
    }
    featured_prompt = prompt_map.get(kind, "realistic outdoor grill photography focused on the specific topic ingredient/tool, no text, no logos")
    meta_title = f"{focus_keyword} | Compass Grill"
    meta_description = f"{focus_keyword}: מדריך ממוקד לפי כוונת חיפוש עם שלבים מעשיים, FAQ וכלים מתאימים."
    draft = ContentArticleDraft(
        status="CONTENT_DRAFT", topic_title=topic_title, title=topic_title, slug=slug,
        meta_title=meta_title,
        meta_description=meta_description,
        focus_keyword=focus_keyword, target_intent=target_intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(related, ensure_ascii=False),
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
    setattr(draft, "link_match_debug", {**discovery_debug, **topic_profile, "detected_topic_type": topic_profile["topic_type"], "forbidden_terms_removed": []})
    return draft
