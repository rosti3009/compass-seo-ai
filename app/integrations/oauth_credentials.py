from pathlib import Path

from google.oauth2.credentials import Credentials

BASE_DIR = Path(__file__).resolve().parents[2]

TOKEN_FILE = (
    BASE_DIR.parent
    / "credentials"
    / "oauth-token.json"
)

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]


def load_oauth_credentials() -> Credentials:
    return Credentials.from_authorized_user_file(
        str(TOKEN_FILE),
        SCOPES,
    )