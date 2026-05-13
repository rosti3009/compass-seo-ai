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

    def generate_internal_link_suggestions(self, pages: list[dict]) -> dict:
        """Improve internal link anchor text and topical relevance for candidate page pairs."""
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an SEO internal linking strategist. Return only a JSON object with exactly one "
                        "top-level key named opportunities. opportunities must be an array of objects with these "
                        "keys: source_url (string), target_url (string), anchor_text (string), reason (string). "
                        "Use concise, natural anchor text that matches topical relevance. Do not add markdown or "
                        "extra top-level keys."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Refine anchor text and reasons for these internal link candidates: " f"{json.dumps(pages)}"
                    ),
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        suggestions = json.loads(content)
        if not isinstance(suggestions, dict):
            raise RuntimeError("OpenAI returned an invalid internal link suggestions payload.")
        opportunities = suggestions.get("opportunities", [])
        if not isinstance(opportunities, list):
            raise RuntimeError("OpenAI returned internal link suggestions without an opportunities array.")
        return suggestions

    def generate_topical_clusters(self, pages: list[dict]) -> dict:
        """Generate topical cluster strategy from crawled page and SEO task context."""
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an SEO topical authority strategist. Return only a JSON object with exactly one "
                        "top-level key named clusters. clusters must be an array of objects with these keys: "
                        "cluster_name (string), pillar_page (string URL), supporting_pages (array of string URLs), "
                        "missing_articles (array of strings), internal_link_strategy (array of strings). "
                        "Do not include markdown or extra top-level keys."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Build topical SEO clusters from these crawled pages and task statuses: " f"{json.dumps(pages)}"
                    ),
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        clusters = json.loads(content)
        if not isinstance(clusters, dict):
            raise RuntimeError("OpenAI returned an invalid topical clusters payload.")
        if not isinstance(clusters.get("clusters"), list):
            raise RuntimeError("OpenAI returned topical clusters without a clusters array.")
        return clusters

    def generate_seo_strategy_enrichment(self, recommendations: list[dict]) -> dict:
        """Generate AI summaries, actions, and reasoning for SEO strategy recommendations."""
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a business-focused SEO strategy engine. Return only a JSON object with exactly one "
                        "top-level key named recommendations. recommendations must be an array of objects with these "
                        "keys: page_url (string), recommendation_type (string), ai_summary (string), "
                        "recommended_action (string), and reasoning (string). Keep all text concise and operational. "
                        "Do not include markdown or extra top-level keys."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Enrich these scored SEO strategy recommendations with summaries, actions, and reasoning: "
                        f"{json.dumps(recommendations)}"
                    ),
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        enrichment = json.loads(content)
        if not isinstance(enrichment, dict):
            raise RuntimeError("OpenAI returned an invalid SEO strategy enrichment payload.")
        if not isinstance(enrichment.get("recommendations"), list):
            raise RuntimeError("OpenAI returned SEO strategy enrichment without a recommendations array.")
        return enrichment
