from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest

from app.core.config import settings
from app.integrations.oauth_credentials import load_oauth_credentials


class GA4Client:
    def __init__(self) -> None:
        credentials = load_oauth_credentials()

        self.client = BetaAnalyticsDataClient(
            credentials=credentials,
        )

        self.property_id = settings.ga4_property_id

    @classmethod
    def from_settings(cls) -> "GA4Client":
        return cls()

    def status(self) -> dict:
        request = RunReportRequest(
            property=f"properties/{self.property_id}",
            dimensions=[{"name": "country"}],
            metrics=[{"name": "activeUsers"}],
            date_ranges=[{"start_date": "7daysAgo", "end_date": "today"}],
            limit=1,
        )

        response = self.client.run_report(request)

        return {
            "connected": True,
            "property_id": self.property_id,
            "rows_returned": len(response.rows),
        }