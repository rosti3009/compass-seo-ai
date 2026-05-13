import re
from datetime import date
from urllib.parse import unquote, urlparse

from app.db.models import GSCKeywordMetric, PageAudit

HEBREW_NIKUD_RE = re.compile(r"[\u0591-\u05C7]")
HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
TOKEN_RE = re.compile(r"[\w\u0590-\u05FF]+", re.UNICODE)

GRILL_TERMINOLOGY = {
    "bbq": "גריל",
    "barbecue": "גריל",
    "barbeque": "גריל",
    "weber": "וובר",
    "napoleon": "נפוליאון",
    "broil": "גריל",
    "king": "קינג",
    "grill": "גריל",
    "grills": "גריל",
    "גרילים": "גריל",
    "גרילרים": "גריל",
    "מנגל": "גריל",
    "מנגלים": "גריל",
    "ברביקיו": "גריל",
    "ברביקיוים": "גריל",
    "מעשנות": "מעשנה",
    "מעשנים": "מעשנה",
    "פלנצ'ות": "פלנצ'ה",
    "פלאנצ'ה": "פלנצ'ה",
    "גז": "גז",
    "פחמים": "פחם",
    "לקנות": "לקנות",
    "חשמליים": "חשמלי",
    "חשמליות": "חשמלי",
}

ENGLISH_PRODUCT_TERMS = {
    "gas": "גז",
    "charcoal": "פחם",
    "electric": "חשמלי",
    "smoker": "מעשנה",
    "smokers": "מעשנה",
    "plancha": "פלנצ'ה",
    "outdoor": "חוץ",
    "kitchen": "מטבח",
    "sale": "מבצע",
    "premium": "פרימיום",
}

TRANSACTIONAL_TERMS = {"לקנות", "קנייה", "קנה", "מחיר", "מבצע", "הנחה", "משלוח", "להזמין", "רכישה", "קופון"}
INFORMATIONAL_TERMS = {"איך", "מה", "מדריך", "טיפים", "הסבר", "תחזוקה", "ניקוי", "מתכון", "כמה", "למה"}
COMPARISON_TERMS = {"לעומת", "השוואה", "מול", "הבדל", "או", "vs", "הכי טוב", "מומלץ"}
LOCAL_TERMS = {"ישראל", "בארץ", "קרוב", "תל אביב", "ירושלים", "חיפה", "ראשון לציון", "פתח תקווה", "חנות", "סניף"}
COMMERCIAL_INVESTIGATION_TERMS = {"חוות דעת", "ביקורת", "מומלץ", "מותג", "מותגים", "דגם", "דגמים", "סקירה", "איכות"}
COMMERCIAL_TERMS = TRANSACTIONAL_TERMS | COMMERCIAL_INVESTIGATION_TERMS | {"אחריות", "יבואן", "מקורי", "מלאי"}

CATEGORY_MARKERS = {
    "collections",
    "collection",
    "categories",
    "category",
    "קטגוריה",
    "גרילי-גז",
    "גרילי-פחמים",
    "מעשנות",
}
PRODUCT_MARKERS = {"products", "product", "p/", "sku", "דגם", "גריל-גז", "מעשנה"}
BRAND_MARKERS = {"brands", "brand", "weber", "napoleon", "broil-king", "וובר", "נפוליאון", "ברויל"}
LOW_STOCK_TERMS = {"אזל", "אחרונים", "במלאי מוגבל", "מלאי מוגבל", "נותרו", "limited", "low stock"}
SEASONAL_TERMS = {"יום העצמאות", "פסח", "קיץ", "חגים", "חג", "מנגל", "על האש", "אירוח"}


def remove_nikud(text: str | None) -> str:
    """Remove Hebrew vowel marks and cantillation from a string."""
    return HEBREW_NIKUD_RE.sub("", text or "")


def _normalize_plural_token(token: str) -> str:
    if token in GRILL_TERMINOLOGY:
        return GRILL_TERMINOLOGY[token]
    if len(token) > 4 and token.endswith("יים"):
        return token[:-3] + "י"
    if len(token) > 4 and token.endswith("ים"):
        return token[:-2]
    if len(token) > 4 and token.endswith("ות"):
        return token[:-2] + "ה"
    return token


def normalize_hebrew_keyword(keyword: str | None) -> str:
    """Normalize Hebrew/English ecommerce keywords into canonical Hebrew grill-search phrasing."""
    cleaned = remove_nikud(keyword).lower().replace("/", " ").replace("-", " ")
    tokens: list[str] = []
    for raw_token in TOKEN_RE.findall(cleaned):
        token = ENGLISH_PRODUCT_TERMS.get(raw_token, raw_token)
        token = GRILL_TERMINOLOGY.get(token, token)
        token = _normalize_plural_token(token)
        if token not in tokens:
            tokens.append(token)
    return " ".join(tokens)


def keyword_tokens(text: str | None) -> set[str]:
    normalized = normalize_hebrew_keyword(text)
    return {token for token in normalized.split() if len(token) > 1}


