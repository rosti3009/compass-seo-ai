from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import import_module, util
from typing import Any

from app.core.config import settings
from app.integrations.google_auth import MissingGoogleCredentialsError, resolve_google_credentials

GSC_SCOPES = ("https://www.googleapis.com/auth/webmasters.readonly",)


class GSCAPIError(RuntimeError):
    """Raised when the Google Search Console API cannot return usable data."""


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _row_float(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _row_int(row: dict[str, Any], key: str) -> int:
    return int(round(_row_float(row, key)))


@dataclass(frozen=True)
class GSCClient:
    """Google Search Console Search Analytics client with service-account auth.

    The implementation keeps service-account support while staying easy to swap for a future OAuth credential provider:
    callers only depend on the fetch_* methods and not on the credential construction details.
    """

    credentials: Any
    site_url: str
    auth_source: str
    credentials_file: str | None = None
    service: Any | None = None

    @classmethod
    def from_settings(cls, db: Any | None = None) -> GSCClient:
        google_credentials = resolve_google_credentials(db, GSC_SCOPES)
        if not settings.gsc_site_url:
            raise MissingGoogleCredentialsError(
                "GSC_SITE_URL is not configured. Add your verified GSC property URL to .env."
            )
        return cls(
            credentials=google_credentials.credentials,
            site_url=settings.gsc_site_url,
            auth_source=google_credentials.source,
            credentials_file=google_credentials.credentials_file,
        )

    def status(self) -> dict[str, object]:
        payload: dict[str, object] = {"configured": True, "site_url": self.site_url, "auth_source": self.auth_source}
        if self.credentials_file:
            payload["credentials_file"] = self.credentials_file
        return payload

    def _service(self) -> Any:
        if self.service is not None:
            return self.service
        missing_discovery = util.find_spec("googleapiclient.discovery") is None
        if missing_discovery:
            raise GSCAPIError(
                "Google Search Console dependencies are not installed. "
                "Install google-api-python-client and google-auth."
            )

        discovery = import_module("googleapiclient.discovery")
        return discovery.build("searchconsole", "v1", credentials=self.credentials, cache_discovery=False)

    def _query(
        self,
        site_url: str,
        *,
        dimensions: list[str],
        limit: int,
        dimension_filter_groups: list[dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        end_date = _today_utc() - timedelta(days=2)
        start_date = end_date - timedelta(days=28)
        body: dict[str, object] = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "dimensions": dimensions,
            "rowLimit": max(1, limit),
            "searchType": "web",
        }
        if dimension_filter_groups:
            body["dimensionFilterGroups"] = dimension_filter_groups

        try:
            response = self._service().searchanalytics().query(siteUrl=site_url, body=body).execute()
        except Exception as exc:  # noqa: BLE001 - third-party clients raise multiple transport/auth exceptions.
            raise GSCAPIError(f"Google Search Console API request failed: {exc}") from exc

        rows = response.get("rows", []) if isinstance(response, dict) else []
        if not isinstance(rows, list):
            return []
        return [self._metric_from_row(row, dimensions) for row in rows if isinstance(row, dict)]

    def _metric_from_row(self, row: dict[str, Any], dimensions: list[str]) -> dict[str, object]:
        keys = row.get("keys", [])
        key_values = keys if isinstance(keys, list) else []
        dimensions_by_name = {
            dimension: str(key_values[index]) if index < len(key_values) else ""
            for index, dimension in enumerate(dimensions)
        }
        page_url = dimensions_by_name.get("page") or ""
        query = dimensions_by_name.get("query") or ""
        row_date = dimensions_by_name.get("date") or _today_utc().isoformat()
        return {
            "page_url": page_url,
            "query": query,
            "clicks": _row_int(row, "clicks"),
            "impressions": _row_int(row, "impressions"),
            "ctr": _row_float(row, "ctr"),
            "average_position": _row_float(row, "position"),
            "date": row_date,
            "source": "gsc",
        }

    def fetch_top_queries(self, site_url: str, limit: int = 100) -> list[dict[str, object]]:
        """Fetch top query/page/date rows for a verified Search Console property."""
        return self._query(site_url, dimensions=["query", "page", "date"], limit=limit)

    def fetch_page_queries(self, page_url: str, limit: int = 50) -> list[dict[str, object]]:
        """Fetch query rows for one page using the client's configured GSC property."""
        return self._query(
            self.site_url,
            dimensions=["query", "page", "date"],
            limit=limit,
            dimension_filter_groups=[
                {
                    "filters": [
                        {
                            "dimension": "page",
                            "operator": "equals",
                            "expression": page_url,
                        }
                    ]
                }
            ],
        )

    def fetch_low_ctr_opportunities(self, limit: int = 100) -> list[dict[str, object]]:
        """Return high-impression rows with weak CTR and reachable average rankings."""
        rows = self.fetch_top_queries(self.site_url, limit=max(limit * 3, limit))
        opportunities = [
            row
            for row in rows
            if int(row.get("impressions") or 0) >= 50
            and float(row.get("ctr") or 0.0) < 0.03
            and 4 <= float(row.get("average_position") or 0.0) <= 20
        ]
        return opportunities[:limit]
