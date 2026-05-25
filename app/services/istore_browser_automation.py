from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from re import search

from app.core.config import settings

CREATE_PAGE_URL = "https://app.istores.co.il/client/shop_information/create"
OTP_MESSAGE = "Manual OTP verification required in browser session."


@dataclass
class IStoreBrowserStatus:
    success: bool
    current_url: str
    logged_in: bool
    otp_required: bool
    can_access_create_page: bool
    title: str
    screenshot_saved: bool
    screenshot_path: str | None
    error: str | None
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class IStoreBrowserCreateResult:
    success: bool
    current_url: str
    external_content_id: str | None
    otp_required: bool
    error: str | None
    screenshot_path: str | None
    selector_availability: dict[str, object] | None = None
    planned_fields: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _bool_like(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_title(page: object) -> str:
    try:
        return str(page.title())
    except Exception:  # noqa: BLE001
        return ""


def _infer_state(current_url: str, title: str, content: str) -> tuple[bool, bool, bool]:
    lowered_url = current_url.lower()
    lowered_title = title.lower()
    lowered_content = content.lower()

    otp_required = any(
        marker in lowered_url or marker in lowered_title or marker in lowered_content
        for marker in ["otp", "verify", "verification code", "two-factor", "2fa", "one time password"]
    )

    on_create_page = "/client/shop_information/create" in lowered_url
    login_like = any(marker in lowered_url for marker in ["/login", "/signin", "auth"])

    can_access_create_page = on_create_page and not login_like and not otp_required
    logged_in = can_access_create_page or (not login_like and not otp_required and "app.istores.co.il" in lowered_url)

    return logged_in, otp_required, can_access_create_page


def check_istore_browser_status() -> IStoreBrowserStatus:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return IStoreBrowserStatus(
            success=False,
            current_url="",
            logged_in=False,
            otp_required=False,
            can_access_create_page=False,
            title="",
            screenshot_saved=False,
            screenshot_path=None,
            error=f"Playwright import failed: {exc}",
        )

    storage_state_path = Path(settings.istore_browser_storage_state_path)
    screenshot_path = storage_state_path.with_name("istore_browser_status.png")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=_bool_like(settings.istore_browser_headless, default=True),
                slow_mo=settings.istore_browser_slowmo_ms,
            )

            context_kwargs: dict[str, object] = {}
            if storage_state_path.exists():
                context_kwargs["storage_state"] = str(storage_state_path)

            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.set_default_timeout(settings.istore_browser_timeout_ms)
            page.goto(CREATE_PAGE_URL, wait_until="domcontentloaded")

            current_url = page.url or ""
            title = _safe_title(page)
            content = page.content()
            logged_in, otp_required, can_access_create_page = _infer_state(current_url=current_url, title=title, content=content)

            screenshot_saved = False
            saved_screenshot_path: str | None = None
            try:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_path), full_page=True)
                screenshot_saved = screenshot_path.exists()
                saved_screenshot_path = str(screenshot_path) if screenshot_saved else None
            except Exception:  # noqa: BLE001
                screenshot_saved = False
                saved_screenshot_path = None

            message = OTP_MESSAGE if otp_required else None

            if can_access_create_page:
                try:
                    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(storage_state_path))
                except Exception:
                    pass

            context.close()
            browser.close()

            return IStoreBrowserStatus(
                success=True,
                current_url=current_url,
                logged_in=logged_in,
                otp_required=otp_required,
                can_access_create_page=can_access_create_page,
                title=title,
                screenshot_saved=screenshot_saved,
                screenshot_path=saved_screenshot_path,
                error=None,
                message=message,
            )
    except PlaywrightError as exc:
        return IStoreBrowserStatus(
            success=False,
            current_url="",
            logged_in=False,
            otp_required=False,
            can_access_create_page=False,
            title="",
            screenshot_saved=False,
            screenshot_path=None,
            error=f"Playwright runtime error: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return IStoreBrowserStatus(
            success=False,
            current_url="",
            logged_in=False,
            otp_required=False,
            can_access_create_page=False,
            title="",
            screenshot_saved=False,
            screenshot_path=None,
            error=str(exc),
        )


