from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import ContentArticleDraft, IStoreProduct

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


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "compass-grill-article"


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


def _related_products(db: Session, limit: int = 4) -> list[dict[str, str]]:
    products = db.query(IStoreProduct).order_by(IStoreProduct.updated_at.desc()).limit(limit).all()
    out: list[dict[str, str]] = []
    for p in products:
        out.append({"title": p.title or "מוצר גריל", "url": p.product_url or ""})
    if not out:
        out = [
            {"title": "גרילי גז", "url": "/category/gas-grills"},
            {"title": "מעשנות", "url": "/category/smokers"},
        ]
    return out


def generate_daily_article_draft(db: Session) -> ContentArticleDraft:
    title, keyword, intent = _select_topic(db)
    related = _related_products(db)
    english_hint = {
        "שבבי": "wood chips smoking meat",
        "גז": "gas grill vs charcoal",
        "אנטריקוט": "how to grill ribeye",
        "בריסקט": "brisket smoking guide",
        "טאבון": "gas vs wood pizza oven",
        "סכין": "best knife for meat",
        "מייארד": "maillard reaction steak",
        "כנפיים": "crispy grilled wings",
    }
    prompt_hint = next((v for k, v in english_hint.items() if k in title), "outdoor premium bbq")
    body = (
        f"<h1>{title}</h1>\n"
        "<p>במדריך הזה נסביר בצורה ברורה ומעשית איך לבחור נכון, מה עובד בשטח, ואיך להוציא יותר טעם מכל צלייה.</p>\n"
        "<h2>למה זה חשוב לצלייה איכותית?</h2>\n"
        "<p>בחירה נכונה של ציוד וטכניקה תשפיע על טעם, עסיסיות, ושליטה בחום לאורך זמן.</p>\n"
        "<h2>שלבים מעשיים</h2>\n"
        "<h3>שלב 1: הכנה מוקדמת</h3><p>בחרו בשר איכותי, תבלו בעדינות, ותנו לבשר להגיע לטמפרטורת חדר.</p>\n"
        "<h3>שלב 2: שליטה בחום</h3><p>עבדו עם אזור חום ישיר ועקיף כדי לשלוט במידת העשייה.</p>\n"
        "<h2>קישורים פנימיים מומלצים</h2>\n"
        + "".join([f"<p><a href='{p['url']}'>{p['title']}</a></p>" for p in related])
        + "\n<h2>שאלות נפוצות</h2>\n"
        "<h3>כמה זמן לצלות?</h3><p>תלוי בעובי הנתח ובטמפרטורה, לכן מומלץ לעבוד עם מדחום.</p>\n"
        "<h3>איך שומרים על עסיסיות?</h3><p>נותנים לבשר מנוחה של 5–10 דקות לפני חיתוך.</p>"
    )
    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "כמה זמן לצלות?",
                "acceptedAnswer": {"@type": "Answer", "text": "תלוי בעובי הנתח ובטמפרטורה."},
            },
            {
                "@type": "Question",
                "name": "איך שומרים על עסיסיות?",
                "acceptedAnswer": {"@type": "Answer", "text": "נותנים לבשר מנוחה לפני חיתוך."},
            },
        ],
    }
    draft = ContentArticleDraft(
        status="CONTENT_DRAFT",
        topic_title=title,
        title=title,
        slug=_slugify(prompt_hint),
        meta_title=f"{title} | Compass Grill",
        meta_description=f"{title} – מדריך מעשי בעברית עם טיפים לצלייה, עישון ובחירת ציוד נכון.",
        focus_keyword=keyword,
        target_intent=intent,
        article_body=body,
        suggested_related_products_json=json.dumps(related, ensure_ascii=False),
        internal_links_json=json.dumps(related, ensure_ascii=False),
        faq_schema_json=json.dumps(faq_schema, ensure_ascii=False),
        section_image_prompts_json=json.dumps([
            {"section": "הכנה מוקדמת", "prompt": "Close-up of premium kosher beef being seasoned near a modern grill"},
            {"section": "שליטה בחום", "prompt": "Two-zone grilling setup outdoors, realistic food and safe cooking"},
        ], ensure_ascii=False),
        featured_image_prompt=f"Realistic premium BBQ scene, {prompt_hint}, outdoor cooking, natural light, no logos",
        image_alt_text=f"{title} על גריל איכותי בחצר",
        image_title=f"תמונת שער: {title}",
        image_caption="הכנה נכונה וצלייה מדויקת משדרגות כל נתח.",
        image_filename_slug=f"compass-grill-{_slugify(prompt_hint)}",
        image_style_rules=(
            "ultra realistic BBQ / grill / meat / outdoor cooking photography; "
            "premium but natural Israeli BBQ style; realistic meat texture; realistic grill smoke and fire; "
            "clean composition suitable for blog hero image; horizontal website article header format; "
            "no fake logos; no text inside the image; no distorted Hebrew text; no unrealistic food; "
            "no unsafe cooking behavior; no people unless explicitly needed"
        ),
        generated_image_url=None,
        uploaded_media_id=None,
        image_publish_status="NOT_PUBLISHED",
        target_site_section="blog",
        target_publish_type="article",
        target_blog_base_url="https://compassgrill.co.il/blog/",
        target_path=f"/blog/{_slugify(prompt_hint)}",
        target_url=f"https://compassgrill.co.il/blog/{_slugify(prompt_hint)}",
        publish_destination_status="ready",
        featured_image_status="planned",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft
