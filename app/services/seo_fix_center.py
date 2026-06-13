# ruff: noqa: E501
"""Employee-friendly SEO Fix Center task discovery and workflow helpers."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, GSCKeywordMetric, IStoreProduct, PageAudit, SEOFix, SEOTask

ISSUE_TYPES = [
    "broken_link",
    "gsc_404",
    "soft_404",
    "orphan_page",
    "redirect_chain",
    "missing_meta_title",
    "missing_meta_description",
    "duplicate_meta_title",
    "missing_h1",
    "duplicate_h1",
    "image_missing_alt",
    "internal_link_opportunity",
    "product_seo_issue",
]

STATUS_HEBREW = ["חדש", "בבדיקה", "ממתין לאישור", "אושר", "בוצע", "נדחה"]
OPEN_STATUSES = {"חדש", "בבדיקה", "ממתין לאישור", "אושר"}
LOW_RISK_SAFE_FIXES = {
    "image_missing_alt": "הוסף ALT חסר לפי שם המוצר או המאמר",
    "missing_meta_title": "קצר/הצע כותרת מטא בטוחה",
    "internal_link_opportunity": "הצע קישור פנימי רלוונטי",
    "gsc_404": "סמן 404 ישן כהתעלמות",
}


@dataclass(frozen=True)
class IssueDefinition:
    title_template: str
    explanation: str
    why_it_matters: str
    recommended_fix: str
    difficulty: str
    risk_level: str
    estimated_impact: str
    severity: str
    tooltip: str


ISSUE_DEFINITIONS: dict[str, IssueDefinition] = {
    "broken_link": IssueDefinition(
        "קישור שבור בעמוד {page}",
        "העמוד מפנה לכתובת שלא מחזירה תוכן תקין. לקוח שילחץ על הקישור עלול להגיע לשגיאה.",
        "זה פוגע בחוויית המשתמש ועלול לפגוע באמון ובדירוג בגוגל.",
        "לבדוק את הכתובת, להחליף לקישור תקין או להסיר את הקישור הישן לאחר אישור.",
        "בינוני",
        "בינוני",
        "גבוה",
        "גבוהה",
        "קישור שמוביל לעמוד שגיאה או יעד לא תקין.",
    ),
    "gsc_404": IssueDefinition(
        "גוגל מצא 404 בעמוד {page}",
        "Search Console מזהה כתובת שמחזירה שגיאת 404 או שאין לה יעד תקין.",
        "גוגל עלול לבזבז זמן סריקה על עמודים לא קיימים, ומשתמשים עלולים לנחות בשגיאה.",
        "אם הכתובת ישנה ולא חשובה - לסמן כהתעלמות. אם יש חלופה - להכין הפניה ידנית לאישור.",
        "קל",
        "נמוך",
        "בינוני",
        "בינונית",
        "שגיאת 404 שנמצאה לפי נתוני סריקה או Search Console.",
    ),
    "soft_404": IssueDefinition(
        "עמוד נראה כמו Soft 404: {page}",
        "העמוד מחזיר קוד תקין אבל נראה דל, ריק או לא מספיק שימושי לגולש.",
        "גוגל עשוי להתייחס אליו כעמוד לא איכותי, גם אם טכנית הוא לא מחזיר 404.",
        "להוסיף תוכן מועיל, לחבר לעמוד רלוונטי או לבקש אישור להפניה/הסרה.",
        "מתקדם",
        "גבוה",
        "גבוה",
        "גבוהה",
        "עמוד תקין טכנית אך נראה לגוגל כמו עמוד חסר ערך.",
    ),
    "orphan_page": IssueDefinition(
        "עמוד יתום בלי מספיק קישורים: {page}",
        "כמעט ואין קישורים פנימיים שמובילים לעמוד הזה מתוך האתר.",
        "עמודים בלי קישורים פנימיים קשים יותר לגילוי ולדירוג בגוגל.",
        "להוסיף קישור פנימי מעמוד רלוונטי, לאחר בדיקה שהעמוד חשוב לקידום.",
        "בינוני",
        "נמוך",
        "בינוני",
        "בינונית",
        "עמוד שקיים באתר אבל לא מקבל מספיק קישורים פנימיים.",
    ),
    "redirect_chain": IssueDefinition(
        "שרשרת הפניות בעמוד {page}",
        "הכתובת עוברת דרך יותר מדי הפניות לפני שמגיעה ליעד הסופי.",
        "שרשראות הפניה מאטות את האתר ומקשות על גוגל להבין את היעד הנכון.",
        "להחליף קישורים פנימיים כך שיצביעו ישירות לכתובת הסופית.",
        "מתקדם",
        "בינוני",
        "בינוני",
        "בינונית",
        "כמה הפניות ברצף במקום קישור ישיר לכתובת הסופית.",
    ),
    "missing_meta_title": IssueDefinition(
        "חסרה כותרת SEO בעמוד {page}",
        "לעמוד אין כותרת מטא ברורה שמסבירה לגוגל וללקוחות מה יש בו.",
        "כותרת טובה יכולה לשפר הבנה של גוגל ואת אחוז ההקלקה בתוצאות החיפוש.",
        "להוסיף כותרת קצרה וברורה לפי שם העמוד/המוצר, ולשלוח לאישור.",
        "קל",
        "נמוך",
        "גבוה",
        "גבוהה",
        "Meta title הוא הטקסט המרכזי שמופיע לרוב ככותרת בגוגל.",
    ),
    "missing_meta_description": IssueDefinition(
        "חסר תיאור SEO בעמוד {page}",
        "לעמוד אין תיאור קצר שמסביר למה כדאי להיכנס אליו מתוצאות החיפוש.",
        "תיאור טוב יכול לשפר הקלקות ולעזור ללקוח להבין שהעמוד מתאים לו.",
        "לכתוב תיאור קצר, טבעי וברור בעברית ולהעביר לאישור.",
        "קל",
        "נמוך",
        "בינוני",
        "בינונית",
        "Meta description הוא תקציר שעשוי להופיע בתוצאות גוגל.",
    ),
    "duplicate_meta_title": IssueDefinition(
        "כותרת SEO כפולה בעמוד {page}",
        "לעמוד יש כותרת זהה לעמוד אחר באתר.",
        "כותרות כפולות מקשות על גוגל להבין איזה עמוד הכי מתאים לכל חיפוש.",
        "להציע כותרת ייחודית שמדגישה את הערך המיוחד של העמוד.",
        "בינוני",
        "נמוך",
        "בינוני",
        "בינונית",
        "כמה עמודים משתמשים באותה כותרת SEO.",
    ),
    "missing_h1": IssueDefinition(
        "חסרה כותרת ראשית בעמוד {page}",
        "לעמוד אין כותרת H1 ברורה בראש התוכן.",
        "כותרת H1 עוזרת לגולש ולגוגל להבין במה העמוד עוסק.",
        "להוסיף H1 קצר וברור שמתאים לתוכן העמוד, לאחר אישור.",
        "קל",
        "נמוך",
        "בינוני",
        "בינונית",
        "H1 היא הכותרת הראשית שמופיעה בתוך העמוד עצמו.",
    ),
    "duplicate_h1": IssueDefinition(
        "כותרת H1 כפולה בעמוד {page}",
        "הכותרת הראשית של העמוד זהה לכותרת בעמוד אחר.",
        "כותרות כפולות מקשות על בידול העמודים ועל הבנת הנושא המרכזי.",
        "להציע H1 ייחודי ומדויק לעמוד הזה.",
        "בינוני",
        "נמוך",
        "נמוך",
        "נמוכה",
        "כמה עמודים משתמשים באותה כותרת ראשית.",
    ),
    "image_missing_alt": IssueDefinition(
        "חסר טקסט ALT לתמונה בעמוד {page}",
        "יש תמונה בלי טקסט חלופי שמסביר מה רואים בה.",
        "ALT עוזר לנגישות, להבנת תמונות ולתנועת חיפוש תמונות.",
        "להוסיף ALT פשוט לפי שם המוצר או כותרת המאמר.",
        "קל",
        "נמוך",
        "נמוך",
        "נמוכה",
        "טקסט חלופי לתמונה עבור נגישות וגוגל תמונות.",
    ),
    "internal_link_opportunity": IssueDefinition(
        "הזדמנות לקישור פנימי לעמוד {page}",
        "יש עמוד שיכול להתחזק אם נקשר אליו מעמוד רלוונטי באתר.",
        "קישורים פנימיים עוזרים לגולשים ולגוגל להגיע לעמודים חשובים.",
        "להוסיף הצעת קישור פנימי בלבד; לא לפרסם בלי אישור.",
        "קל",
        "נמוך",
        "בינוני",
        "בינונית",
        "מקום בטוח יחסית להמליץ ממנו על קישור לעמוד חשוב.",
    ),
    "product_seo_issue": IssueDefinition(
        "בעיית SEO במוצר {page}",
        "עמוד מוצר חסר מידע SEO חשוב או נשמע גנרי מדי.",
        "עמודי מוצר חלשים עלולים לקבל פחות חשיפה ופחות הקלקות מגוגל.",
        "לשנות שדה ספציפי ב-iStore לפי הערך המוצע בכרטיס: שם, Meta title, Meta description, H1 או תיאור.",
        "בינוני",
        "בינוני",
        "גבוה",
        "גבוהה",
        "בעיה ממוקדת בעמוד מוצר, כמו טקסט גנרי או מטא חסר.",
    ),
}


MANUAL_ISTORE_NOTICE = "יש להעתיק ידנית לאתר ISTORE לאחר בדיקה."


def _page_payload(page: PageAudit | None) -> dict[str, Any]:
    if page is None:
        return {}
    payload = page.to_dict()
    payload["raw_missing_fields"] = page.missing_fields or ""
    return payload


def _is_hebrew_text(value: str | None) -> bool:
    return bool(value and re.search(r"[\u0590-\u05FF]", value))


def _clean_label(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    return cleaned or fallback


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _site_root(page_url: str) -> str:
    parsed = urlparse(page_url)
    if parsed.scheme and parsed.netloc:
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return "/"


def _suggested_slug(page_url: str, base: str) -> str | None:
    current = urlparse(page_url).path.rstrip("/").split("/")[-1]
    if _is_hebrew_text(current):
        return None
    words = re.findall(r"[\u0590-\u05FF\w]+", base.lower())[:6]
    return "-".join(words).strip("-") or None


def _istore_path_for(key: str) -> str:
    paths = {
        "current_product_title": "עריכת מוצר/קטגוריה > כללי > שם",
        "suggested_hebrew_product_title": "עריכת מוצר/קטגוריה > כללי > שם",
        "suggested_meta_title": "עריכת מוצר/קטגוריה > כללי > כותרת לקידום במנוע חיפוש",
        "suggested_meta_description": "עריכת מוצר/קטגוריה > כללי > תיאור לקידום במנוע חיפוש",
        "suggested_h1": "עריכת מוצר/קטגוריה > כללי > שם / כותרת העמוד",
        "suggested_short_product_description": "עריכת מוצר > כללי > תיאור קצר",
        "suggested_long_product_description": "עריכת מוצר/קטגוריה > כללי > תיאור",
        "suggested_slug": "עריכת מוצר/קטגוריה > נתונים > שם ייחודי לקישור",
        "suggested_alt": "עריכת תמונה / העלאת תמונה > ALT / תיאור תמונה",
        "suggested_replacement_url": "ניהול הפניות ידני > יעד 301 מוצע",
    }
    return paths.get(key, "עריכה ידנית ב-iStore לאחר בדיקה")


def _copy_field(
    key: str, label: str, value: str | None, current: str | None = None, issue: str = "נדרש שיפור ידני"
) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "value": str(value or ""),
        "current": str(current or ""),
        "issue_he": issue,
        "istore_path_he": _istore_path_for(key),
        "manual_notice": MANUAL_ISTORE_NOTICE,
    }


def _solution_summary(fields: list[dict[str, str]]) -> str:
    return " | ".join(f"{field['label']}: {field['value']}" for field in fields if field.get("value"))


def _build_ready_solution(
    issue_type: str, page_url: str, page: PageAudit | None, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build copy-paste-only Hebrew remediation content; never writes to the live site."""

    evidence = evidence or {}
    page_data = _page_payload(page)
    page_label = _clean_label(str(page_data.get("title") or page_data.get("h1") or ""), _page_name(page_url))
    specific = _specific_hebrew_copy(page_label)
    meta_title = specific["title"]
    meta_description = specific["description"]
    h1 = specific["name"]
    short_description = specific["short"]
    long_description = (
        f"{specific['name']} — יש לעדכן את תיאור העמוד לפי המוצר המאומת בלבד. "
        "ציינו שימושים, התאמה לגריל/מעשנה/מטבח חוץ, יתרון ללקוח, מידות או חומרי גלם רק אם הם מופיעים במפרט הרשמי. "
        "אין להמציא נתונים טכניים שאינם ידועים."
    )
    faq_block = (
        f"שאלה: למי מתאים {specific['name']}?\n"
        "תשובה: למי שמחפש פתרון מתאים בתחום הגריל, הבישול או מטבח החוץ, לאחר בדיקת התאמה למוצר בפועל.\n"
        "שאלה: מה חשוב לבדוק לפני רכישה?\n"
        "תשובה: מפרט מאומת, מידות, חומרי גלם, הוראות שימוש וזמינות במלאי."
    )
    manual_notice = "ידני בלבד: יש לוודא שהניסוח תואם למוצר בפועל, למלאי ולמדיניות החנות לפני העתקה לאתר."
    fields: list[dict[str, str]] = []
    replacements: list[dict[str, str]] = []
    affected_pages = evidence.get("affected_pages") if isinstance(evidence.get("affected_pages"), list) else []

    if issue_type == "product_seo_issue":
        slug = _suggested_slug(page_url, page_label)
        fields = [
            _copy_field("current_product_title", "כותרת מוצר נוכחית", str(page_data.get("title") or "")),
            _copy_field("suggested_hebrew_product_title", "כותרת מוצר מוצעת בעברית", specific["name"]),
            _copy_field("suggested_meta_title", "Meta title מוצע", meta_title, str(page_data.get("title") or "")),
            _copy_field(
                "suggested_meta_description",
                "Meta description מוצע",
                meta_description,
                str(page_data.get("meta_description") or ""),
            ),
            _copy_field("suggested_h1", "H1 מוצע", h1, str(page_data.get("h1") or "")),
            _copy_field("suggested_short_product_description", "תיאור מוצר קצר מוצע", short_description),
            _copy_field("suggested_long_product_description", "תיאור מוצר ארוך מוצע", long_description),
            _copy_field("suggested_faq", "FAQ מוצע", faq_block),
        ]
        if slug:
            fields.append(_copy_field("suggested_slug", "Slug מוצע", slug))
    elif issue_type == "image_missing_alt":
        image_name = str(evidence.get("image_url") or evidence.get("filename") or _page_name(page_url))
        fields = [
            _copy_field("image_url_or_filename", "כתובת/שם קובץ תמונה", image_name),
            _copy_field("current_alt", "ALT נוכחי", str(evidence.get("current_alt") or "")),
            _copy_field("suggested_alt", "ALT מוצע בעברית", f"{specific['name']} - תמונת מוצר Compass Grill"),
            _copy_field("suggested_image_title", "כותרת תמונה מוצעת", f"{specific['name']} - Compass Grill"),
        ]
    elif issue_type == "missing_meta_title":
        fields = [
            _copy_field("current_meta_title", "Meta title נוכחי", str(page_data.get("title") or "")),
            _copy_field("suggested_meta_title", "Meta title מוצע", meta_title),
        ]
    elif issue_type == "missing_meta_description":
        fields = [
            _copy_field(
                "current_meta_description", "Meta description נוכחי", str(page_data.get("meta_description") or "")
            ),
            _copy_field("suggested_meta_description", "Meta description מוצע", meta_description),
        ]
    elif issue_type == "missing_h1":
        fields = [
            _copy_field("current_h1", "H1 נוכחי", str(page_data.get("h1") or "")),
            _copy_field("suggested_h1", "H1 מוצע", h1),
        ]
    elif issue_type in {"duplicate_meta_title", "duplicate_h1"}:
        duplicated = str(evidence.get("title") or evidence.get("h1") or page_label)
        field_label = "Meta title" if issue_type == "duplicate_meta_title" else "H1"
        fields = [_copy_field("duplicated_value", "ערך כפול", duplicated)]
        pages_for_replacements = affected_pages or [page_url]
        for index, affected_url in enumerate(pages_for_replacements, start=1):
            if issue_type == "duplicate_meta_title":
                unique = _truncate(f"{duplicated} - {index} | ISTORE", 60)
            else:
                unique = f"{duplicated} - עמוד {index}"
            replacements.append(
                {"page_url": str(affected_url), "label": f"{field_label} ייחודי לעמוד {index}", "value": unique}
            )
    elif issue_type in {"broken_link", "gsc_404"}:
        replacement = str(
            evidence.get("suggested_redirect_target")
            or evidence.get("matching_live_url_candidate")
            or evidence.get("suggested_replacement_url")
            or page_data.get("canonical")
            or _site_root(page_url)
        )
        fields = [
            _copy_field("source_page", "עמוד מקור", str(evidence.get("source_page") or "לא זוהה בסריקה - לבדוק ידנית")),
            _copy_field("broken_url", "כתובת שבורה", page_url),
            _copy_field("suggested_replacement_url", "יעד 301 מוצע / כתובת חלופית", replacement),
            _copy_field("redirect_action_type", "סוג פעולה", str(evidence.get("action_type") or "manual review")),
            _copy_field("redirect_confidence", "ציון ביטחון", str(evidence.get("confidence_score") or "לא ידוע")),
            _copy_field("redirect_reason", "סיבה", str(evidence.get("reason") or "בדיקה ידנית בלבד")),
            _copy_field("suggested_anchor_text", "טקסט עוגן מוצע", specific["name"]),
        ]
    elif issue_type == "internal_link_opportunity":
        target = str(evidence.get("target_page") or page_url)
        anchor = page_label
        fields = [
            _copy_field("source_page", "עמוד מקור", page_url),
            _copy_field("target_page", "עמוד יעד", target),
            _copy_field("suggested_anchor_text", "טקסט עוגן מוצע", anchor),
            _copy_field(
                "suggested_link_sentence",
                "משפט מוצע עם הקישור",
                f"למידע נוסף על {anchor}, עברו לעמוד הרלוונטי באתר Compass Grill.",
            ),
        ]
    else:
        fields = [_copy_field("suggested_manual_fix", "פתרון ידני מוצע", ISSUE_DEFINITIONS[issue_type].recommended_fix)]

    return {
        "heading": "פתרון מוכן להעתקה",
        "manual_notice": manual_notice if "manual_notice" in locals() else MANUAL_ISTORE_NOTICE,
        "fields": fields,
        "affected_pages": [str(url) for url in affected_pages],
        "unique_replacements": replacements,
        "links": {"site_url": page_url, "admin_url": evidence.get("admin_url")},
    }


