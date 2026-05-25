from app.services.istore_browser_automation import _infer_state


def test_infer_state_create_page_accessible() -> None:
    logged_in, otp_required, can_access_create_page = _infer_state(
        current_url="https://app.istores.co.il/client/shop_information/create",
        title="Create Shop Information",
        content="<html><body>Editor</body></html>",
    )

    assert logged_in is True
    assert otp_required is False
    assert can_access_create_page is True


def test_infer_state_detects_otp() -> None:
    logged_in, otp_required, can_access_create_page = _infer_state(
        current_url="https://app.istores.co.il/auth/otp",
        title="OTP Verification",
        content="Please enter your verification code",
    )

    assert logged_in is False
    assert otp_required is True
    assert can_access_create_page is False


def test_infer_state_detects_login_page() -> None:
    logged_in, otp_required, can_access_create_page = _infer_state(
        current_url="https://app.istores.co.il/login",
        title="Sign In",
        content="",
    )

    assert logged_in is False
    assert otp_required is False
    assert can_access_create_page is False
