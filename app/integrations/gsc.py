from googleapiclient.discovery import build

from app.core.config import settings
from app.integrations.oauth_credentials import load_oauth_credentials


class GSCClient:
    def __init__(self) -> None:
        credentials = load_oauth_credentials()
        self.service = build("searchconsole", "v1", credentials=credentials)
        self.site_url = settings.gsc_site_url

    @classmethod
    def from_settings(cls) -> "GSCClient":
        return cls()

    def status(self) -> dict:
        sites = self.service.sites().list().execute()
        site_entries = sites.get("siteEntry", [])

        matching_sites = [
            site
            for site in site_entries
            if site.get("siteUrl") == self.site_url
        ]

        return {
            "connected": True,
            "site_url": self.site_url,
            "sites_count": len(site_entries),
            "site_found": bool(matching_sites),
            "matching_sites": matching_sites,
        }

    def search_performance(self, days: int = 28) -> dict:
        request = {
            "startDate": "2026-04-11",
            "endDate": "2026-05-09",
            "dimensions": ["query", "page"],
            "rowLimit": 25,
        }

        response = (
            self.service.searchanalytics()
            .query(siteUrl=self.site_url, body=request)
            .execute()
        )

        rows = response.get("rows", [])

        return {
            "connected": True,
            "site_url": self.site_url,
            "rows_returned": len(rows),
            "rows": rows,
        }

    def seo_opportunities(self) -> dict:
        performance = self.search_performance()
        rows = performance.get("rows", [])

        opportunities = []

        for row in rows:
            keys = row.get("keys", [])
            query = keys[0] if len(keys) > 0 else None
            page = keys[1] if len(keys) > 1 else None

            clicks = row.get("clicks", 0)
            impressions = row.get("impressions", 0)
            ctr = row.get("ctr", 0)
            position = row.get("position", 0)

            opportunity_type = None
            priority_score = 0

            if impressions >= 100 and 4 <= position <= 15 and ctr < 0.08:
                opportunity_type = "high_impressions_low_ctr"
                priority_score = impressions * (1 - ctr) / max(position, 1)

            elif 4 <= position <= 10 and impressions >= 50:
                opportunity_type = "page_one_push"
                priority_score = impressions / max(position, 1)

            elif position <= 3 and ctr < 0.12 and impressions >= 50:
                opportunity_type = "title_meta_ctr_improvement"
                priority_score = impressions * (1 - ctr)

            if opportunity_type:
                opportunities.append(
                    {
                        "query": query,
                        "page": page,
                        "clicks": clicks,
                        "impressions": impressions,
                        "ctr": round(ctr, 4),
                        "position": round(position, 2),
                        "opportunity_type": opportunity_type,
                        "priority_score": round(priority_score, 2),
                    }
                )

        opportunities.sort(
            key=lambda item: item["priority_score"],
            reverse=True,
        )

        return {
            "connected": True,
            "site_url": self.site_url,
            "total_opportunities": len(opportunities),
            "opportunities": opportunities[:25],
        }

    def seo_recommendations(self) -> dict:
        opportunities_data = self.seo_opportunities()
        opportunities = opportunities_data.get("opportunities", [])

        recommendations = []

        for item in opportunities:
            query = item["query"]
            page = item["page"]
            opportunity_type = item["opportunity_type"]
            impressions = item["impressions"]
            ctr = item["ctr"]
            position = item["position"]

            if opportunity_type == "high_impressions_low_ctr":
                issue = "High impressions but low CTR"
                action = "Improve the title, meta description, and opening paragraph to better match the search intent."
            elif opportunity_type == "page_one_push":
                issue = "Ranking close to top results"
                action = "Add internal links, expand the content, and strengthen topical relevance around this query."
            elif opportunity_type == "title_meta_ctr_improvement":
                issue = "Strong ranking but weak click-through rate"
                action = "Rewrite title and meta description to create a stronger reason to click."
            else:
                issue = "SEO opportunity detected"
                action = "Review this page and improve search intent alignment."

            recommendations.append(
                {
                    "query": query,
                    "page": page,
                    "issue": issue,
                    "recommended_action": action,
                    "suggested_title_direction": f"{query} | מדריך מקצועי, טיפים והמלצות",
                    "suggested_meta_direction": (
                        f"מחפשים {query}? קבלו מדריך ברור, מקצועי ומעשי "
                        "עם טיפים, הסברים והמלצות שיעזרו לכם לבחור ולהצליח."
                    ),
                    "priority": "high" if impressions >= 300 or position <= 5 else "medium",
                    "impressions": impressions,
                    "ctr": ctr,
                    "position": position,
                }
            )

        return {
            "connected": True,
            "site_url": self.site_url,
            "total_recommendations": len(recommendations),
            "recommendations": recommendations[:25],
        }

    def get_page_metrics(
        self,
        page_url: str,
    ) -> dict:
        """Get Search Console metrics for a specific page."""

        request = {
            "startDate": "2026-01-01",
            "endDate": "2026-12-31",
            "dimensions": ["page"],
            "dimensionFilterGroups": [
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
            "rowLimit": 1,
        }

        response = (
            self.service.searchanalytics()
            .query(
                siteUrl=self.site_url,
                body=request,
            )
            .execute()
        )

        rows = response.get("rows", [])

        if not rows:
            return {
                "clicks": 0,
                "impressions": 0,
                "ctr": 0,
                "position": 0,
            }

        row = rows[0]

        return {
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": row.get("ctr", 0),
            "position": row.get("position", 0),
        }