import pytest

from scrapology import detect

LONG = "word " * 2000


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_block_statuses(status: int) -> None:
    reason = detect.block_reason(status, "<html>anything</html>", "anything")
    assert reason is not None and str(status) in reason


@pytest.mark.parametrize("status", [200, 201, 301, 404, 500, 502, None])
def test_non_block_statuses_without_markers(status: int | None) -> None:
    assert detect.block_reason(status, "<html><p>plain page</p></html>", "plain page") is None


@pytest.mark.parametrize(
    "snippet",
    [
        "<title>Just a moment...</title>",
        '<div id="cf-chl-widget"></div>',
        "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script>",
        "Please verify you are human",
        "Enter the captcha to continue",
        "Access Denied",
        "Request unsuccessful. Incapsula incident ID",
        "awswaf-token",
        "Our systems have detected unusual traffic",
        "Pardon Our Interruption",
    ],
)
def test_challenge_markers(snippet: str) -> None:
    html = f"<html><body>{snippet}</body></html>"
    assert detect.challenge_marker(html, "") is not None
    assert detect.block_reason(200, html, "") is not None


def test_marker_inside_a_long_article_is_not_a_block() -> None:
    html = f"<html><body><p>{LONG} We discuss the history of the captcha.</p></body></html>"
    assert detect.challenge_marker(html, LONG) is None
    assert detect.block_reason(200, html, LONG) is None


def test_short_page_is_a_warning_not_a_block() -> None:
    text = "This domain is for use in illustrative examples in documents."
    assert detect.block_reason(200, f"<html><body><p>{text}</p></body></html>", text) is None
    warning = detect.short_page_warning(text)
    assert warning is not None and "short page" in warning
    assert detect.short_page_warning(LONG) is None


def test_needs_render_only_for_thin_scripted_pages() -> None:
    thin = "a few words"
    assert detect.needs_render("<html><script src='a.js'></script></html>", thin)
    assert not detect.needs_render("<html><body><p>a few words</p></body></html>", thin)
    assert not detect.needs_render("<html><script src='a.js'></script></html>", LONG)
    assert detect.is_thin("") and not detect.is_thin(LONG)
