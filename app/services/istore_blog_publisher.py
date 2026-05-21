from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from app.db.models import ContentArticleDraft
from app.integrations.istore import IStoreAPIError, IStoreClient

logger = logging.getLogger(__name__)


class IStoreBlogPublishError(RuntimeError):
    """Raised when ISTORE blog publishing fails or cannot be verified live."""


@dataclass(frozen=True)
class PublishVerificationResult:
    url: str
    status_code: int
    title_found: bool
    meta_title_found: bool


class IStoreBlogPublisher:
    def __init__(self, client: IStoreClient, timeout_seconds: float = 20.0) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls) -> IStoreBlogPublisher:
        client = IStoreClient.from_settings()
        return cls(client=client, timeout_seconds=max(10.0, client.timeout_seconds))

    def publish(self, draft: ContentArticleDraft) -> dict[str, Any]:
        payload = self._build_payload(draft)
        attempts = self._publish_attempts(payload)
        logger.info("[ISTORE BLOG PUBLISH] start slug=%s url=%s", draft.slug, draft.target_url)
        logger.info("[ISTORE BLOG PUBLISH] request payload keys=%s", sorted(payload.keys()))

        last_error: str | None = None
        for path in attempts:
            try:
                response = self.client._put(path, payload)
                logger.info("[ISTORE BLOG PUBLISH] endpoint=%s response=%s", path, response)
                verified = self._verify_live_url(draft.target_url, draft.title, draft.meta_title)
                logger.info("[ISTORE BLOG PUBLISH] verification endpoint=%s result=%s", path, verified)
                return {"endpoint": path, "publish_response": response, "verification": verified.__dict__}
            except (IStoreAPIError, IStoreBlogPublishError, requests.RequestException) as exc:
                last_error = str(exc)
                logger.warning("[ISTORE BLOG PUBLISH] endpoint=%s failed=%s", path, exc)

        raise IStoreBlogPublishError(last_error or "ISTORE blog publish failed on all endpoints")

    def _build_payload(self, draft: ContentArticleDraft) -> dict[str, Any]:
        return {
            "title": draft.title,
            "slug": draft.slug,
            "body_html": draft.article_body,
            "meta_title": draft.meta_title,
            "meta_description": draft.meta_description,
            "canonical": draft.target_url,
            "featured_image": {
                "url": draft.featured_image_url,
                "alt": draft.image_alt_text,
                "title": draft.image_title,
                "caption": draft.image_caption,
                "filename_slug": draft.image_filename_slug,
            },
            "internal_links": draft.to_dict().get("internal_links", []),
            "site_section": draft.target_site_section,
            "publish_type": draft.target_publish_type,
            "target_path": draft.target_path,
        }

    def _publish_attempts(self, payload: dict[str, Any]) -> list[str]:
        slug = str(payload.get("slug") or "")
        return [
            "blog/articles",
            "content/articles",
            "cms/pages",
            "content/pages",
            f"blog/{slug}",
        ]

    def _verify_live_url(self, url: str, expected_title: str, expected_meta_title: str) -> PublishVerificationResult:
        response = requests.get(url, timeout=self.timeout_seconds)
        html = response.text if response.ok else ""
        title_found = expected_title in html
        meta_title_found = expected_meta_title in html
        result = PublishVerificationResult(
            url=url,
            status_code=response.status_code,
            title_found=title_found,
            meta_title_found=meta_title_found,
        )
        if response.status_code != 200:
            raise IStoreBlogPublishError(f"Live URL verification failed with HTTP {response.status_code}")
        if not title_found:
            raise IStoreBlogPublishError("Live URL verification failed: title not found in HTML")
        if not meta_title_found:
            raise IStoreBlogPublishError("Live URL verification failed: meta title not found in HTML")
        return result
