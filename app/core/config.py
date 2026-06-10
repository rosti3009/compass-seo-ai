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
    istore_base_url: str | None = None
    istore_company_id: str | None = None
    istore_x_token: str | None = None
    istore_api_token: str | None = None
    istore_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    istore_publish_enabled: bool = False
    istore_safe_mode: bool = True
    istore_admin_base_url: str = "https://app.istores.co.il"
    istore_admin_cookie: str | None = None
    istore_raw_cookie_header: str | None = None
    istore_xsrf_token: str | None = None
    istore_xsrf_token_override: str | None = None
    istore_inertia_version: str | None = None
    istore_create_submit_mode: str = "form"
    istore_create_minimal_payload: bool = False
    istore_use_browser_headers: bool = False
    istore_create_follow_redirects: bool = False
    istore_language_id: int = 3
    istore_blog_is_blog: int = 0
    istore_browser_headless: bool = True
    istore_browser_storage_state_path: str = "/var/data/istore_storage_state.json"
    istore_browser_slowmo_ms: int = Field(default=0, ge=0, le=10000)
    istore_browser_timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    istore_browser_wait_ms: int = Field(default=1000, ge=0, le=30000)
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    content_daily_articles_enabled: bool = False
    daily_article_generation_enabled: bool = False
    daily_article_generation_hour: int = Field(default=8, ge=0, le=23)
    daily_article_generation_timezone: str = "Asia/Jerusalem"
    image_provider: str | None = None
    manual_action_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached settings for dependency-free imports."""
    return Settings()


settings = get_settings()


def get_istore_token() -> str | None:
    """Return ISTORE auth token with backward compatibility for legacy key names."""
    token = getattr(settings, "istore_x_token", None)
    if token:
        return token
    return getattr(settings, "istore_api_token", None)
