from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven application settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Compass SEO AI"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./compass_seo.db"
    target_domain: str = "https://compassgrill.co.il"
    crawler_max_pages: int = Field(default=25, ge=1, le=250)
    crawler_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    google_service_account_file: str | None = None
    google_application_credentials_json: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    gsc_site_url: str | None = None
    ga4_property_id: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"


@lru_cache
def get_settings() -> Settings:
    """Return cached settings for dependency-free imports."""
    return Settings()


settings = get_settings()
