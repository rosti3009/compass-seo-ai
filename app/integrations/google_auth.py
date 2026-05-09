from pathlib import Path

from app.core.config import settings


class MissingGoogleCredentialsError(RuntimeError):
    """Raised when Google API credentials are not configured."""


def require_service_account_file() -> Path:
    """Return a validated Google service account JSON path or raise a clear error."""
    if not settings.google_service_account_file:
        raise MissingGoogleCredentialsError(
            "GOOGLE_SERVICE_ACCOUNT_FILE is not configured. Add a service account JSON path to .env."
        )
    credentials_path = Path(settings.google_service_account_file)
    if not credentials_path.exists():
        raise MissingGoogleCredentialsError(
            f"Google service account file does not exist: {credentials_path}. Check GOOGLE_SERVICE_ACCOUNT_FILE."
        )
    return credentials_path
