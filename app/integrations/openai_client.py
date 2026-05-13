import json

from openai import OpenAI

from app.core.config import settings


class OpenAIClient:
    """Small OpenAI wrapper for generating structured SEO recommendations."""

    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured. Add it to your environment or .env file.")
        self.model = settings.openai_model
        self.client = OpenAI(api_key=settings.openai_api_key)

    def generate_seo_recommendation(self, page: dict) -> dict:
        """Generate a structured recommendation JSON object for a page audit."""
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an SEO strategist. Return only a JSON object with exactly these keys: "
                        "suggested_title (string), suggested_h1 (string), meta_description (string), "
                        "primary_keyword (string), secondary_keywords (array of strings), "
                        "content_recommendations (array of strings), technical_recommendations (array of strings), "
                        "internal_link_ideas (array of strings), and priority_reason (string). "
                        "Do not include markdown, explanations, or additional top-level keys."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Create SEO recommendations for this crawled page audit: {json.dumps(page)}",
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        recommendation = json.loads(content)
        if not isinstance(recommendation, dict):
            raise RuntimeError("OpenAI returned an invalid SEO recommendation payload.")
        return recommendation

    def generate_full_article(self, task: dict) -> dict:
        """Generate a complete SEO article package for a saved task and recommendation."""
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert SEO content writer. Return only a JSON object with exactly these keys: "
                        "article_title (string), article_html (string), faq (array of objects), "
                        "faq_schema_json (object), article_schema_json (object), meta_title (string), "
                        "meta_description (string), and slug_suggestion (string). "
                        "The article_html value must be publish-ready semantic HTML and must not include markdown. "
                        "Do not include explanations or additional top-level keys."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Generate a full SEO article from this saved SEO task: {json.dumps(task)}",
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        article = json.loads(content)
        if not isinstance(article, dict):
            raise RuntimeError("OpenAI returned an invalid SEO article payload.")
        return article

