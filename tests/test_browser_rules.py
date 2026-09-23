"""Offline unit tests for the CORS-mode header rule in `browser.cross_origin_refusal`: no Playwright
import, no browser, no local server. `tests/test_localserver.py` covers the same rule end to end
through a real Chromium (the `origin` header Chromium actually sends cannot be proven here, only the
handler's own logic against it)."""

from dataclasses import dataclass, field

from scrapology.browser import _Interceptor


@dataclass
class _FakeRequest:
    """Duck-typed stand-in for a Playwright Request: `cross_origin_refusal` only reads `.headers`
    and `.resource_type` (the `.url` argument is passed separately, as the handler itself does)."""

    resource_type: str = "fetch"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _FakeFrame:
    """Duck-typed stand-in for a Playwright Frame: has `.url` but no `.evaluate`, so `frame_origin`'s
    live-evaluation step fails fast (an `AttributeError`, caught the same as a real evaluation failure
    on a frame that is navigating away) and falls back to the URL walk."""

    url: str


def _interceptor() -> _Interceptor:
    # Constructor signature: page, guard, throttle, max_bytes, classifier, page_url.
    return _Interceptor(
        page=None,
        guard=None,
        throttle=None,
        max_bytes=1,
        classifier=lambda h: "loopback",
        page_url="http://127.0.0.1/",
    )


def test_origin_header_present_and_different_is_refused() -> None:
    interceptor = _interceptor()
    request = _FakeRequest(headers={"origin": "https://a.test"})
    reason = interceptor.cross_origin_refusal(request, "https://b.test/x", frame=None)
    assert reason is not None and reason[0] == "cross-origin script request"


def test_origin_header_present_and_equal_is_allowed() -> None:
    interceptor = _interceptor()
    request = _FakeRequest(headers={"origin": "https://a.test"})
    assert interceptor.cross_origin_refusal(request, "https://a.test/x", frame=None) is None


def test_origin_header_null_is_refused_as_opaque() -> None:
    interceptor = _interceptor()
    request = _FakeRequest(headers={"origin": "null"})
    reason = interceptor.cross_origin_refusal(request, "https://a.test/x", frame=None)
    assert reason is not None and "opaque origin" in reason[1]


def test_no_header_no_cors_resource_type_is_allowed() -> None:
    interceptor = _interceptor()
    request = _FakeRequest(resource_type="image", headers={})
    assert interceptor.cross_origin_refusal(request, "https://b.test/pic.png", frame=None) is None


def test_no_header_fetch_resource_type_falls_back_to_frame_origin_same_origin() -> None:
    interceptor = _interceptor()
    request = _FakeRequest(resource_type="fetch", headers={})
    same_frame = _FakeFrame(url="https://a.test/page")
    assert interceptor.cross_origin_refusal(request, "https://a.test/data", frame=same_frame) is None


def test_no_header_fetch_resource_type_falls_back_to_frame_origin_cross_origin() -> None:
    interceptor = _interceptor()
    request = _FakeRequest(resource_type="fetch", headers={})
    cross_frame = _FakeFrame(url="https://b.test/page")
    reason = interceptor.cross_origin_refusal(request, "https://a.test/data", frame=cross_frame)
    assert reason is not None and reason[0] == "cross-origin script request"
