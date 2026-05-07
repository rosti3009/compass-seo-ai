from dataclasses import dataclass

from app.core.config import settings
from app.integrations.google_auth import MissingGoogleCredentialsError, require_service_account_file


@dataclass(frozen=True)
class GA4Client:
    """Small GA4 wrapper with explicit configuration validation."""

    credentials_file: str
    property_id: str

    @classmethod
    def from_settings(cls) -> "GA4Client":
        credentials_file = require_service_account_file()
        if not settings.ga4_property_id:
            raise MissingGoogleCredentialsError(
                "GA4_PROPERTY_ID is not configured. Add your GA4 numeric property ID to .env."
            )
        return cls(credentials_file=str(credentials_file), property_id=settings.ga4_property_id)

    def status(self) -> dict[str, object]:
        return {"configured": True, "property_id": self.property_id, "credentials_file": self.credentials_file}

    def run_report(self) -> dict[str, object]:
        """Placeholder for production GA4 Data API calls once credentials are supplied."""
        return {"message": "GA4 client configured. Install google-analytics-data to run live reports."}
