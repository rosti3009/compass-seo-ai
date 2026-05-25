from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from json import dumps as json_dumps
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


CREATE_REDIRECT_PATH = "/client/shop_information/create"
VALID_SUBMIT_MODES = {"json", "form", "multipart"}


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
        submit_mode = self._resolve_submit_mode()
        minimal_payload = bool(settings.istore_create_minimal_payload)
        if dry_run:
            contract = self._sanitized_request_contract(payload)
            return {
                "dry_run": True,
                "endpoint": CREATE_REDIRECT_PATH,
                "submit_mode": submit_mode,
                "minimal_payload": minimal_payload,
                "payload": payload,
                "headers": self._build_create_headers(sanitize=True),
                "request_contract": contract,
            }

        create_result = self._create_shop_information(payload)
        if minimal_payload:
            return {
                "endpoint": CREATE_REDIRECT_PATH,
                "external_content_id": create_result.external_content_id,
                "redirect_location": create_result.location,
                "minimal_payload_test": True,
                "result_he": "ISTORE minimal create test succeeded; full article payload still needs investigation.",
            }
        live_url = self._resolve_live_url(draft, create_result.external_content_id)
        verification = self._verify_live_url(live_url, draft.title)

        return {
            "endpoint": CREATE_REDIRECT_PATH,
            "external_content_id": create_result.external_content_id,
            "redirect_location": create_result.location,
            "live_url": live_url,
            "verification": verification.__dict__,
        }

    def _build_payload(self, draft: ContentArticleDraft) -> dict[str, Any]:
        language_id = str(self.language_id)
        if settings.istore_create_minimal_payload:
            return {
                "descriptions": {
                    language_id: {
                        "title": "בדיקת יצירת עמוד",
                        "description": "<p>בדיקה</p>",
                        "meta_title": "בדיקת יצירת עמוד",
                        "meta_description": "בדיקה",
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
        self._validate_create_payload(payload)
        submit_mode = self._resolve_submit_mode()
        url = urljoin(self.base_url, CREATE_REDIRECT_PATH.lstrip("/"))
        headers = self._build_create_headers(sanitize=False)
        request_kwargs = self._build_submit_payload(payload, submit_mode)
        sanitized_contract = self._sanitized_request_contract(payload)
        logger.info(
            "[ISTORE BLOG PUBLISH] sanitized request contract: endpoint=%s submit_mode=%s payload_keys=%s language_id=%s is_blog=%s has_cookie=%s cookie_names=%s has_xsrf=%s has_inertia_version=%s",
            sanitized_contract["endpoint"],
            submit_mode,
            sorted((sanitized_contract.get("payload") or {}).keys()),
            self.language_id,
            (sanitized_contract.get("payload") or {}).get("is_blog"),
            bool(sanitized_contract.get("cookie_names")),
            sanitized_contract.get("cookie_names"),
            sanitized_contract.get("xsrf_token_present"),
            sanitized_contract.get("inertia_version_present"),
        )
        logger.info(
            "ISTORE submit request: mode=%s outgoing_content_type=%s",
            submit_mode,
            headers.get("Content-Type", "[auto]"),
        )
        response = self.session.post(url, headers=headers, timeout=self.timeout_seconds, allow_redirects=False, **request_kwargs)
        self._log_create_response(response, submit_mode)
        if response.status_code != 302:
            raise IStoreBlogPublishError(
                f"ISTORE admin create failed: expected 302 redirect, got HTTP {response.status_code}"
            )

        location = response.headers.get("Location", "")
        if self._is_create_form_redirect(location):
            raise IStoreBlogPublishError(
                "ISTORE rejected create request. Compare manual request contract with /debug/istore/create-dry-run."
            )

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

    def _build_create_headers(self, sanitize: bool) -> dict[str, str]:
        submit_mode = self._resolve_submit_mode()
        cookie_header = (settings.istore_raw_cookie_header or self.admin_cookie or "").strip()
        xsrf_token = (settings.istore_xsrf_token_override or self.xsrf_token or "").strip()
        headers = {
            "Accept": "text/html, application/xhtml+xml",
            "X-Inertia": "true",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": xsrf_token,
            "Referer": urljoin(self.base_url, CREATE_REDIRECT_PATH.lstrip("/")),
            "Origin": self.base_url.rstrip("/"),
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Cookie": cookie_header,
        }
        if submit_mode == "json":
            headers["Content-Type"] = "application/json"
        elif submit_mode == "form":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if settings.istore_inertia_version:
            headers["X-Inertia-Version"] = settings.istore_inertia_version
        if sanitize:
            return {
                key: ("[REDACTED]" if key in {"Cookie", "X-XSRF-TOKEN"} else value)
                for key, value in headers.items()
            }
        return headers

    def _resolve_submit_mode(self) -> str:
        configured = str(settings.istore_create_submit_mode or "form").strip().lower()
        if configured in VALID_SUBMIT_MODES:
            return configured
        logger.warning("Invalid ISTORE_CREATE_SUBMIT_MODE '%s'; falling back to form", configured)
        return "form"

    def _build_submit_payload(self, payload: dict[str, Any], submit_mode: str) -> dict[str, Any]:
        if submit_mode == "json":
            return {"json": payload}

        flattened = self._flatten_payload(payload)
        if submit_mode == "form":
            return {"data": flattened}
        if submit_mode == "multipart":
            return {"files": [(k, (None, v)) for k, v in flattened]}
        return {"json": payload}

    def _flatten_payload(self, payload: dict[str, Any]) -> list[tuple[str, str]]:
        output: list[tuple[str, str]] = []

        def _walk(prefix: str, value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    key_prefix = f"{prefix}[{key}]" if prefix else str(key)
                    _walk(key_prefix, inner)
                return
            if isinstance(value, list):
                for idx, inner in enumerate(value):
                    key_prefix = f"{prefix}[{idx}]"
                    _walk(key_prefix, inner)
                if not value:
                    output.append((prefix, "[]"))
                return
            if value is None:
                output.append((prefix, ""))
            elif isinstance(value, bool):
                output.append((prefix, "1" if value else "0"))
            elif isinstance(value, (int, float, str)):
                output.append((prefix, str(value)))
            else:
                output.append((prefix, json_dumps(value, ensure_ascii=False)))

        _walk("", payload)
        return output

    def _sanitized_request_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._build_create_headers(sanitize=False)
        cookie_names = self._cookie_names(headers.get("Cookie", ""))
        duplicate_cookie_names = sorted({name for name in cookie_names if cookie_names.count(name) > 1})
        cookie_source = "raw_header" if (settings.istore_raw_cookie_header or "").strip() else "parsed"
        xsrf_source = "override" if (settings.istore_xsrf_token_override or "").strip() else "parsed"
        language_id = str(self.language_id)
        desc = (payload.get("descriptions") or {}).get(language_id, {}) if isinstance(payload.get("descriptions"), dict) else {}
        title = str(desc.get("title") or "")
        description = str(desc.get("description") or "")
        estimated_json_length = len(json_dumps(payload, ensure_ascii=False))
        return {
            "endpoint": CREATE_REDIRECT_PATH,
            "method": "POST",
            "headers": self._sanitize_headers_for_debug(headers),
            "payload": payload,
            "cookie_names": cookie_names,
            "cookie_source": cookie_source,
            "duplicate_cookie_names": duplicate_cookie_names,
            "cookie_count": len(cookie_names),
            "xsrf_token_present": bool(headers.get("X-XSRF-TOKEN")),
            "xsrf_length": len(headers.get("X-XSRF-TOKEN", "")),
            "xsrf_source": xsrf_source,
            "inertia_version_present": bool(headers.get("X-Inertia-Version")),
            "minimal_payload": bool(settings.istore_create_minimal_payload),
            "payload_description_length": len(description),
            "payload_title_length": len(title),
            "estimated_json_length": estimated_json_length,
        }

    def _sanitize_headers_for_debug(self, headers: dict[str, str]) -> dict[str, str]:
        sanitized: dict[str, str] = {}
        for key, value in headers.items():
            if key in {"Cookie", "X-XSRF-TOKEN"}:
                continue
            sanitized[key] = value
        return sanitized

    def _cookie_names(self, cookie_header: str) -> list[str]:
        if not cookie_header:
            return []
        names: list[str] = []
        for part in cookie_header.split(";"):
            name = part.strip().split("=", 1)[0].strip()
            if name:
                names.append(name)
        return names

    def _validate_create_payload(self, payload: dict[str, Any]) -> None:
        language_id = str(self.language_id)
        desc = (payload.get("descriptions") or {}).get(language_id, {}) if isinstance(payload.get("descriptions"), dict) else {}
        required_desc = ["title", "description", "meta_title", "meta_description"]
        missing = [f"descriptions[{language_id}].{field}" for field in required_desc if not str(desc.get(field) or "").strip()]

        top_level_fields = ["dynamic_fields", "is_blog", "keyword", "sort_order", "start_date", "end_date", "status"]
        missing.extend([field for field in top_level_fields if field not in payload])
        if payload.get("dynamic_fields") != []:
            missing.append("dynamic_fields must equal []")

        if missing:
            raise IStoreBlogPublishError(f"Invalid ISTORE create payload; missing/invalid fields: {', '.join(missing)}")

    def _is_create_form_redirect(self, location: str) -> bool:
        if not location:
            return False
        parsed = urlparse(location)
        path = parsed.path or location
        return path.rstrip("/") == CREATE_REDIRECT_PATH

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

    def _log_create_response(self, response: requests.Response, submit_mode: str) -> None:
        safe_text = self._sanitize((getattr(response, "text", "") or "")[:500])
        diagnostic_headers = {
            "location": self._sanitize(response.headers.get("Location", "")),
            "content-type": self._sanitize(response.headers.get("Content-Type", "")),
            "x-inertia": self._sanitize(response.headers.get("X-Inertia", "")),
            "set-cookie": self._sanitize(response.headers.get("Set-Cookie", "")),
        }
        logger.info(
            "ISTORE create response: submit_mode=%s status_code=%s location=%s response_url=%s response_headers=%s response_text=%s",
            submit_mode,
            response.status_code,
            diagnostic_headers["location"],
            self._sanitize(getattr(response, "url", "")),
            diagnostic_headers,
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
