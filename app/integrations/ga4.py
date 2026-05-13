from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.integrations.google_auth import MissingGoogleCredentialsError, resolve_google_credentials

GA4_SCOPES = ("https://www.googleapis.com/auth/analytics.readonly",)


@dataclass(frozen=True)
class GA4Client:
    """Small GA4 wrapper with explicit configuration validation."""

    credentials: Any
    property_id: str
    auth_source: str
    credentials_file: str | None = None

    @classmethod
    def from_settings(cls, db: Any | None = None) -> "GA4Client":
        google_credentials = resolve_google_credentials(db, GA4_SCOPES)
        if not settings.ga4_property_id:
            raise MissingGoogleCredentialsError(
                "GA4_PROPERTY_ID is not configured. Add your GA4 numeric property ID to .env."
            )
        return cls(
            credentials=google_credentials.credentials,
            property_id=settings.ga4_property_id,
            auth_source=google_credentials.source,
            credentials_file=google_credentials.credentials_file,
        )

    def status(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "configured": True,
            "property_id": self.property_id,
            "auth_source": self.auth_source,
        }
        if self.credentials_file:
            payload["credentials_file"] = self.credentials_file
        return payload

    def run_report(self) -> dict[str, object]:
        """Placeholder for production GA4 Data API calls once credentials are supplied."""
        return {"message": "GA4 client configured. Install google-analytics-data to run live reports."}
