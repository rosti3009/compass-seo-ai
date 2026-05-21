from __future__ import annotations

import pytest

from app.services.istore_blog_publisher import IStoreBlogPublisher, IStoreBlogPublishError


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _put(self, path: str, payload: dict) -> dict[str, object]:
        self.calls.append((path, payload))
        return {"ok": True, "path": path}


class _Draft:
    title = "T1"
    slug = "slug1"
    article_body = "<p>x</p>"
    meta_title = "M1"
    meta_description = "D"
    target_url = "https://compassgrill.co.il/blog/slug1"
    target_site_section = "blog"
    target_publish_type = "article"
    target_path = "/blog/slug1"
    featured_image_url = None
    image_alt_text = ""
    image_title = ""
    image_caption = ""
    image_filename_slug = ""

    def to_dict(self) -> dict[str, object]:
        return {"internal_links": []}


def test_publish_success_with_live_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = IStoreBlogPublisher(client=_FakeClient())

    class _Resp:
        status_code = 200
        ok = True
        text = "<html><title>T1</title><meta property='og:title' content='M1'/></html>"

    monkeypatch.setattr("app.services.istore_blog_publisher.requests.get", lambda *a, **k: _Resp())
    out = publisher.publish(_Draft())
    assert out["verification"]["status_code"] == 200


def test_publish_fails_when_live_url_404(monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = IStoreBlogPublisher(client=_FakeClient())

    class _Resp:
        status_code = 404
        ok = False
        text = ""

    monkeypatch.setattr("app.services.istore_blog_publisher.requests.get", lambda *a, **k: _Resp())
    with pytest.raises(IStoreBlogPublishError):
        publisher.publish(_Draft())
