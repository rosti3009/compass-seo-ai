from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

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
