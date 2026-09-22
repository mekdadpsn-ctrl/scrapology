"""Shared fixtures: a fake network and fake engines so the offline suite never opens a socket or a browser."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from scrapology import browser, transport
from scrapology.session import Session

LONG_TEXT = "This sentence is here to give the page enough main text to count as a real article. " * 30

ARTICLE_HTML = f"""<!doctype html><html><head><title>A real article</title>
<meta name="author" content="Jane Doe"><meta property="article:published_time" content="2024-05-01">
<meta property="og:title" content="A real article"><meta property="og:type" content="article">
</head><body><nav>Home</nav><main><h1>A real article</h1><p>{LONG_TEXT}</p>
<h2>Second part</h2><p>{LONG_TEXT}</p></main></body></html>"""

STUB_HTML = """<!doctype html><html><head><title>Stub</title></head>
<body><h1>Stub</h1><p>Only a few words live here.</p></body></html>"""

SCRIPTED_STUB_HTML = """<!doctype html><html><head><title>App</title><script src="/app.js"></script></head>
<body><div id="root"></div><noscript>Loading requires JavaScript.</noscript></body></html>"""

CHALLENGE_HTML = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><div id="challenge-platform">Checking your browser before accessing the site.</div></body></html>"""


def response(url: str, status: int = 200, body: bytes | str = b"", content_type: str = "text/html; charset=utf-8"):
    if isinstance(body, str):
        body = body.encode()
    return transport.Response(status, url, body, {"content-type": content_type})


@dataclass
class FakeNet:
    """URL to Response (or a callable returning one, or an exception to raise). Replaces transport.get,
    so redirects are not simulated here; the transport and local-server suites cover those."""

    routes: dict[str, object] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    default_status: int = 404

    def add(self, url: str, status: int = 200, body: bytes | str = b"", content_type: str = "text/html"):
        self.routes[url] = response(url, status, body, content_type)

    def robots(self, origin: str, text: str | None = "User-agent: *\nDisallow:\n", status: int = 200):
        url = origin.rstrip("/") + "/robots.txt"
        self.add(url, status, text or "", "text/plain")

    def get(self, url: str, **_: object) -> transport.Response:
        self.calls.append(url)
        hit = self.routes.get(url)
        if hit is None:
            for prefix, value in self.routes.items():
                if prefix.endswith("*") and url.startswith(prefix[:-1]):
                    hit = value
                    break
        if hit is None:
            return response(url, self.default_status, b"not found")
        if isinstance(hit, Exception):
            raise hit
        if callable(hit):
            return hit(url)
        return hit


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> FakeNet:
    fake = FakeNet()
    monkeypatch.setattr(transport, "get", fake.get)
    return fake


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any attempt to render is recorded and fails as 'no usable browser'."""
    attempts: list[str] = []

    def render(url: str, timeout: float = 60, user_agent: str = "", **_: Any) -> browser.RenderResult:
        attempts.append(url)
        raise browser.BrowserUnavailable("no usable browser (test)")

    monkeypatch.setattr(browser, "render", render)
    return attempts


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Install a fake renderer that returns `html` with `status` at `final_url`; returns the rendered URLs.
    The fake applies the guard to `final_url` the way the real renderer does."""

    def install(
        html: str, status: int | None = 200, final_url: str | None = None, warning: str | None = None
    ) -> list[str]:
        rendered: list[str] = []

        def render(url: str, timeout: float = 60, user_agent: str = "", **kwargs: Any) -> browser.RenderResult:
            rendered.append(url)
            landed = final_url or url
            guard = kwargs.get("guard")
            if guard is not None:
                reason = guard(landed)
                if reason:
                    raise browser.RenderBlocked(landed, reason)
            history = [url] if landed != url else []
            return browser.RenderResult(
                html=html, url=landed, status=status, browser="fake browser", history=history, warning=warning
            )

        monkeypatch.setattr(browser, "render", render)
        return rendered

    return install


@pytest.fixture
def session() -> Session:
    return Session(delay=0, timeout=5)
