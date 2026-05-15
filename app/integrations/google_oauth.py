from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

BASE_DIR = Path(__file__).resolve().parents[2]

CLIENT_SECRET_FILE = (
    BASE_DIR.parent
    / "credentials"
    / "oauth-client.json"
)

TOKEN_FILE = (
    BASE_DIR.parent
    / "credentials"
    / "oauth-token.json"
)


def authenticate_google():
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        SCOPES,
    )

    credentials = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w", encoding="utf-8") as token:
        token.write(credentials.to_json())

    print("Google OAuth connected successfully.")
    print(f"Token saved to: {TOKEN_FILE}")


if __name__ == "__main__":
    authenticate_google()