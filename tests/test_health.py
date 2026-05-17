from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.integrations.google_auth import require_service_account_file
from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def test_health_startup_smoke_with_testclient(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_stats_empty(client: TestClient) -> None:
    response = client.get("/stats")
    assert response.status_code == 200
    assert "total_runs" in response.json()


def test_google_credentials_json_env_materializes_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.integrations.google_auth.settings.google_application_credentials_json", '{"type":"service_account"}'
    )
    monkeypatch.setattr("app.integrations.google_auth.settings.google_service_account_file", None)

    credentials_path = require_service_account_file()

    assert credentials_path.exists()
    assert credentials_path.read_text(encoding="utf-8") == '{"type":"service_account"}'