def classify_intent(query: str | None) -> str:
    normalized = normalize_hebrew_keyword(query)
    haystack = f" {normalized} "
    if any(term in haystack for term in LOCAL_TERMS):
        return "local"
    if any(term in haystack for term in TRANSACTIONAL_TERMS):
        return "transactional"
    if any(term in haystack for term in COMMERCIAL_INVESTIGATION_TERMS):
        return "commercial_investigation"
    if any(term in haystack for term in COMPARISON_TERMS - {"או"}) or " או " in haystack:
        return "comparison"
    if any(term in haystack for term in INFORMATIONAL_TERMS):
        return "informational"
    return "commercial_investigation" if "גריל" in normalized else "informational"


def _length_score(text: str | None, min_len: int, max_len: int) -> int:
    text_len = len(remove_nikud(text).strip())
    if min_len <= text_len <= max_len:
        return 100
    if text_len == 0:
        return 0
    distance = min(abs(text_len - min_len), abs(text_len - max_len))
    return max(35, 100 - distance * 4)


def _hebrew_presence_score(text: str | None) -> int:
    clean = remove_nikud(text)
    if not clean:
        return 0
    hebrew_chars = len(HEBREW_RE.findall(clean))
    alpha_chars = sum(char.isalpha() for char in clean)
    if alpha_chars == 0:
        return 0
    return round(min(1.0, hebrew_chars / alpha_chars) * 100)


def _field_quality(text: str | None, primary_keyword: str, min_len: int, max_len: int) -> dict[str, object]:
    normalized_text = normalize_hebrew_keyword(text)
    normalized_keyword = normalize_hebrew_keyword(primary_keyword)
    keyword_present = bool(normalized_keyword and normalized_keyword in normalized_text)
    length_score = _length_score(text, min_len, max_len)
    hebrew_score = _hebrew_presence_score(text)
    score = round(length_score * 0.45 + hebrew_score * 0.35 + (20 if keyword_present else 0))
    return {
        "score": min(100, score),
        "length_score": length_score,
        "hebrew_presence_score": hebrew_score,
        "contains_primary_keyword": keyword_present,
    }


def commercial_keyword_density(*texts: str | None) -> dict[str, object]:
    tokens = " ".join(normalize_hebrew_keyword(text) for text in texts).split()
    if not tokens:
        return {"score": 0, "density": 0.0, "commercial_terms": []}
    terms = sorted({term for term in COMMERCIAL_TERMS if normalize_hebrew_keyword(term) in tokens})
    normalized_commercial_terms = {normalize_hebrew_keyword(term) for term in COMMERCIAL_TERMS}
    density = len([token for token in tokens if token in normalized_commercial_terms]) / len(tokens)
    score = round(min(100, density * 900))
    return {"score": score, "density": round(density, 4), "commercial_terms": terms}


def keyword_coverage(page: PageAudit, primary_keyword: str, supporting_keywords: list[str]) -> dict[str, object]:
    page_text = " ".join([page.title or "", page.meta_description or "", page.h1 or "", unquote(page.url)])
    page_tokens = keyword_tokens(page_text)
    targets = [primary_keyword, *supporting_keywords]
    covered = [keyword for keyword in targets if keyword_tokens(keyword).issubset(page_tokens)]
    score = round((len(covered) / len(targets)) * 100) if targets else 0
    return {
        "score": score,
        "covered_keywords": covered,
        "missing_keywords": [kw for kw in targets if kw not in covered],
    }


def infer_ecommerce_signals(page: PageAudit, metric: GSCKeywordMetric | None = None) -> dict[str, object]:
    decoded_url = unquote(page.url).lower()
    text = " ".join([decoded_url, page.title or "", page.meta_description or "", page.h1 or ""]).lower()
    page_type = "content_page"
    if any(marker in text for marker in CATEGORY_MARKERS):
        page_type = "category_page"
    if any(marker in text for marker in BRAND_MARKERS):
        page_type = "brand_page"
    if any(marker in text for marker in PRODUCT_MARKERS):
        page_type = "product_page"
    low_stock = any(term in text for term in LOW_STOCK_TERMS)
    high_demand = bool(metric and (metric.impressions >= 500 or metric.clicks >= 25))
    seasonal = any(term in text for term in SEASONAL_TERMS)
    return {
        "page_type": page_type,
        "is_category_page": page_type == "category_page",
        "is_brand_page": page_type == "brand_page",
        "is_product_page": page_type == "product_page",
        "is_low_stock_high_demand": low_stock and high_demand,
        "is_seasonal_product": seasonal,
        "demand_signal": {
            "impressions": metric.impressions if metric else 0,
            "clicks": metric.clicks if metric else 0,
            "high_demand": high_demand,
        },
    }


