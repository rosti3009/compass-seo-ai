from collections.abc import Generator
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.integrations.istore import REDACTED_TOKEN, IStoreAPIError, IStoreClient
from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def istore_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.integrations.istore.settings.istore_base_url", "https://istore.example/api")
    monkeypatch.setattr("app.integrations.istore.settings.istore_company_id", "company-123")
    monkeypatch.setattr("app.integrations.istore.settings.istore_x_token", "super-secret-token")  # noqa: S105
    monkeypatch.setattr("app.integrations.istore.settings.istore_timeout_seconds", 7.5)


class MockResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class RecordingSession:
    def __init__(self, response: MockResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> MockResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def test_istore_status_redacts_token(istore_settings: None) -> None:
    status = IStoreClient.from_settings().status()

    assert status == {
        "configured": True,
        "base_url": "https://istore.example/api",
        "company_id": "company-123",
        "x_token": REDACTED_TOKEN,
        "timeout_seconds": 7.5,
        "mode": "read_only",
        "allowed_methods": ["GET"],
    }
    assert "super-secret-token" not in repr(status)  # noqa: S105


def test_istore_client_uses_get_only_with_token_header(istore_settings: None) -> None:
    session = RecordingSession(MockResponse([{"id": "sku-1"}]))
    istore = IStoreClient.from_settings()
    client = IStoreClient(
        base_url=istore.base_url,
        company_id=istore.company_id,
        x_token=istore.x_token,
        timeout_seconds=istore.timeout_seconds,
        session=session,  # type: ignore[arg-type]
    )

    assert client.list_products() == [{"id": "sku-1"}]

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://istore.example/api/products"
    assert call["headers"] == {"Accept": "application/json", "X-Token": "super-secret-token"}  # noqa: S105
    assert call["params"] == {"company_id": "company-123"}
    assert call["timeout"] == 7.5


def test_istore_client_encodes_product_id(istore_settings: None) -> None:
    session = RecordingSession(MockResponse({"id": "sku/1"}))
    istore = IStoreClient.from_settings()
    client = IStoreClient(
        base_url=istore.base_url,
        company_id=istore.company_id,
        x_token=istore.x_token,
        timeout_seconds=istore.timeout_seconds,
        session=session,  # type: ignore[arg-type]
    )

    assert client.get_product("sku/1") == {"id": "sku/1"}

    assert session.calls[0]["url"] == "https://istore.example/api/products/sku%2F1"


def test_istore_api_errors_redact_token(istore_settings: None) -> None:
    class FailingSession:
        def get(self, *_args: object, **_kwargs: object) -> object:
            raise requests.RequestException("super-secret-token leaked")  # noqa: S105

    istore = IStoreClient.from_settings()
    client = IStoreClient(
        base_url=istore.base_url,
        company_id=istore.company_id,
        x_token=istore.x_token,
        timeout_seconds=istore.timeout_seconds,
        session=FailingSession(),  # type: ignore[arg-type]
    )

    with pytest.raises(IStoreAPIError) as exc_info:
        client.list_products()

    assert "super-secret-token" not in str(exc_info.value)  # noqa: S105
    assert REDACTED_TOKEN in str(exc_info.value)


def test_istore_status_endpoint_redacts_token(client: TestClient, istore_settings: None) -> None:
    response = client.get("/integrations/istore/status")

    assert response.status_code == 200
    body = response.json()
    assert body["x_token"] == REDACTED_TOKEN
    assert "super-secret-token" not in response.text  # noqa: S105


def test_istore_products_endpoint_is_get_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeIStoreClient:
        def list_products(self) -> list[dict[str, str]]:
            return [{"id": "sku-1"}]

    monkeypatch.setattr("app.api.routes.IStoreClient.from_settings", lambda: FakeIStoreClient())

    response = client.get("/integrations/istore/products")

    assert response.status_code == 200
    assert response.json() == {"products": [{"id": "sku-1"}]}
    assert client.put("/integrations/istore/products").status_code == 405


def test_istore_product_endpoint_is_get_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeIStoreClient:
        def get_product(self, product_id: str) -> dict[str, str]:
            return {"id": product_id}

    monkeypatch.setattr("app.api.routes.IStoreClient.from_settings", lambda: FakeIStoreClient())

    response = client.get("/integrations/istore/products/sku-1")

    assert response.status_code == 200
    assert response.json() == {"product": {"id": "sku-1"}}
    assert client.put("/integrations/istore/products/sku-1").status_code == 405


def test_istore_product_seo_analysis_endpoint_is_read_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeIStoreClient:
        def get_product(self, product_id: str) -> dict[str, Any]:
            return {
                "id": product_id,
                "name": "גריל גז מקצועי",
                "description": "<p>גריל איכותי לגינה עם מבערי נירוסטה, משטח צלייה רחב ואחריות יבואן.</p>",
                "price": 2490,
                "category": "גרילים",
                "images": ["front.jpg"],
            }

    monkeypatch.setattr("app.api.routes.IStoreClient.from_settings", lambda: FakeIStoreClient())

    response = client.get("/integrations/istore/products/sku-1/seo-analysis.json")

    assert response.status_code == 200
    body = response.json()
    assert body["product"]["id"] == "sku-1"
    assert body["analysis"]["product_id"] == "sku-1"
    assert body["analysis"]["suggested_h1"] == "גריל גז מקצועי"
    assert "חסרה כותרת SEO" in body["analysis"]["issues"]
    assert client.put("/integrations/istore/products/sku-1/seo-analysis.json").status_code == 405


def test_istore_product_seo_analysis_view_renders_template(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeIStoreClient:
        def get_product(self, product_id: str) -> dict[str, Any]:
            return {
                "id": product_id,
                "name": "מעשנת פחם",
                "meta_title": "מעשנת פחם מקצועית לגינה | קומפס",
                "meta_description": (
                    "מעשנת פחם איכותית עם שטח צלייה גדול, שליטה בחום "
                    "ואביזרים משלימים לחוויית ברביקיו ביתית."
                ),
                "description": "מעשנת פחם עמידה שמיועדת לבישול ארוך, צלייה ועישון בשרים בבית ובגינה.",
                "category": "מעשנות",
                "url": "https://example.test/products/smoker",
                "images": ["smoker.jpg", "smoker-side.jpg"],
            }

    monkeypatch.setattr("app.api.routes.IStoreClient.from_settings", lambda: FakeIStoreClient())

    response = client.get("/integrations/istore/products/smoker-1/seo-analysis")

    assert response.status_code == 200
    assert "Product SEO analysis" in response.text
    assert "מעשנת פחם" in response.text
    assert "View JSON" in response.text

from app.services.istore_browser_automation import IStoreBrowserCreateResult


def test_browser_create_test_dry_run_returns_dom_diagnostics(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Draft:
        title = "Title"
        article_body = "Body"
        meta_title = "Meta"
        meta_description = "Meta desc"
        slug = "title"

    monkeypatch.setattr("app.api.routes._get_content_draft_or_404", lambda db, draft_id: _Draft())

    monkeypatch.setattr(
        "app.api.routes.create_shop_information_page",
        lambda payload, dry_run: IStoreBrowserCreateResult(
            success=True,
            current_url="https://app.istores.co.il/client/shop_information/create",
            external_content_id=None,
            otp_required=False,
            error=None,
            screenshot_path=None,
            selector_availability={"title": False, "description": False},
            planned_fields={"title": "Title"},
            dom_diagnostics={
                "total_inputs": 2,
                "inputs": [],
                "buttons": [],
                "visible_text_sample": "sample",
            },
        ),
    )

    response = client.post("/debug/istore/browser-create-test?draft_id=4&dry_run=true")

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["selector_availability"] == {"title": False, "description": False}
    assert "dom_diagnostics" in body
    assert body["dom_diagnostics"]["total_inputs"] == 2
    assert body["dom_diagnostics"]["inputs"] == []
    assert body["dom_diagnostics"]["buttons"] == []
    assert body["dom_diagnostics"]["visible_text_sample"] == "sample"
