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
    ("איך לבחור שבבי עץ לעישון בשר", "שבבי עץ לעישון", "informational"),
    ("ההבדל בין פלט לעישון לשבבי עץ", "פלט לעישון מול שבבי עץ", "comparison"),
    ("איך לנקות מעשנה אחרי עישון ארוך", "ניקוי מעשנה", "how-to"),
    ("מדריך עישון בריסקט למתחילים", "עישון בריסקט", "how-to"),
    ("טאבון גז או טאבון עצים", "טאבון גז מול טאבון עצים", "comparison"),
    ("איך לבחור גריל גז לגינה", "בחירת גריל גז", "commercial"),
    ("פיקניה על הגריל – מדריך מלא", "פיקניה על הגריל", "how-to"),
    ("איך להשתמש בנייר קצבים בעישון בשר", "נייר קצבים לעישון", "how-to"),
    ("ההבדל בין פחם קוקוס לפחם עץ", "פחם קוקוס מול פחם עץ", "comparison"),
    ("איך לבחור מדחום לבשר", "מדחום לבשר", "commercial"),
    ("איך לצלות אנטריקוט נכון", "צליית אנטריקוט", "how-to"),
    ("אפקט מייארד בבשר: מדע וטעם", "אפקט מייארד בבשר", "scientific"),
    ("איך להכין כנפיים קריספיות על הגריל", "כנפיים על הגריל", "how-to"),
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
    return "grill-smoking-guide", "hard_fallback"


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


def _semantic_topic_match_score(topic: str, product: object) -> float:
    title = _safe_product_title(product)
    slug = getattr(product, "slug", "") or ""
    category = getattr(product, "category_name", "") or ""
    topic_tokens = _tokenize_hebrew(topic)
    target_tokens = _tokenize_hebrew(f"{title} {slug} {category}")
    overlap = len(topic_tokens & target_tokens)
    score = overlap * 20
    if any(k in target_tokens for k in {"גריל", "bbq", "smoker", "מעשנה", "שבבי", "עישון"}):
        score += 30
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


def _related_products(db: Session, topic: str, limit: int = 6) -> list[dict[str, str | float]]:
    products = db.query(IStoreProduct).order_by(IStoreProduct.updated_at.desc()).limit(40).all()
    out: list[dict[str, str | float]] = []
    for p in products:
        title = _safe_product_title(p)
        url = _safe_product_url(p)
        if not title or not url:
            continue
        score = _semantic_topic_match_score(topic, p)
        if topic == "שבבי עץ לעישון":
            score = min(100.0, score + _wood_link_priority_score(p))
        if score < 20:
            continue
        out.append({"title": title, "url": url, "semantic_topic_match_score": score, "relatedness_score": score})
    out.sort(key=lambda item: float(item.get("semantic_topic_match_score", 0)), reverse=True)
    return out[: max(3, min(limit, 6))]


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


