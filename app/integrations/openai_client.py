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
                        "You are an SEO strategist. Return only JSON with these keys: "
                        "suggested_title, suggested_h1, meta_description, priority, keyword, recommendations."
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
