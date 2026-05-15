import json

from openai import OpenAI

from app.core.config import settings


class OpenAIClient:
    def __init__(self) -> None:
        self.client = OpenAI(
            api_key=settings.openai_api_key,
        )
        self.model = settings.openai_model

    def generate_seo_rewrite(
        self,
        query: str,
        page_url: str,
    ) -> dict:
        prompt = f"""
Generate Hebrew SEO content for this keyword and page.

Keyword:
{query}

Page:
{page_url}

Return valid JSON only:
{{
  "seo_title": "",
  "meta_description": "",
  "marketing_hook": "",
  "telegram_teaser": ""
}}
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert Hebrew SEO copywriter.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"

        return {
            "query": query,
            "page_url": page_url,
            "ai_response": json.loads(content),
        }

    def generate_seo_plan(
        self,
        recommendations: list[dict],
    ) -> dict:
        prompt = f"""
אתה מומחה SEO בכיר לאתרי איקומרס בעברית.

צור תוכנית SEO מעשית לאתר Compass Grill לפי נתוני Google Search Console הבאים:
{recommendations}

ענה בעברית בלבד.
החזר JSON תקין בלבד, בלי Markdown ובלי הסברים מסביב.

מבנה JSON חובה:
{{
  "pages": [
    {{
      "page_url": "",
      "main_keyword": "",
      "main_problem": "",
      "recommended_title": "",
      "recommended_meta_description": "",
      "recommended_h1": "",
      "content_actions": ["", "", ""],
      "internal_link_ideas": ["", "", ""],
      "priority": "גבוהה"
    }}
  ]
}}
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "אתה מומחה SEO בכיר ומנהל תוכן לאתרי מסחר בעברית.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"

        return {
            "connected": True,
            "seo_plan": json.loads(content),
        }

    def generate_full_article(
        self,
        keyword: str,
        page_url: str,
    ) -> dict:
        prompt = f"""
אתה מומחה SEO ותוכן בעברית לאתרי מסחר.

כתוב מאמר SEO מלא ואיכותי עבור מילת המפתח:
{keyword}

עמוד יעד:
{page_url}

החזר JSON בלבד במבנה הבא:

{{
  "seo_title": "",
  "meta_description": "",
  "h1": "",
  "html_article": "",
  "faq": [
    {{
      "question": "",
      "answer": ""
    }}
  ],
  "faq_schema": "",
  "article_schema": "",
  "image_prompts": [
    {{
      "section": "",
      "prompt": "",
      "alt_text": ""
    }}
  ],
  "internal_links": [
    ""
  ],
  "cta": ""
}}

דרישות חשובות:

- html_article חייב להיות HTML אמיתי
- להשתמש ב:
<h2>
<h3>
<p>
<ul>
<li>

- לא להשתמש ב-Markdown
- המאמר צריך להיות מוכן להדבקה ישירות ל-ISTORE
- FAQ schema חייב להיות JSON-LD תקין
- article schema חייב להיות JSON-LD תקין
- image_prompts צריכים להיות מותאמים ליצירת תמונות ריאליסטיות
- alt_text חייב להיות מותאם SEO
- התוכן חייב להיות מקצועי, אמין ושיווקי
- מותאם ל-Google Helpful Content
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "אתה מומחה SEO ותוכן לאתרי מסחר בעברית.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"

        return json.loads(content)

    def generate_seo_patch_plan(
        self,
        seo_data: dict,
    ) -> dict:
        prompt = f"""
אתה מומחה SEO בכיר.

נתוני עמוד:
{json.dumps(seo_data, ensure_ascii=False, indent=2)}

נתח את מצב ה-SEO של העמוד.

החזר JSON בלבד במבנה:

{{
  "problems": [],
  "quick_wins": [],
  "content_gaps": [],
  "recommended_changes": [],
  "internal_link_opportunities": [],
  "ctr_improvements": [],
  "schema_opportunities": [],
  "image_improvements": []
}}

ענה בעברית בלבד.
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "אתה מומחה SEO טכני ואסטרטג תוכן.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"

        return json.loads(content)

    def generate_internal_link_opportunities(
        self,
        pages: list[dict],
    ) -> dict:
        prompt = f"""
אתה מומחה SEO ו-Internal Linking.

קיבלת רשימת עמודים מאתר.

המטרה:
לזהות אילו עמודים חזקים יכולים לחזק עמודים חלשים.

החזר JSON בלבד במבנה:

{{
  "links": [
    {{
      "source_page": "",
      "target_page": "",
      "anchor_text": "",
      "reason": ""
    }}
  ]
}}

נתוני העמודים:
{json.dumps(pages, ensure_ascii=False, indent=2)}

חוקים:
- source_page חייב להיות עמוד חזק יותר
- target_page חייב להיות עמוד עם פוטנציאל SEO
- anchor_text חייב להיות טבעי
- reason חייב להסביר למה הקישור חשוב
- תעדיף topical relevance
- תעדיף עמודים עם CTR נמוך או position 4-15
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "אתה מומחה SEO ו-Topical Authority.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"

        return json.loads(content)