def create_shop_information_page(payload: dict[str, object], dry_run: bool = True) -> IStoreBrowserCreateResult:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return IStoreBrowserCreateResult(False, "", None, False, f"Playwright import failed: {exc}", None)

    storage_state_path = Path(settings.istore_browser_storage_state_path)
    screenshot_path = storage_state_path.with_name("istore_browser_create_test.png")
    planned_fields = {
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "meta_title": str(payload.get("meta_title") or ""),
        "meta_description": str(payload.get("meta_description") or ""),
        "slug": str(payload.get("slug") or ""),
        "status": payload.get("status"),
        "is_blog": payload.get("is_blog"),
    }
    selector_matrix: dict[str, list[str]] = {
        "title": ["input[name='title']", "input#title", "input[name*='title']"],
        "description": ["textarea[name='description']", "textarea[name='body']", "[contenteditable='true']"],
        "meta_title": ["input[name='meta_title']", "input[name='metaTitle']", "input[name*='meta'][name*='title']"],
        "meta_description": ["textarea[name='meta_description']", "textarea[name='metaDescription']", "textarea[name*='meta'][name*='description']"],
        "slug": ["input[name='keyword']", "input[name='slug']", "input[name*='slug']", "input[name*='keyword']"],
        "status": ["select[name='status']", "input[name='status']", "[name='status']"],
        "is_blog": ["input[name='is_blog']", "select[name='is_blog']", "[name='is_blog']"],
        "submit": ["button[type='submit']", "button:has-text('Save')", "button:has-text('שמור')", "form button"],
    }

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=_bool_like(settings.istore_browser_headless, default=True),
                slow_mo=settings.istore_browser_slowmo_ms,
            )
            context_kwargs: dict[str, object] = {}
            if storage_state_path.exists():
                context_kwargs["storage_state"] = str(storage_state_path)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.set_default_timeout(settings.istore_browser_timeout_ms)
            page.goto(CREATE_PAGE_URL, wait_until="domcontentloaded")
            current_url = page.url or ""
            title = _safe_title(page)
            content = page.content()
            _, otp_required, can_access_create_page = _infer_state(current_url=current_url, title=title, content=content)
            if otp_required or not can_access_create_page:
                return IStoreBrowserCreateResult(
                    False,
                    current_url,
                    None,
                    otp_required,
                    OTP_MESSAGE if otp_required else "Browser not logged in or create page inaccessible.",
                    None,
                )

            selector_availability: dict[str, object] = {}
            for key, selectors in selector_matrix.items():
                available = None
                for selector in selectors:
                    if page.locator(selector).count() > 0:
                        available = selector
                        break
                selector_availability[key] = {"found": bool(available), "selector": available, "candidates": selectors}

            if dry_run:
                return IStoreBrowserCreateResult(True, page.url or "", None, False, None, None, selector_availability, planned_fields)

            def fill_with_value(field_key: str, value: str) -> None:
                selected = selector_availability.get(field_key, {})
                selector = selected.get("selector") if isinstance(selected, dict) else None
                if not selector or value == "":
                    return
                if selector == "[contenteditable='true']":
                    page.locator(selector).first.click()
                    page.locator(selector).first.fill(value)
                    return
                page.fill(selector, value)

            fill_with_value("title", planned_fields["title"])
            fill_with_value("description", planned_fields["description"])
            fill_with_value("meta_title", planned_fields["meta_title"])
            fill_with_value("meta_description", planned_fields["meta_description"])
            fill_with_value("slug", planned_fields["slug"])

            status_selector = selector_availability.get("status", {}).get("selector") if isinstance(selector_availability.get("status"), dict) else None
            if status_selector and planned_fields["status"] is not None:
                page.select_option(status_selector, str(planned_fields["status"]))
            is_blog_selector = selector_availability.get("is_blog", {}).get("selector") if isinstance(selector_availability.get("is_blog"), dict) else None
            if is_blog_selector and planned_fields["is_blog"] is not None:
                value = "1" if _bool_like(planned_fields["is_blog"], default=True) else "0"
                if is_blog_selector.startswith("select"):
                    page.select_option(is_blog_selector, value)
                else:
                    page.check(is_blog_selector) if value == "1" else page.uncheck(is_blog_selector)

            submit_selector = selector_availability.get("submit", {}).get("selector") if isinstance(selector_availability.get("submit"), dict) else None
            if submit_selector:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=settings.istore_browser_timeout_ms):
                    page.locator(submit_selector).first.click()
            else:
                page.evaluate("document.querySelector('form')?.submit()")
                page.wait_for_timeout(1500)

            current_url = page.url or ""
            matched = search(r"/client/shop_information/edit/(\d+)", current_url)
            external_content_id = matched.group(1) if matched else None

            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            context.storage_state(path=str(storage_state_path))

            return IStoreBrowserCreateResult(
                success=bool(external_content_id),
                current_url=current_url,
                external_content_id=external_content_id,
                otp_required=False,
                error=None if external_content_id else "Create did not redirect to edit page.",
                screenshot_path=str(screenshot_path) if screenshot_path.exists() else None,
                selector_availability=selector_availability,
                planned_fields=planned_fields,
            )
    except PlaywrightError as exc:
        return IStoreBrowserCreateResult(False, "", None, False, f"Playwright runtime error: {exc}", None)
    except Exception as exc:  # noqa: BLE001
        return IStoreBrowserCreateResult(False, "", None, False, str(exc), None)