def _json_load(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _page_name(url: str) -> str:
    path = unquote(urlparse(url).path.rstrip("/"))
    slug = path.split("/")[-1] if path else urlparse(url).netloc
    return slug.replace("-", " ") or url


def _normalize_url_key(url: str | None) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.I) else f"https://{raw}")
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "/")).rstrip("/") or "/"
    return f"{host}{path}".lower()


def _specific_hebrew_copy(label: str) -> dict[str, str]:
    text = label.lower()
    if any(token in text for token in ("basalt", "בזלת")):
        name = "אבני בזלת לגריל גז"
        return {
            "name": name,
            "title": "אבני בזלת לגריל גז – פיזור חום והפחתת התלקחויות | Compass Grill",
            "description": "אבני בזלת לגריל גז לשיפור פיזור החום, שמירה על טמפרטורה יציבה והפחתת התלקחויות בזמן צלייה.",
            "short": "אבני בזלת לגריל גז מסייעות לפיזור חום אחיד, להפחתת התלקחויות ולצלייה יציבה יותר.",
        }
    if any(token in text for token in ("vacuum", "ואקום")):
        name = "שקיות ואקום מחורצות 20×30"
        return {
            "name": name,
            "title": "שקיות ואקום מחורצות 20×30 לסו-ויד ואחסון מזון | Compass Grill",
            "description": "שקיות ואקום מחורצות לשימוש עם מכונות ואקום תואמות. מתאימות לסו-ויד, הקפאה ושמירה על טריות המזון.",
            "short": "שקיות ואקום מחורצות לאחסון מזון, הקפאה ובישול סו-ויד במכונה תואמת.",
        }
    if any(token in text for token in ("tandoor", "kazan", "טנדור", "קאזן", "persian", "roma")):
        name = "טנדור או קאזן לבישול חוץ"
        return {
            "name": name,
            "title": "קאזן אסייתי מברזל יצוק לבישול שטח | Compass Grill",
            "description": "קאזן או טנדור לבישול שטח, קדירות, תבשילים וצלייה מעל אש. מתאים לקמפינג ולמטבחי גינה.",
            "short": "קאזן/טנדור לבישול חוץ מתאים לקדירות, תבשילים וצלייה מעל אש פתוחה.",
        }
    if any(token in text for token in ("smoker", "מעשנה")):
        name = "מעשנת פלט מקצועית"
        return {
            "name": name,
            "title": "מעשנת פלט מקצועית לבשר, דגים וירקות | Compass Grill",
            "description": "מעשנת פלט לשליטה בטמפרטורה, עישון ארוך וצלייה איטית. מתאימה לבריסקט, אסאדו, עוף ודגים.",
            "short": "מעשנה לעישון ארוך וצלייה איטית עם שליטה יציבה בחום וטעמי עץ.",
        }
    if any(token in text for token in ("grill", "גריל")):
        name = "גריל גז מקצועי"
        return {
            "name": name,
            "title": "גריל גז מקצועי לגינה ולמטבח חוץ | Compass Grill",
            "description": "גריל גז איכותי לצלייה ביתית ומקצועית, עם פיזור חום יציב וחוויית בישול נוחה בגינה או במרפסת.",
            "short": "גריל גז לגינה או למרפסת מאפשר חימום מהיר, שליטה בחום וצלייה נוחה.",
        }
    if any(token in text for token in ("knife", "סכין")):
        name = "סכין מקצועית לחיתוך בשר"
        return {
            "name": name,
            "title": "סכין מקצועית לחיתוך בשר ועבודת מטבח | Compass Grill",
            "description": "סכין מקצועית לחיתוך, פריסה והכנת בשר במטבח ובאזור הגריל לפני צלייה, עישון ובישול.",
            "short": "סכין מקצועית מסייעת בחיתוך מדויק של בשר וחומרי גלם לפני צלייה או עישון.",
        }
    if text in {"assman", "atman", "skiff"} or not re.search(r"[֐-׿]", label):
        return {
            "name": "זהות מוצר לא ברורה — נדרש בדיקה ידנית",
            "title": "זהות מוצר לא ברורה — נדרש בדיקה ידנית",
            "description": "זהות מוצר לא ברורה — נדרש שיוך ידני לפני כתיבת SEO.",
            "short": "אין להפיק טקסט שיווקי עד שמוודאים מה המוצר.",
        }
    return {
        "name": label,
        "title": _truncate(f"{label} | Compass Grill", 60),
        "description": _truncate(
            f"{label} לשימוש בתחום הגריל, הבישול או מטבח החוץ. יש לבדוק מפרט, התאמה וזמינות לפני רכישה.", 155
        ),
        "short": f"{label} מיועד לשימוש בתחום הגריל, הבישול או מטבח החוץ לאחר אימות מפרט המוצר.",
    }


def _missing_fields(page: PageAudit) -> set[str]:
    return {field.strip() for field in (page.missing_fields or "").split(",") if field.strip()}


def _remediations(page: PageAudit) -> set[str]:
    values = _json_load(page.remediation_suggestions, [])
    return {str(value) for value in values} if isinstance(values, list) else set()


def latest_crawl(db: Session) -> CrawlRun | None:
    return db.query(CrawlRun).order_by(CrawlRun.completed_at.desc().nullslast(), CrawlRun.id.desc()).first()


def discover_issue_candidates(db: Session) -> list[dict[str, Any]]:
    """Discover issue candidates from crawl, Search Console-like metrics and product metadata."""

    crawl = latest_crawl(db)
    pages = [] if crawl is None else db.query(PageAudit).filter(PageAudit.crawl_run_id == crawl.id).all()
    title_counts = Counter((page.title or "").strip().lower() for page in pages if (page.title or "").strip())
    h1_counts = Counter((page.h1 or "").strip().lower() for page in pages if (page.h1 or "").strip())
    candidates: list[dict[str, Any]] = []

    def add(page: PageAudit, issue_type: str, evidence: dict[str, Any] | None = None) -> None:
        candidates.append({"page_url": page.url, "issue_type": issue_type, "page": page, "evidence": evidence or {}})

    for page in pages:
        missing = _missing_fields(page)
        remediations = _remediations(page)
        if page.status_code >= 400:
            add(page, "broken_link", {"status_code": page.status_code})
            if page.status_code == 404:
                add(page, "gsc_404", {"status_code": 404, "source": "crawl"})
        if page.status_code == 200 and (page.word_count or 0) < 50 and (page.seo_score or 0) < 55:
            add(page, "soft_404", {"word_count": page.word_count, "seo_score": page.seo_score})
        if (page.internal_links or 0) <= 0 and page.status_code < 400:
            add(page, "orphan_page", {"internal_links": page.internal_links})
        if "redirect_chain" in missing or "redirect_chain" in remediations:
            add(page, "redirect_chain")
        if "title" in missing or "meta_title" in missing or not (page.title or "").strip():
            add(page, "missing_meta_title")
        if "meta_description" in missing or not (page.meta_description or "").strip():
            add(page, "missing_meta_description")
        if (page.title or "").strip() and title_counts[(page.title or "").strip().lower()] > 1:
            duplicate_pages = [
                item.url for item in pages if (item.title or "").strip().lower() == (page.title or "").strip().lower()
            ]
            add(page, "duplicate_meta_title", {"title": page.title, "affected_pages": duplicate_pages})
        if "h1" in missing or not (page.h1 or "").strip():
            add(page, "missing_h1")
        if (page.h1 or "").strip() and h1_counts[(page.h1 or "").strip().lower()] > 1:
            duplicate_pages = [
                item.url for item in pages if (item.h1 or "").strip().lower() == (page.h1 or "").strip().lower()
            ]
            add(page, "duplicate_h1", {"h1": page.h1, "affected_pages": duplicate_pages})
        if "image_alt" in missing or "image_missing_alt" in missing or "missing_image_alt" in missing:
            add(page, "image_missing_alt")
        if (page.internal_links or 0) < 3 and page.status_code < 400:
            add(page, "internal_link_opportunity", {"internal_links": page.internal_links})
        if (page.page_type == "product" and (page.seo_score or 0) < 70) or "generic_ai_meta" in missing:
            add(page, "product_seo_issue", {"page_type": page.page_type, "seo_score": page.seo_score})

    known_urls = {candidate["page_url"] for candidate in candidates}
    gsc_pages = (
        db.query(GSCKeywordMetric.page_url)
        .filter(GSCKeywordMetric.impressions > 0)
        .group_by(GSCKeywordMetric.page_url)
        .limit(100)
        .all()
    )
    crawled_urls = {page.url for page in pages}
    products_for_matching = db.query(IStoreProduct).limit(1000).all()
    for (page_url,) in gsc_pages:
        if page_url and page_url not in crawled_urls and page_url not in known_urls:
            evidence = {"source": "gsc", **_find_redirect_candidate(page_url, pages, products_for_matching)}
            candidates.append({"page_url": page_url, "issue_type": "gsc_404", "page": None, "evidence": evidence})
            known_urls.add(page_url)

    for product in products_for_matching[:250]:
        product_url = (
            getattr(product, "canonical_url", None)
            or getattr(product, "product_url", None)
            or getattr(product, "slug", None)
        )
        if product_url and product_url not in known_urls:
            meta_title = getattr(product, "meta_title", "") or ""
            meta_description = getattr(product, "meta_description", "") or ""
            if not meta_title.strip() or not meta_description.strip():
                candidates.append(
                    {
                        "page_url": product_url,
                        "issue_type": "product_seo_issue",
                        "page": None,
                        "evidence": {"source": "product_metadata"},
                    }
                )
                known_urls.add(product_url)

    return candidates


def _find_redirect_candidate(page_url: str, pages: list[PageAudit], products: list[IStoreProduct]) -> dict[str, Any]:
    old_tokens = {
        token
        for token in re.split(r"[^\w\u0590-\u05FF]+", _page_name(page_url).lower())
        if len(token) > 2 and not token.isdigit()
    }
    best_url = ""
    best_score = 0
    best_type = "manual review"
    for page in pages:
        if page.status_code >= 400:
            continue
        tokens = {
            token
            for token in re.split(r"[^\w\u0590-\u05FF]+", _page_name(page.url).lower())
            if len(token) > 2 and not token.isdigit()
        }
        score = int(100 * len(old_tokens & tokens) / max(1, len(old_tokens | tokens)))
        if _normalize_url_key(page.canonical) == _normalize_url_key(page_url):
            score = max(score, 95)
        if score > best_score:
            best_url, best_score = page.url, score
            best_type = (
                "redirect to category"
                if page.page_type == "category"
                else "redirect to live product"
                if page.page_type == "product"
                else "redirect to blog article"
                if page.page_type in {"blog", "article"}
                else "manual review"
            )
    for product in products:
        target = product.canonical_url or product.product_url or product.slug or ""
        if not target:
            continue
        compare = f"{product.product_name or ''} {target}".lower()
        score = 80 if any(token and token in compare for token in old_tokens) else 0
        if "basalt" in page_url.lower() and ("בזלת" in compare or "basalt" in compare):
            score = 92
        if score > best_score:
            best_url, best_score, best_type = target, score, "redirect to live product"
    action = best_type if best_score >= 75 else "manual review"
    return {
        "matching_live_url_candidate": best_url,
        "confidence_score": best_score,
        "suggested_redirect_target": best_url if best_score >= 75 else "",
        "action_type": action,
        "reason": "התאמה לפי סלאג/שם מוצר/קנוניקל; המלצה ידנית בלבד, לא נוצרת הפניה אוטומטית.",
    }


