from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import ContentArticleDraft, IStoreProduct

logger = logging.getLogger(__name__)

TOPIC_POOL = [
    ("איך לבחור שבבי עץ לעישון בשר", "שבבי עץ לעישון", "informational"),
    ("ההבדל בין גריל גז לגריל פחמים", "גריל גז מול פחמים", "comparison"),
    ("איך לצלות אנטריקוט נכון", "צליית אנטריקוט", "how-to"),
    ("מדריך עישון בריסקט למתחילים", "עישון בריסקט", "how-to"),
    ("טאבון גז או טאבון עצים", "טאבון גז", "comparison"),
    ("איך לבחור סכין טובה לבשר", "סכין לבשר", "commercial"),
    ("אפקט מייארד בבשר: מדע וטעם", "אפקט מייארד בבשר", "scientific"),
    ("איך להכין כנפיים קריספיות על הגריל", "כנפיים על הגריל", "how-to"),
]


SLUG_OVERRIDES = {
    "איך לבחור שבבי עץ לעישון בשר": "wood-chips-for-smoking-meat",
    "ההבדל בין גריל גז לגריל פחמים": "gas-grill-vs-charcoal-guide",
    "איך לצלות אנטריקוט נכון": "grilled-ribeye-step-by-step",
    "מדריך עישון בריסקט למתחילים": "brisket-smoking-guide",
    "טאבון גז או טאבון עצים": "tabun-gas-vs-tabun-wood",
    "איך לבחור סכין טובה לבשר": "best-meat-knife-buying-guide",
    "אפקט מייארד בבשר: מדע וטעם": "maillard-reaction-meat-guide",
    "איך להכין כנפיים קריספיות על הגריל": "crispy-grilled-wings",
}


def _slugify(text: str) -> str:
    slug = SLUG_OVERRIDES.get(text)
    if slug:
        return slug
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return normalized or "bbq-hebrew-guide"


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


def _related_products(db: Session, topic: str, limit: int = 6) -> list[dict[str, str | float]]:
    products = db.query(IStoreProduct).order_by(IStoreProduct.updated_at.desc()).limit(40).all()
    out: list[dict[str, str | float]] = []
    for p in products:
        title = _safe_product_title(p)
        url = _safe_product_url(p)
        if not title or not url:
            continue
        score = _semantic_topic_match_score(topic, p)
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


def generate_daily_article_draft(db: Session) -> ContentArticleDraft:
    title, keyword, intent = _select_topic(db)
    related = _related_products(db, keyword)
    slug = _slugify(title)
    body = _build_article_html(title, keyword, related)
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
        {"section": "פתיח", "placement_hint": "[IMAGE_1_HERE]", "prompt": f"Hebrew BBQ blog hero showing {slug.replace('-', ' ')}, realistic grill setup and food texture"},
        {"section": "שלב-אחר-שלב", "placement_hint": "[IMAGE_2_HERE]", "prompt": f"Step-by-step cooking process for {slug.replace('-', ' ')}, close-up on grill grates, thermometers, fire zones"},
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
        featured_image_prompt=f"Realistic {slug.replace('-', ' ')} scene, grill, smoke, natural light, no logos",
        image_alt_text=f"{title} - הדגמה על גריל", image_title=f"תמונת שער: {title}", image_caption="הדגמה מעשית של השיטה במאמר.",
        image_filename_slug=f"compass-grill-{slug}", image_style_rules="realistic outdoor BBQ photography",
        generated_image_url=None, uploaded_media_id=None, image_publish_status="NOT_PUBLISHED",
        target_site_section="blog", target_publish_type="article", target_blog_base_url="https://compassgrill.co.il/blog/",
        target_path=f"/blog/{slug}", target_url=f"https://compassgrill.co.il/blog/{slug}", publish_destination_status="ready", featured_image_status="planned",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft
