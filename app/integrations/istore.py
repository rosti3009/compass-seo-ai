from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin

import requests

from app.core.config import settings

REDACTED_TOKEN = "[redacted]"  # noqa: S105


class MissingIStoreSettingsError(RuntimeError):
    """Raised when the ISTORE integration is not fully configured."""


class IStoreAPIError(RuntimeError):
    """Raised when the ISTORE read-only API cannot return usable data."""


def _redact_token(value: object, token: str | None = None) -> str:
    """Return a string representation with any configured ISTORE token redacted."""
    text = str(value)
    if token:
        text = text.replace(token, REDACTED_TOKEN)
    configured_token = settings.istore_x_token
    if configured_token:
        text = text.replace(configured_token, REDACTED_TOKEN)
    return text


def _normalized_base_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/"


@dataclass(frozen=True)
class IStoreClient:
    """Small read-only ISTORE API client.

    Phase 1 intentionally exposes only GET operations and never sends PUT/PATCH/POST
    product mutation requests.
    """

    base_url: str
    company_id: str
    x_token: str = field(repr=False)
    timeout_seconds: float
    session: requests.Session = field(default_factory=requests.Session, repr=False, compare=False)

    @classmethod
    def from_settings(cls) -> IStoreClient:
        missing = [
            name
            for name, value in (
                ("ISTORE_BASE_URL", settings.istore_base_url),
                ("ISTORE_COMPANY_ID", settings.istore_company_id),
                ("ISTORE_X_TOKEN", settings.istore_x_token),
            )
            if not value
        ]
        if missing:
            raise MissingIStoreSettingsError(f"ISTORE integration is not configured. Set {', '.join(missing)}.")
        return cls(
            base_url=_normalized_base_url(settings.istore_base_url or ""),
            company_id=settings.istore_company_id or "",
            x_token=settings.istore_x_token or "",
            timeout_seconds=settings.istore_timeout_seconds,
        )

    def status(self) -> dict[str, object]:
        return {
            "configured": True,
            "base_url": self.base_url.rstrip("/"),
            "company_id": self.company_id,
            "x_token": REDACTED_TOKEN,
            "timeout_seconds": self.timeout_seconds,
            "mode": "safe_write_gated" if settings.istore_publish_enabled else "read_only",
            "allowed_methods": ["GET", "PUT"] if settings.istore_publish_enabled else ["GET"],
        }

    def list_products(self) -> Any:
        """Fetch products from ISTORE without mutating remote state."""
        return self._get("products")

    def get_product(self, product_id: str) -> Any:
        """Fetch a single product from ISTORE without mutating remote state."""
        return self._get(f"products/{quote(product_id, safe='')}")

    def update_product(self, product_id: str, payload: dict[str, Any]) -> Any:
        """PUT a tightly-scoped, pre-approved SEO payload to one ISTORE product."""
        return self._put(f"products/{quote(product_id, safe='')}", payload)

    def _get(self, path: str) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "X-Token": self.x_token,
        }
        params = {"company_id": self.company_id}
        try:
            response = self.session.get(url, headers=headers, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IStoreAPIError(f"ISTORE API request failed: {_redact_token(exc, self.x_token)}") from exc

        try:
            return response.json()
        except ValueError as exc:
            raise IStoreAPIError("ISTORE API returned a non-JSON response.") from exc

 def _put(self, path: str, payload: dict[str, Any]) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Token": self.x_token,
        }
        params = {"company_id": self.company_id}
        try:
            response = self.session.put(
                url, headers=headers, params=params, json=payload, timeout=self.timeout_seconds
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IStoreAPIError(f"ISTORE API request failed: {_redact_token(exc, self.x_token)}") from exc

        try:
            return response.json()
        except ValueError:
            return {"status_code": response.status_code, "text": _redact_token(response.text, self.x_token)}