def _priority_for(definition: IssueDefinition) -> str:
    if definition.estimated_impact == "גבוה" or definition.risk_level == "גבוה":
        return "high"
    if definition.estimated_impact == "נמוך":
        return "low"
    return "medium"


def _proposed_value(issue_type: str, page_url: str, page: PageAudit | None) -> str:
    page_label = (page.title or page.h1) if page else _page_name(page_url)
    if issue_type == "image_missing_alt":
        return f"ALT מוצע: {page_label}"
    if issue_type == "missing_meta_title":
        return f"כותרת SEO מוצעת: {str(page_label)[:55]}"
    if issue_type == "internal_link_opportunity":
        return f"להוסיף קישור פנימי לעמוד: {page_url}"
    if issue_type == "gsc_404":
        return "סימון כ-404 ישן להתעלמות או בדיקת הפניה ידנית"
    return ISSUE_DEFINITIONS[issue_type].recommended_fix


def ensure_fix_center_tasks(db: Session) -> dict[str, int]:
    """Create missing reviewable SEOFix rows for discovered issues without editing website content."""

    created = 0
    candidates = discover_issue_candidates(db)
    for candidate in candidates:
        page_url = candidate["page_url"]
        issue_type = candidate["issue_type"]
        existing = (
            db.query(SEOFix)
            .filter(SEOFix.page_url == page_url, SEOFix.fix_type == issue_type, SEOFix.source == "fix_center")
            .first()
        )
        if existing:
            continue
        definition = ISSUE_DEFINITIONS[issue_type]
        task = SEOTask(
            source="fix_center",
            page_url=page_url,
            priority=_priority_for(definition),
            status="חדש",
            suggested_title=definition.title_template.format(page=_page_name(page_url)),
            recommendation_json=json.dumps(
                {"issue_type": issue_type, "evidence": candidate.get("evidence", {}), "safety": "approval_required"},
                ensure_ascii=False,
            ),
        )
        db.add(task)
        db.flush()
        page = candidate.get("page")
        evidence = candidate.get("evidence", {})
        ready_solution = _build_ready_solution(issue_type, page_url, page, evidence)
        current_value = None
        if page is not None:
            current_value = json.dumps(page.to_dict(), ensure_ascii=False)
        db.add(
            SEOFix(
                task_id=task.id,
                page_url=page_url,
                fix_type=issue_type,
                current_value=current_value,
                proposed_value=(
                    _solution_summary(ready_solution["fields"]) or _proposed_value(issue_type, page_url, page)
                ),
                status="חדש",
                confidence_score=0.85 if definition.risk_level == "נמוך" else 0.65,
                source="fix_center",
                notes_json=json.dumps(
                    {
                        "no_auto_publish": True,
                        "no_auto_content_edits": True,
                        "requires_approval": True,
                        "requires_double_confirmation": definition.risk_level == "גבוה",
                        "safe_one_click": issue_type in LOW_RISK_SAFE_FIXES and definition.risk_level == "נמוך",
                        "copyable_solution": ready_solution,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        created += 1
    db.commit()
    return {"created_count": created, "total_candidates": len(candidates)}


def _notes(fix: SEOFix) -> dict[str, Any]:
    parsed = _json_load(fix.notes_json, {})
    return parsed if isinstance(parsed, dict) else {}


def task_card(fix: SEOFix) -> dict[str, Any]:
    definition = ISSUE_DEFINITIONS.get(fix.fix_type, ISSUE_DEFINITIONS["product_seo_issue"])
    notes = _notes(fix)
    task = fix.task
    title = definition.title_template.format(page=_page_name(fix.page_url))
    copyable_solution = notes.get("copyable_solution")
    if not isinstance(copyable_solution, dict):
        copyable_solution = _build_ready_solution(fix.fix_type, fix.page_url, None, {})
    return {
        "id": fix.id,
        "task_id": fix.task_id,
        "page_url": fix.page_url,
        "page_label": _page_name(fix.page_url),
        "issue_type": fix.fix_type,
        "issue_type_hebrew": title,
        "title": task.suggested_title if task and task.suggested_title else title,
        "explanation": definition.explanation,
        "why_it_matters": definition.why_it_matters,
        "recommended_fix": definition.recommended_fix,
        "difficulty": definition.difficulty,
        "risk_level": definition.risk_level,
        "estimated_impact": definition.estimated_impact,
        "severity": definition.severity,
        "status": fix.status or "חדש",
        "proposed_value": fix.proposed_value,
        "copyable_solution": copyable_solution,
        "manual_notice": MANUAL_ISTORE_NOTICE,
        "tooltip": definition.tooltip,
        "safe_one_click": bool(notes.get("safe_one_click")),
        "safe_action": LOW_RISK_SAFE_FIXES.get(fix.fix_type),
        "requires_double_confirmation": bool(notes.get("requires_double_confirmation")),
        "priority_score": _priority_score(definition, fix.status),
        "created_at": fix.created_at,
        "updated_at": fix.updated_at,
    }


def _priority_score(definition: IssueDefinition, status: str | None) -> int:
    impact = {"גבוה": 40, "בינוני": 25, "נמוך": 10}.get(definition.estimated_impact, 10)
    severity = {"גבוהה": 35, "בינונית": 20, "נמוכה": 8}.get(definition.severity, 8)
    risk = {"גבוה": 15, "בינוני": 10, "נמוך": 5}.get(definition.risk_level, 5)
    status_bonus = 10 if (status or "חדש") in {"חדש", "בבדיקה"} else 0
    return impact + severity + risk + status_bonus


def list_fix_center_tasks(db: Session, filters: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
    query = db.query(SEOFix).filter(SEOFix.source == "fix_center")
    filters = filters or {}
    if filters.get("issue_type"):
        query = query.filter(SEOFix.fix_type == filters["issue_type"])
    if filters.get("status"):
        query = query.filter(SEOFix.status == filters["status"])
    if filters.get("page"):
        query = query.filter(SEOFix.page_url.contains(filters["page"] or ""))
    fixes = query.order_by(SEOFix.updated_at.desc(), SEOFix.id.desc()).all()
    cards = [task_card(fix) for fix in fixes]
    if filters.get("severity"):
        cards = [card for card in cards if card["severity"] == filters["severity"]]
    if filters.get("difficulty"):
        cards = [card for card in cards if card["difficulty"] == filters["difficulty"]]
    return sorted(cards, key=lambda card: int(card["priority_score"]), reverse=True)


def dashboard_summary(cards: list[dict[str, Any]]) -> dict[str, Any]:
    today = date.today()
    open_cards = [card for card in cards if card["status"] in OPEN_STATUSES]
    return {
        "new_issues": sum(1 for card in cards if card["status"] == "חדש"),
        "fixed_today": sum(
            1
            for card in cards
            if (
                card["status"] == "בוצע"
                and isinstance(card.get("updated_at"), datetime)
                and card["updated_at"].date() == today
            )
        ),
        "waiting_for_approval": sum(1 for card in cards if card["status"] == "ממתין לאישור"),
        "high_priority_open": sum(
            1 for card in open_cards if card["estimated_impact"] == "גבוה" or card["severity"] == "גבוהה"
        ),
        "top_open_tasks": open_cards[:5],
        "safety": [
            "אין פרסום אוטומטי",
            "אין עריכת תוכן אוטומטית",
            "כל שינוי דורש אישור",
            "שינויים בסיכון גבוה דורשים אישור כפול",
        ],
    }


def update_fix_status(db: Session, fix_id: int, status_value: str, double_confirm: bool = False) -> SEOFix:
    fix = db.get(SEOFix, fix_id)
    if fix is None or fix.source != "fix_center":
        raise ValueError("Fix Center task not found")
    if status_value not in STATUS_HEBREW:
        raise ValueError("Unsupported status")
    card = task_card(fix)
    if status_value == "אושר" and card["requires_double_confirmation"] and not double_confirm:
        raise PermissionError("High-risk changes require double confirmation")
    fix.status = status_value
    if fix.task:
        fix.task.status = status_value
    fix.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(fix)
    return fix


def apply_safe_one_click(db: Session, fix_id: int) -> SEOFix:
    fix = db.get(SEOFix, fix_id)
    if fix is None or fix.source != "fix_center":
        raise ValueError("Fix Center task not found")
    card = task_card(fix)
    if not card["safe_one_click"] or card["risk_level"] != "נמוך":
        raise PermissionError("Only low-risk safe suggestions can use one-click preparation")
    notes = _notes(fix)
    notes["safe_prepared_at"] = datetime.now(UTC).isoformat()
    notes["safe_preparation_only"] = True
    notes["auto_published"] = False
    fix.notes_json = json.dumps(notes, ensure_ascii=False)
    fix.status = "ממתין לאישור"
    if fix.task:
        fix.task.status = "ממתין לאישור"
    fix.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(fix)
    return fix
