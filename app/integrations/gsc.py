from dataclasses import dataclass

from app.core.config import settings
from app.integrations.google_auth import MissingGoogleCredentialsError, require_service_account_file


@dataclass(frozen=True)
class GSCClient:
    """Small Google Search Console wrapper with explicit configuration validation."""

    credentials_file: str
    site_url: str

    @classmethod
    def from_settings(cls) -> "GSCClient":
        credentials_file = require_service_account_file()
        if not settings.gsc_site_url:
            raise MissingGoogleCredentialsError(
                "GSC_SITE_URL is not configured. Add your verified GSC property URL to .env."
            )
        return cls(credentials_file=str(credentials_file), site_url=settings.gsc_site_url)

    def status(self) -> dict[str, object]:
        return {"configured": True, "site_url": self.site_url, "credentials_file": self.credentials_file}

    def search_analytics_query(self) -> dict[str, object]:
        """Placeholder for production GSC API calls once credentials are supplied."""
        return {"message": "GSC client configured. Install google-api-python-client to run live queries."}