def _build_article_html(title: str, keyword: str, related: list[dict[str, str | float]]) -> str:
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
    links_html = "".join([f"<li><a href='{p['url']}'>{p['title']}</a></li>" for p in related[:4]])
    return (
        f"<p>{title} הוא נושא שמכריע אם תקבלו תוצאה בינונית או מנה שמרגישה כמו מסעדת בשרים מקצועית. במדריך הזה תקבלו שיטה ברורה, מדידה וישימה בבית.</p>\n"
        "<h2>למה הנושא הזה חשוב באמת</h2>\n"
        f"<p>כשעובדים נכון עם {keyword}, מקבלים שליטה בטמפרטורה, מרקם יציב וטעם עמוק יותר. הטעויות הקטנות קורות בדיוק בנקודות של חום, זמן ומנוחה – ושם רוב התוצאות נופלות.</p>\n"
        "<h2>ציוד ומוצרים שכדאי להכין מראש</h2>\n"
        "<ul><li><strong>מדחום דיגיטלי</strong> למדידת טמפ' פנימית מדויקת.</li><li><strong>גריל עם אזור ישיר ועקיף</strong> לניהול חום נכון.</li><li><strong>רשת נקייה ומשומנת</strong> כדי למנוע הדבקות וקריעה.</li><li><strong>מלקחיים ארוכים</strong> להפיכה בטוחה בלי איבוד נוזלים.</li></ul>\n"
        "<h2>שיטת עבודה שלב-אחר-שלב</h2>\n"
        "<p><strong>שלב 1:</strong> חימום מוקדם 15–20 דקות עד אזור חם של 230–260°C ואזור עקיף של 160–190°C.</p>\n"
        "<p><strong>שלב 2:</strong> ייבוש עדין של חומר הגלם ותיבול מאוזן 20–40 דקות לפני הצלייה.</p>\n"
        "<p><strong>שלב 3:</strong> סגירה מהירה 2–4 דקות לכל צד לקבלת צבע וקריספיות.</p>\n"
        "<p><strong>שלב 4:</strong> העברה לאזור עקיף עד טמפ' יעד פנימית (למשל 74°C לעוף, 54–57°C למדיום-רייר בקר).</p>\n"
        "<p><strong>שלב 5:</strong> מנוחה 5–10 דקות לפני הגשה כדי לשמור על עסיסיות.</p>\n"
        "<h2>טעויות נפוצות ואיך להימנע מהן</h2>\n"
        "<ul><li>הפיכה מוקדמת מדי – יוצרת קריעה במקום צריבה.</li><li>עבודה בלי מדחום – גורמת לבישול יתר.</li><li>חוסר מנוחה – מוציא נוזלים לצלחת במקום לביס.</li><li>מתיקות גבוהה מוקדם מדי – גלייז נשרף.</li></ul>\n"
        "<h2>טיפים מקצועיים לשדרוג</h2>\n"
        "<p>עבדו בשיטת שתי שכבות תיבול: שכבה יבשה לפני חום ושכבת סיום עדינה אחרי מנוחה. הוסיפו עשן רק בתחילת הבישול (8–15 דקות) כדי למנוע מרירות. שמרו על מכסה סגור ככל האפשר ליציבות תרמית.</p>\n"
        "<h2>קישורים פנימיים ומוצרים משלימים</h2>\n"
        + (f"<ul>{links_html}</ul>\n" if links_html else "<p>כרגע אין קישורים פנימיים רלוונטיים להצגה.</p>\n")
        + "<h2>שאלות נפוצות</h2>\n"
        "<h3>איזו טמפרטורה הכי חשובה למדוד?</h3><p>הטמפרטורה הפנימית של הנתח. זו המדידה היחידה שמבטיחה תוצאה עקבית.</p>\n"
        "<h3>כמה זמן מנוחה באמת צריך?</h3><p>בדרך כלל 5–10 דקות לנתחים רגילים ו-15 דקות לנתחים גדולים יותר.</p>\n"
        "<h3>מתי מוסיפים רוטב או גלייז?</h3><p>רק בשלב הסופי של הצלייה כדי למנוע שריפה של סוכרים.</p>\n"
        "<hr><p>רוצים לשדרג את הצלייה כבר בארוחה הקרובה? בחרו מוצר אחד מתאים מהרשימה, נסו את השיטה במדויק ותראו הבדל כבר מהסבב הראשון.</p>"
    )


def generate_daily_article_draft(db: Session, *, randomize: bool = False) -> tuple[ContentArticleDraft, bool, datetime | None]:
    if randomize:
        (title, keyword, intent), reused, last_generated_at = select_random_topic(db)
    else:
        title, keyword, intent = _select_topic(db)
        reused = False
        last_generated_at = None
    related = _related_products(db, keyword)
    slug, _slug_source = _fallback_topic_slug(keyword, title)
    body, _ = _remove_h1_tags(_build_article_html(title, keyword, related))
    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "איזו טמפרטורה הכי חשובה למדוד?", "acceptedAnswer": {"@type": "Answer", "text": "הטמפרטורה הפנימית של הנתח."}},
            {"@type": "Question", "name": "כמה זמן מנוחה באמת צריך?", "acceptedAnswer": {"@type": "Answer", "text": "בדרך כלל 5–10 דקות."}},
            {"@type": "Question", "name": "מתי מוסיפים רוטב או גלייז?", "acceptedAnswer": {"@type": "Answer", "text": "בשלב הסופי כדי למנוע שריפה."}},
        ],
    }
    section_prompts = [
        {"section": "פתיח", "placement_hint": "[IMAGE_1_HERE]", "prompt": "Close-up of different wood chip types by texture and color (Hickory, Oak, Apple, Mesquite, Cherry), physically separated piles, no text in image, realistic studio lighting"},
        {"section": "שלב-אחר-שלב", "placement_hint": "[IMAGE_2_HERE]", "prompt": "Smoker box filled with wood chips producing thin blue smoke inside a grill smoker chamber, realistic BBQ photo, no text"},
    ]
    draft = ContentArticleDraft(
        status="CONTENT_DRAFT", topic_title=title, title=title, slug=slug,
        meta_title=f"{title} | Compass Grill",
        meta_description=f"{title} - מדריך מעשי בעברית עם שלבים, טמפרטורות, טעויות נפוצות וטיפים מקצועיים.",
        focus_keyword=keyword, target_intent=intent, article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(related, ensure_ascii=False),
        faq_schema_json=json.dumps(faq_schema, ensure_ascii=False),
        section_image_prompts_json=json.dumps(section_prompts, ensure_ascii=False),
        featured_image_prompt="wood chips in smoker box with thin blue smoke inside grill smoker, meat in background, realistic BBQ photography",
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
