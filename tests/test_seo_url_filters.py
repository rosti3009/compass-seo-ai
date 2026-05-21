from app.services.seo_url_filters import get_url_exclusion_reason, is_seo_eligible_url, is_system_url


def test_blog_index_is_not_treated_as_system_or_excluded() -> None:
    url = "https://example.com/blog/"
    assert is_system_url(url) is False
    assert is_seo_eligible_url(url) is True
    assert get_url_exclusion_reason(url) is None


def test_blog_article_slug_is_not_treated_as_system_or_excluded() -> None:
    url = "https://example.com/blog/cart-optimization-guide"
    assert is_system_url(url) is False
    assert is_seo_eligible_url(url) is True
    assert get_url_exclusion_reason(url) is None
