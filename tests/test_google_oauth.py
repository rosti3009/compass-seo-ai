from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import GoogleOAuthToken
from app.integrations.google_auth import resolve_google_credentials
from app.main import app


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def oauth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.settings.google_oauth_client_id", "client-id")
    monkeypatch.setattr("app.api.routes.settings.google_oauth_client_secret", "client-secret")
    monkeypatch.setattr("app.api.routes.settings.google_oauth_redirect_uri", "https://example.com/auth/google/callback")
    monkeypatch.setattr("app.integrations.google_auth.settings.google_oauth_client_id", "client-id")
    monkeypatch.setattr("app.integrations.google_auth.settings.google_oauth_client_secret", "client-secret")
    monkeypatch.setattr(
        "app.integrations.google_auth.settings.google_oauth_redirect_uri", "https://example.com/auth/google/callback"
    )


def test_google_oauth_status_without_token(client: TestClient) -> None:
    response = client.get("/auth/google/status")

    assert response.status_code == 200
    assert response.json() == {"connected": False, "scopes": []}


def test_google_oauth_callback_stores_mocked_token(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    oauth_settings: None,
) -> None:
    class MockTokenResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/webmasters.readonly https://www.googleapis.com/auth/analytics.readonly",
            }

    def mock_post(url: str, data: dict[str, object], timeout: int) -> MockTokenResponse:
        assert url == "https://oauth2.googleapis.com/token"
        assert data["code"] == "mock-code"
        assert data["client_id"] == "client-id"
        assert data["client_secret"] == "client-secret"  # noqa: S105
        assert timeout == 20
        return MockTokenResponse()

    monkeypatch.setattr("app.api.routes.requests.post", mock_post)

    response = client.get("/auth/google/callback?code=mock-code")

    assert response.status_code == 200
    assert response.json()["connected"] is True
    token = db_session.query(GoogleOAuthToken).one()
    assert token.access_token == "access-token"  # noqa: S105
    assert token.refresh_token == "refresh-token"  # noqa: S105
    assert token.scopes == [
        "https://www.googleapis.com/auth/webmasters.readonly",
        "https://www.googleapis.com/auth/analytics.readonly",
    ]


def test_google_auth_helper_prefers_oauth_token(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    oauth_settings: None,
) -> None:
    token = GoogleOAuthToken(
        provider="google",
        access_token="oauth-access",  # noqa: S106
        refresh_token="oauth-refresh",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes_json='["https://www.googleapis.com/auth/webmasters.readonly"]',
        expiry=datetime(2026, 5, 13, tzinfo=UTC),
    )
    db_session.add(token)
    db_session.commit()

    class MockCredentials:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class MockCredentialsModule:
        Credentials = MockCredentials

    monkeypatch.setattr("app.integrations.google_auth.util.find_spec", lambda name: object())
    monkeypatch.setattr("app.integrations.google_auth.import_module", lambda name: MockCredentialsModule)

    resolved = resolve_google_credentials(db_session, ("https://www.googleapis.com/auth/webmasters.readonly",))

    assert resolved.source == "oauth"
    assert resolved.credentials.kwargs["token"] == "oauth-access"  # noqa: S105


def test_google_auth_helper_falls_back_to_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockServiceAccountCredentials:
        @classmethod
        def from_service_account_file(cls, filename: str, scopes: list[str]) -> dict[str, object]:
            return {"filename": filename, "scopes": scopes}

    class MockServiceAccountModule:
        Credentials = MockServiceAccountCredentials

    monkeypatch.setattr(
        "app.integrations.google_auth.settings.google_application_credentials_json", '{"type":"service_account"}'
    )
    monkeypatch.setattr("app.integrations.google_auth.settings.google_service_account_file", None)
    monkeypatch.setattr("app.integrations.google_auth.util.find_spec", lambda name: object())
    monkeypatch.setattr("app.integrations.google_auth.import_module", lambda name: MockServiceAccountModule)

    resolved = resolve_google_credentials(None, ("https://www.googleapis.com/auth/analytics.readonly",))

    assert resolved.source == "service_account"
    assert resolved.credentials["scopes"] == ["https://www.googleapis.com/auth/analytics.readonly"]
    assert resolved.credentials_file is not None


def test_dashboard_connect_google_link_exists(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/auth/google/start"' in response.text
    assert "Connect Google Account" in response.text


def test_google_diagnostics_endpoint_returns_safe_configuration_booleans(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.integrations.google_auth.settings.gsc_site_url", "sc-domain:compassgrill.co.il")
    monkeypatch.setattr(
        "app.integrations.google_auth.settings.google_application_credentials_json", '{"type":"service_account"}'
    )
    monkeypatch.setattr("app.integrations.google_auth.settings.google_service_account_file", None)
    monkeypatch.setattr("app.integrations.google_auth.settings.google_oauth_client_id", "client-id")
    monkeypatch.setattr("app.integrations.google_auth.settings.google_oauth_redirect_uri", "https://example.com/callback")

    response = client.get("/integrations/google/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "gsc_site_url_configured": True,
        "google_application_credentials_json_configured": True,
        "google_service_account_file_configured": False,
        "google_oauth_client_id_configured": True,
        "google_oauth_redirect_uri_configured": True,
        "resolved_auth_method": "service_account",
        "gsc_site_url_value_redacted": "sc-domain:co**************il",
    }
    assert "service_account" not in str(payload.get("gsc_site_url_value_redacted"))
    assert "compassgrill.co.il" not in str(payload)


def test_google_diagnostics_endpoint_prefers_connected_oauth_token(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.integrations.google_auth.settings.gsc_site_url", None)
    monkeypatch.setattr("app.integrations.google_auth.settings.google_application_credentials_json", None)
    monkeypatch.setattr("app.integrations.google_auth.settings.google_service_account_file", "google-credentials.json")
    monkeypatch.setattr("app.integrations.google_auth.settings.google_oauth_client_id", None)
    monkeypatch.setattr("app.integrations.google_auth.settings.google_oauth_redirect_uri", None)
    db_session.add(
        GoogleOAuthToken(
            provider="google",
            access_token="oauth-access",  # noqa: S106
            refresh_token="oauth-refresh",  # noqa: S106
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
            client_id="client-id",
            client_secret="client-secret",  # noqa: S106
            scopes_json='["https://www.googleapis.com/auth/webmasters.readonly"]',
        )
    )
    db_session.commit()

    response = client.get("/integrations/google/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["resolved_auth_method"] == "oauth"
    assert payload["google_service_account_file_configured"] is True
    assert payload["gsc_site_url_value_redacted"] is None
    assert "oauth-access" not in str(payload)
    assert "oauth-refresh" not in str(payload)
    assert "client-secret" not in str(payload)
