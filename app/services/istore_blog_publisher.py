from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from app.core.config import settings
from app.db.models import ContentArticleDraft

logger = logging.getLogger(__name__)


class IStoreBlogPublishError(RuntimeError):
    """Raised when ISTORE blog publishing fails or cannot be verified live."""


class MissingIStoreAdminSettingsError(IStoreBlogPublishError):
    """Raised when ISTORE admin publishing settings are missing."""


@dataclass(frozen=True)
class PublishVerificationResult:
    url: str
    status_code: int
    title_found: bool


@dataclass(frozen=True)
class IStoreCreateResult:
    external_content_id: str
    location: str
    status_code: int
    response_url: str


class IStoreAdminShopInformationPublisher:
    def __init__(
        self,
        base_url: str,
        admin_cookie: str,
        xsrf_token: str,
        language_id: int,
        blog_is_blog: int,
        timeout_seconds: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.admin_cookie = admin_cookie
        self.xsrf_token = xsrf_token
        self.language_id = language_id
        self.blog_is_blog = blog_is_blog
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    @classmethod
    def from_settings(cls) -> IStoreAdminShopInformationPublisher:
        missing = [
            name
            for name, value in (
                ("ISTORE_ADMIN_BASE_URL", settings.istore_admin_base_url),
                ("ISTORE_ADMIN_COOKIE", settings.istore_admin_cookie),
                ("ISTORE_XSRF_TOKEN", settings.istore_xsrf_token),
            )
            if not value
        ]
        if missing:
            raise MissingIStoreAdminSettingsError(f"ISTORE admin publishing is not configured. Set {', '.join(missing)}.")

        return cls(
            base_url=settings.istore_admin_base_url or "",
            admin_cookie=settings.istore_admin_cookie or "",
            xsrf_token=settings.istore_xsrf_token or "",
            language_id=settings.istore_language_id,
            blog_is_blog=settings.istore_blog_is_blog,
            timeout_seconds=max(10.0, settings.istore_timeout_seconds),
        )

    def publish(self, draft: ContentArticleDraft, dry_run: bool = False) -> dict[str, Any]:
        payload = self._build_payload(draft)
        if dry_run:
            return {"dry_run": True, "endpoint": "client/shop_information/create", "payload": payload}

        create_result = self._create_shop_information(payload)
        live_url = self._resolve_live_url(draft, create_result.external_content_id)
        verification = self._verify_live_url(live_url, draft.title)

        return {
            "endpoint": "client/shop_information/create",
            "external_content_id": create_result.external_content_id,
            "redirect_location": create_result.location,
            "live_url": live_url,
            "verification": verification.__dict__,
        }

    def _build_payload(self, draft: ContentArticleDraft) -> dict[str, Any]:
        language_id = str(self.language_id)
        return {
            "descriptions": {
                language_id: {
                    "title": draft.title,
                    "description": draft.article_body,
                    "meta_title": draft.meta_title,
                    "meta_description": draft.meta_description,
                }
            },
            "dynamic_fields": [],
            "end_date": None,
            "is_blog": self.blog_is_blog,
            "keyword": "",
            "sort_order": 0,
            "start_date": None,
            "status": 1,
        }

    def _create_shop_information(self, payload: dict[str, Any]) -> IStoreCreateResult:
        url = urljoin(self.base_url, "client/shop_information/create")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": self.admin_cookie,
            "X-XSRF-TOKEN": self.xsrf_token,
            "X-Inertia": "true",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self.session.post(url, headers=headers, json=payload, timeout=self.timeout_seconds, allow_redirects=False)
        self._log_create_response(response)
        if response.status_code != 302:
            raise IStoreBlogPublishError(
                f"ISTORE admin create failed: expected 302 redirect, got HTTP {response.status_code}"
            )

        location = response.headers.get("Location", "")
        external_content_id = self._extract_shop_information_id(location, response)
        if not external_content_id:
            status_code = response.status_code
            raise IStoreBlogPublishError(
                "ISTORE admin create succeeded but no shop_information id found in redirect "
                f"(status_code={status_code}, location={self._sanitize(location)}, response_url={self._sanitize(getattr(response, "url", ""))})"
            )
        return IStoreCreateResult(
            external_content_id=external_content_id,
            location=location,
            status_code=response.status_code,
            response_url=getattr(response, "url", ""),
        )

    def _extract_shop_information_id(self, location: str, response: requests.Response | None = None) -> str | None:
        candidates = [location or ""]
        if response is not None:
            candidates.append(getattr(response, "url", "") or "")

        patterns = [
            r"/client/shop_information/edit/(\d+)",
            r"/client/shop_information/(\d+)/edit",
        ]
        for candidate in candidates:
            for pattern in patterns:
                match = re.search(pattern, candidate)
                if match:
                    return match.group(1)

            parsed = urlparse(candidate)
            query = parse_qs(parsed.query)
            query_id = (query.get("id") or [None])[0]
            if query_id and str(query_id).isdigit():
                return str(query_id)

        if response is not None:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            extracted = self._extract_id_from_payload(payload)
            if extracted:
                return extracted
        return None

    def _extract_id_from_payload(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key == "id" and str(value).isdigit():
                    return str(value)
                extracted = self._extract_id_from_payload(value)
                if extracted:
                    return extracted
        elif isinstance(payload, list):
            for item in payload:
                extracted = self._extract_id_from_payload(item)
                if extracted:
                    return extracted
        return None

    def _log_create_response(self, response: requests.Response) -> None:
        safe_text = self._sanitize((getattr(response, "text", "") or "")[:500])
        logger.info(
            "ISTORE create response: status_code=%s location=%s response_url=%s response_text=%s",
            response.status_code,
            self._sanitize(response.headers.get("Location", "")),
            self._sanitize(getattr(response, "url", "")),
            safe_text,
        )

    def _sanitize(self, value: str) -> str:
        if not value:
            return ""
        sanitized = value
        sanitized = re.sub(r"(?i)(token|xsrf|session|cookie)=([^&\s]+)", r"\1=[REDACTED]", sanitized)
        return sanitized

    def _resolve_live_url(self, draft: ContentArticleDraft, external_content_id: str) -> str:
        candidates = [draft.target_url]
        if getattr(draft, "slug", None):
            candidates.append(urljoin(self.base_url, f"blog/{draft.slug}"))
        candidates.append(urljoin(self.base_url, f"shop_information/{external_content_id}"))

        for candidate in candidates:
            try:
                response = requests.get(candidate, timeout=self.timeout_seconds)
            except requests.RequestException:
                continue
            if response.status_code == 200 and draft.title in response.text:
                return candidate

        raise IStoreBlogPublishError("נוצר עמוד מידע ב-ISTORE אבל לא נמצא URL ציבורי מאומת")

    def _verify_live_url(self, url: str, expected_title: str) -> PublishVerificationResult:
        response = requests.get(url, timeout=self.timeout_seconds)
        title_found = expected_title in (response.text if response.ok else "")
        if response.status_code != 200 or not title_found:
            raise IStoreBlogPublishError("נוצר עמוד מידע ב-ISTORE אבל לא נמצא URL ציבורי מאומת")
        return PublishVerificationResult(url=url, status_code=response.status_code, title_found=title_found)


class IStoreBlogPublisher(IStoreAdminShopInformationPublisher):
    """Backward-compatible alias for admin shop information publisher."""
