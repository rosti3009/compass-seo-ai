from __future__ import annotations

import pytest

from app.services.istore_blog_publisher import (
    IStoreAdminShopInformationPublisher,
    IStoreBlogPublishError,
    MissingIStoreAdminSettingsError,
)


class _Draft:
    title = "T1"
    slug = "slug1"
    article_body = "<div>x</div>"
    meta_title = "M1"
    meta_description = "D"
    target_url = "https://compassgrill.co.il/blog/slug1"


def _publisher() -> IStoreAdminShopInformationPublisher:
    return IStoreAdminShopInformationPublisher(
        base_url="https://app.istores.co.il",
        admin_cookie="session=abc",
        xsrf_token="token123",
        language_id=3,
        blog_is_blog=1,
    )


def test_payload_shape_matches_shop_information_create_contract() -> None:
    publisher = _publisher()
    payload = publisher._build_payload(_Draft())
    assert payload == {
        "descriptions": {
            "3": {
                "title": "T1",
                "description": "<div>x</div>",
                "meta_title": "M1",
                "meta_description": "D",
            }
        },
        "dynamic_fields": [],
        "end_date": None,
        "is_blog": 1,
        "keyword": "",
        "sort_order": 0,
        "start_date": None,
        "status": 1,
    }


def test_extract_id_from_302_location_header() -> None:
    publisher = _publisher()
    assert publisher._extract_shop_information_id("/client/shop_information/edit/98765") == "98765"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/client/shop_information/edit/123", "123"),
        ("https://app.istores.co.il/client/shop_information/edit/456", "456"),
        ("/client/shop_information/789/edit", "789"),
        ("/client/shop_information/edit?id=321", "321"),
    ],
)
def test_extract_id_from_redirect_formats(location: str, expected: str) -> None:
    publisher = _publisher()
    assert publisher._extract_shop_information_id(location) == expected


def test_extract_id_from_inertia_json_props() -> None:
    publisher = _publisher()

    class _Response:
        url = "https://app.istores.co.il/client/shop_information/create"

        @staticmethod
        def json() -> dict[str, object]:
            return {"props": {"shop_information": {"id": 777}}}

    assert publisher._extract_shop_information_id("", _Response()) == "777"


def test_dry_run_shows_payload_without_tokens_or_cookies() -> None:
    publisher = _publisher()
    out = publisher.publish(_Draft(), dry_run=True)
    text = str(out)
    assert out["dry_run"] is True
    assert "session=abc" not in text
    assert "token123" not in text
    assert out["headers"]["X-XSRF-TOKEN"] == "[REDACTED]"
    assert out["headers"]["Cookie"] == "[REDACTED]"


def test_from_settings_fails_without_admin_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.istore_blog_publisher.settings.istore_admin_base_url", "https://app.istores.co.il")
    monkeypatch.setattr("app.services.istore_blog_publisher.settings.istore_admin_cookie", None)
    monkeypatch.setattr("app.services.istore_blog_publisher.settings.istore_xsrf_token", "x")
    with pytest.raises(MissingIStoreAdminSettingsError):
        IStoreAdminShopInformationPublisher.from_settings()


def test_publish_fails_when_live_url_cannot_be_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = _publisher()

    class _CreateResponse:
        status_code = 302
        headers = {"Location": "/client/shop_information/edit/123"}

    class _PublicResponse:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text
            self.ok = status_code == 200

    monkeypatch.setattr(publisher.session, "post", lambda *a, **k: _CreateResponse())
    monkeypatch.setattr(
        "app.services.istore_blog_publisher.requests.get",
        lambda *a, **k: _PublicResponse(404, ""),
    )

    with pytest.raises(IStoreBlogPublishError, match="נוצר עמוד מידע ב-ISTORE אבל לא נמצא URL ציבורי מאומת"):
        publisher.publish(_Draft())


def test_create_fails_with_diagnostic_details_when_redirect_has_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = _publisher()

    class _CreateResponse:
        status_code = 302
        headers = {"Location": "/client/shop_information/edit"}
        url = "https://app.istores.co.il/client/shop_information/create"
        text = "ok"

        @staticmethod
        def json() -> dict[str, object]:
            return {}

    monkeypatch.setattr(publisher.session, "post", lambda *a, **k: _CreateResponse())

    with pytest.raises(IStoreBlogPublishError, match="status_code=302"):
        publisher._create_shop_information(publisher._build_payload(_Draft()))


def test_create_fails_with_clear_message_when_redirected_back_to_create_form(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = _publisher()

    class _CreateResponse:
        status_code = 302
        headers = {"Location": "https://app.istores.co.il/client/shop_information/create"}
        url = "https://app.istores.co.il/client/shop_information/create"
        text = "validation failed"

        @staticmethod
        def json() -> dict[str, object]:
            return {}

    monkeypatch.setattr(publisher.session, "post", lambda *a, **k: _CreateResponse())

    with pytest.raises(IStoreBlogPublishError, match="ISTORE rejected create request and redirected back to create form"):
        publisher._create_shop_information(publisher._build_payload(_Draft()))


def test_create_headers_include_browser_and_inertia_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = _publisher()
    monkeypatch.setattr("app.services.istore_blog_publisher.settings.istore_inertia_version", "abc123")
    headers = publisher._build_create_headers(sanitize=False)
    assert headers["Accept"] == "text/html, application/xhtml+xml"
    assert headers["X-Inertia"] == "true"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["X-Inertia-Version"] == "abc123"


def test_validate_create_payload_requires_contract_fields() -> None:
    publisher = _publisher()
    payload = publisher._build_payload(_Draft())
    publisher._validate_create_payload(payload)

    payload["descriptions"]["3"]["title"] = ""
    with pytest.raises(IStoreBlogPublishError, match=r"descriptions\[3\]\.title"):
        publisher._validate_create_payload(payload)
