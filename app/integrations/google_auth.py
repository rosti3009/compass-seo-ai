import json
import os
import tempfile
from pathlib import Path

from app.core.config import settings


class MissingGoogleCredentialsError(RuntimeError):
    """Raised when Google API credentials are not configured."""


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
            "GOOGLE_SERVICE_ACCOUNT_FILE."
        )
    credentials_path = Path(settings.google_service_account_file)
    if not credentials_path.exists():
        raise MissingGoogleCredentialsError(
            f"Google service account file does not exist: {credentials_path}. Check GOOGLE_SERVICE_ACCOUNT_FILE."
        )
    return credentials_path