def israeli_seasonality(target_date: date | None = None) -> list[dict[str, object]]:
    current = target_date or date.today()
    month = current.month
    helpers = [
        {
            "name": "Independence Day grilling",
            "hebrew_name": "יום העצמאות ועל האש",
            "months": [4, 5],
            "keywords": ["גריל ליום העצמאות", "מנגל ליום העצמאות", "על האש"],
            "recommended_action": (
                "Prepare category banners and buying guides for grills, charcoal, gas, and accessories."
            ),
        },
        {
            "name": "Passover hosting",
            "hebrew_name": "פסח ואירוח בחוץ",
            "months": [3, 4],
            "keywords": ["גריל לפסח", "מטבח חוץ לפסח", "אירוח בפסח"],
            "recommended_action": "Promote outdoor kitchen, premium grill, and kosher-for-Passover hosting content.",
        },
        {
            "name": "Summer grilling season",
            "hebrew_name": "עונת המנגלים בקיץ",
            "months": [6, 7, 8, 9],
            "keywords": ["גריל גז לקיץ", "מנגל לגינה", "מטבח חוץ"],
            "recommended_action": "Prioritize commercial grill categories, delivery messaging, and comparison pages.",
        },
        {
            "name": "Israeli holidays and events",
            "hebrew_name": "חגים ואירועים בישראל",
            "months": [9, 10],
            "keywords": ["גריל לחגים", "מתנות לחג", "אירוח בחגים"],
            "recommended_action": "Create holiday gift, hosting, and family event landing pages.",
        },
    ]
    return [{**helper, "active_now": month in helper["months"]} for helper in helpers]


def _recommendations(score: dict[str, object], signals: dict[str, object]) -> list[str]:
    actions: list[str] = []
    if score["title_quality"]["score"] < 75:
        actions.append("Rewrite the Hebrew title with the primary grill keyword, brand, and a commercial modifier.")
    if score["meta_quality"]["score"] < 75:
        actions.append("Add a Hebrew meta description with delivery, warranty, stock, or promotion messaging.")
    if score["keyword_coverage"]["score"] < 70:
        actions.append("Cover missing Hebrew synonyms such as גריל, מנגל, ברביקיו, גז, פחם, and brand variants.")
    if signals["is_low_stock_high_demand"]:
        actions.append("Protect conversion SEO for this low-stock/high-demand product with urgency and alternatives.")
    if signals["is_category_page"]:
        actions.append(
            "Add ecommerce category copy, FAQ schema ideas, and internal links to top-selling grill products."
        )
    return actions


def analyze_page_hebrew_seo(page: PageAudit, metric: GSCKeywordMetric | None = None) -> dict[str, object]:
    primary_keyword = metric.query if metric else page.h1 or page.title or "גריל"
    normalized_primary = normalize_hebrew_keyword(primary_keyword)
    supporting_keywords = ["גריל גז", "מנגל", "ברביקיו", "גריל ישראל"]
    score = {
        "title_quality": _field_quality(page.title, normalized_primary, 30, 65),
        "meta_quality": _field_quality(page.meta_description, normalized_primary, 70, 155),
        "h1_quality": _field_quality(page.h1, normalized_primary, 15, 70),
        "keyword_coverage": keyword_coverage(page, normalized_primary, supporting_keywords),
        "commercial_keyword_density": commercial_keyword_density(page.title, page.meta_description, page.h1),
    }
    total_score = round(
        score["title_quality"]["score"] * 0.22
        + score["meta_quality"]["score"] * 0.22
        + score["h1_quality"]["score"] * 0.18
        + score["keyword_coverage"]["score"] * 0.25
        + score["commercial_keyword_density"]["score"] * 0.13
    )
    signals = infer_ecommerce_signals(page, metric)
    intent = classify_intent(primary_keyword)
    return {
        "url": page.url,
        "domain_supported": urlparse(page.url).hostname == "compassgrill.co.il",
        "primary_keyword": primary_keyword,
        "normalized_primary_keyword": normalized_primary,
        "intent": intent,
        "hebrew_seo_score": total_score,
        "score_breakdown": score,
        "ecommerce_signals": signals,
        "recommendations": _recommendations(score, signals),
    }


def summarize_hebrew_insights(insights: list[dict[str, object]]) -> dict[str, object]:
    if not insights:
        return {"pages_analyzed": 0, "average_hebrew_seo_score": 0, "intent_mix": {}, "page_type_mix": {}}
    intent_mix: dict[str, int] = {}
    page_type_mix: dict[str, int] = {}
    for insight in insights:
        intent = str(insight["intent"])
        page_type = str(insight["ecommerce_signals"]["page_type"])
        intent_mix[intent] = intent_mix.get(intent, 0) + 1
        page_type_mix[page_type] = page_type_mix.get(page_type, 0) + 1
    return {
        "pages_analyzed": len(insights),
        "average_hebrew_seo_score": round(
            sum(int(insight["hebrew_seo_score"]) for insight in insights) / len(insights), 1
        ),
        "intent_mix": intent_mix,
        "page_type_mix": page_type_mix,
    }
