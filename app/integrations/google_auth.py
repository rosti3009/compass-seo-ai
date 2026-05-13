import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module, util
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import GoogleOAuthToken

GOOGLE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
)


class MissingGoogleCredentialsError(RuntimeError):
    """Raised when Google API credentials are not configured."""


@dataclass(frozen=True)
class GoogleCredentials:
    """Resolved Google credentials and their source."""

    credentials: Any
    source: str
    credentials_file: str | None = None


def _service_account_json_path() -> Path:
    """Materialize Render-friendly JSON credentials from an environment variable."""
    credentials_json = settings.google_application_credentials_json
    if not credentials_json:
        raise MissingGoogleCredentialsError(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON is not configured. Add service account JSON to your environment."
        )

    try:
        json.loads(credentials_json)
    except json.JSONDecodeError as exc:
        raise MissingGoogleCredentialsError(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON must contain valid Google service account JSON."
        ) from exc

    credentials_path = Path(tempfile.gettempdir()) / "google-application-credentials.json"
    credentials_path.write_text(credentials_json, encoding="utf-8")
    os.chmod(credentials_path, 0o600)
    return credentials_path


def require_service_account_file() -> Path:
    """Return validated Google service account JSON credentials or raise a clear error."""
    if settings.google_application_credentials_json:
        return _service_account_json_path()

    if not settings.google_service_account_file:
        raise MissingGoogleCredentialsError(
            "Google credentials are not configured. Set GOOGLE_APPLICATION_CREDENTIALS_JSON or "
            "GOOGLE_SERVICE_ACCOUNT_FILE. To use user OAuth, set GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET, and GOOGLE_OAUTH_REDIRECT_URI, then connect at /auth/google/start."
        )
    credentials_path = Path(settings.google_service_account_file)
    if not credentials_path.exists():
        raise MissingGoogleCredentialsError(
            f"Google service account file does not exist: {credentials_path}. Check GOOGLE_SERVICE_ACCOUNT_FILE."
        )
    return credentials_path


def latest_google_oauth_token(db: Session | None) -> GoogleOAuthToken | None:
    """Return the newest stored Google OAuth token, if a database session is available."""
    if db is None:
        return None
    return (
        db.query(GoogleOAuthToken)
        .filter(GoogleOAuthToken.provider == "google")
        .order_by(GoogleOAuthToken.updated_at.desc(), GoogleOAuthToken.id.desc())
        .first()
    )


def _oauth_credentials_from_token(token: GoogleOAuthToken, scopes: tuple[str, ...] | list[str]) -> Any:
    if util.find_spec("google.oauth2.credentials") is None:
        raise MissingGoogleCredentialsError("Google OAuth dependencies are not installed. Install google-auth.")
    credentials_module = import_module("google.oauth2.credentials")
    return credentials_module.Credentials(
        token=token.access_token,
        refresh_token=token.refresh_token,
        token_uri=token.token_uri,
        client_id=token.client_id,
        client_secret=token.client_secret,
        scopes=list(scopes),
        expiry=token.expiry,
    )


def _service_account_credentials(scopes: tuple[str, ...] | list[str]) -> GoogleCredentials:
    credentials_file = require_service_account_file()
    if util.find_spec("google.oauth2.service_account") is None:
        raise MissingGoogleCredentialsError(
            "Google service account dependencies are not installed. Install google-auth."
        )
    service_account = import_module("google.oauth2.service_account")
    credentials = service_account.Credentials.from_service_account_file(str(credentials_file), scopes=list(scopes))
    return GoogleCredentials(credentials=credentials, source="service_account", credentials_file=str(credentials_file))


def resolve_google_credentials(
    db: Session | None = None,
    scopes: tuple[str, ...] | list[str] = GOOGLE_OAUTH_SCOPES,
) -> GoogleCredentials:
    """Prefer stored user OAuth credentials, falling back to service-account configuration."""
    token = latest_google_oauth_token(db)
    if token is not None:
        return GoogleCredentials(credentials=_oauth_credentials_from_token(token, scopes), source="oauth")

    try:
        return _service_account_credentials(scopes)
    except MissingGoogleCredentialsError as exc:
        raise MissingGoogleCredentialsError(
            "Google credentials are not configured. Connect Google OAuth at /auth/google/start or set "
            "GOOGLE_APPLICATION_CREDENTIALS_JSON / GOOGLE_SERVICE_ACCOUNT_FILE for service-account fallback."
        ) from exc


def oauth_status(db: Session) -> dict[str, object]:
    """Return whether a Google OAuth token is connected and which scopes were stored."""
    token = latest_google_oauth_token(db)
    if token is None:
        return {"connected": False, "scopes": []}
    return {
        "connected": True,
        "provider": token.provider,
        "scopes": token.scopes,
        "expiry": token.expiry.isoformat() if token.expiry else None,
    }


def utc_expiry_from_seconds(expires_in: object) -> datetime | None:
    """Convert a Google token expires_in value to an aware UTC datetime."""
    try:
        seconds = int(expires_in or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    from datetime import timedelta

    return datetime.now(UTC) + timedelta(seconds=seconds)
