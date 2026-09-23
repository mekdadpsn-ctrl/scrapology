"""Rendering with Playwright, plainly: a fresh headless browser, the honest User-Agent, no stealth.

Browser order: Playwright's bundled Chromium, then the installed Chrome (`channel="chrome"`), then
Edge (`channel="msedge"`). The fallback exists because Playwright's own `install` deletes any bundled
build that no registered Playwright copy claims, so a machine with several Playwright installs can
lose the bundled build overnight. `scrapology doctor` reports which browser will run.

Navigation guard, applied through `context.route` to every navigation of every frame and of every
page in the context (popups included), with service workers blocked:

- Every request the browser wants to send, navigation or not, in any frame or page, is checked
  against the scheme allow-list and the caller's guard (robots.txt) before it goes out. A refused
  subresource (script, style, image, XHR or fetch call, prefetch, beacon, on any host) is aborted
  and listed in the warning; robots.txt is cached per host, so a third-party host costs one
  robots.txt request. The guard is never run on a non-http(s) URL. Subresource redirects are chased
  the same way as navigation redirects: the handler fetches each hop itself with `route.fetch`, so a
  hop to a disallowed URL is refused before it is requested, and the final response is handed to the
  browser under the original URL.
- Pages may only make GET and HEAD requests: a POST fetch, a sendBeacon call, or a page-driven
  navigation (a script-submitted form, in any frame) using another method is refused. The user's own
  first navigation is always a plain GET, so this rule only ever catches something the loaded page
  does.
- Allowed subresources are fetched by this handler, not by the browser, and the response is replayed
  to it, which bypasses the browser's own cross-origin checks unless they are reapplied here: a
  cross-origin request the page makes in CORS mode is refused. CORS mode is read from the `origin`
  request header, which Playwright 1.62 does carry at route time for exactly those requests: `fetch`,
  XHR and EventSource calls, module scripts, web fonts, and any resource requested with the
  `crossorigin` attribute or as a `<link rel=preload>` with `crossorigin`. An `origin` value of `null`
  means the request came from an opaque origin, a sandboxed frame or a `data:`/`srcdoc` document, and
  is refused the same way. `sec-fetch-mode` is checked first for a future Playwright that exposes it;
  `resource_type in ("fetch", "xhr", "eventsource")` is the fallback for the one case that carries
  neither header, a same-origin `fetch`/XHR/EventSource, which still needs comparing (and passes,
  being same-origin). A plain cross-origin image, classic script, stylesheet or same-origin preload
  without `crossorigin`, none of which the browser sends in CORS mode, is not affected by this rule;
  it still goes through the robots and network rules.
- A page on a public address may not make the browser reach a loopback, private (RFC 1918), CGNAT,
  link-local (which includes cloud metadata services) or unspecified address; a page that is itself on
  such an address may reach its own class of network and the public internet. The host is classified
  once per render (`scrapology.hostclass`), and this check runs before robots.txt, so a refused host
  never even receives a robots.txt request. Known limit: the name is resolved once per render, so a
  DNS rebinding between that lookup and the browser's own connection to the resolved address is not
  caught.
- WebSockets are refused: every WebSocket the page opens is closed without connecting to the server
  and listed in the warning.
- Browsers follow server redirects internally, where no handler sees them, so the handler performs
  the navigation itself with `route.fetch(max_redirects=0)` and chases the chain hop by hop under the
  guard, the throttle and the 5-hop cap. When the chain ends, the browser is sent to the final URL,
  so `page.url` and the document base are right. Cost: the final page is read by the handler and
  then loaded by the browser, one request more than a plain browser would make for that page.
- A refusal during the first navigation (the URL itself or a hop) aborts it: the render ends as
  blocked with nothing kept. A refusal after the first document has loaded (a meta refresh, a script
  navigation, an iframe, a popup) is answered with an empty 204, which leaves the loaded page in
  place; the page is kept and the refusal is reported as a warning naming the refused URL.
- Popups are never followed. Subresources are not individually throttled: only page navigations
  are, because the per-host delay is a delay between page requests.
- Bodies the handler reads are capped at `max_bytes` before they reach the browser.
"""

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from scrapology import hostclass
from scrapology.hostclass import PUBLIC, UNRESOLVABLE, CachedClassifier, may_reach
from scrapology.transport import (
    ALLOWED_SCHEMES,
    DEFAULT_MAX_BYTES,
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    USER_AGENT,
    redact,
)

BROWSERS: tuple[tuple[str | None, str], ...] = (
    (None, "bundled Chromium"),
    ("chrome", "Chrome"),
    ("msedge", "Edge"),
)
_MISSING_MARKERS = ("Executable doesn't exist", "is not found", "distribution", "missing dependencies")
NETWORK_IDLE_CAP_MS = 10000
REFUSED_LIST_CAP = 10
# Fixed order the warning lists refusal groups in, regardless of the order they happened.
REFUSAL_GROUP_ORDER = ("robots.txt", "http(s) rule", "method", "cross-origin script request", "private network")

Guard = Callable[[str], str | None]
Throttle = Callable[[str], Any]


class RenderError(Exception):
    """The page could not be rendered (navigation error, timeout, browser crash, oversized page)."""


class BrowserUnavailable(RenderError):
    """None of the bundled Chromium, Chrome or Edge could be launched."""


class RenderBlocked(Exception):
    """The first navigation (or one of its redirect hops) was refused by the guard. Nothing was kept."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = redact(url)
        self.reason = reason
        super().__init__(f"navigation to {self.url} refused: {reason}")


@dataclass
class RenderResult:
    html: str
    url: str
    status: int | None
    browser: str
    history: list[str]
    warning: str | None = None


_pipe_filter_installed = False


def install_pipe_filter() -> None:
    """On Windows, asyncio prints "ValueError: I/O operation on closed pipe" when a browser subprocess is
    torn down. It is noise, not an error. This filters exactly that message and nothing else."""
    global _pipe_filter_installed
    if _pipe_filter_installed:
        return
    previous = sys.unraisablehook

    def quiet_closed_pipe(unraisable: Any) -> None:
        if isinstance(unraisable.exc_value, ValueError) and "closed pipe" in str(unraisable.exc_value):
            return
        previous(unraisable)

    sys.unraisablehook = quiet_closed_pipe
    _pipe_filter_installed = True


def is_missing_browser(err: BaseException) -> bool:
    text = str(err)
    return any(marker in text for marker in _MISSING_MARKERS)


def _first_line(err: BaseException) -> str:
    for line in str(err).splitlines():
        if line.strip():
            return line.strip()[:300]
    return type(err).__name__


def _is_web(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ALLOWED_SCHEMES


def origin_of(url: str) -> tuple[str, str, int] | None:
    """(scheme, lower-cased hostname, port) of a web URL, the scheme's default port when none is given.
    None for anything that is not http(s): the scheme allow-list refuses those, not this check."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None
    host = parts.hostname
    if not host:
        return None
    default_port = 443 if scheme == "https" else 80
    return (scheme, host.lower(), parts.port or default_port)


def frame_origin(
    frame: Any, cache: dict[int, tuple[str, str, int] | None] | None = None
) -> tuple[str, str, int] | None:
    """The origin of `frame` itself when it can be read live: `window.origin`, which also catches an
    opaque origin (a sandboxed frame without `allow-same-origin`, or a `data:`/`srcdoc` document both
    report the literal string "null"). When the evaluation fails (the frame is navigating away, for
    one) or returns something unreadable, falls back to the origin of the nearest ancestor frame that
    has a web URL, walking `parent_frame` past `about:blank` and `about:srcdoc` frames. None when the
    frame is opaque, no ancestor has a web URL, or the frame tree cannot be read (a popup's first
    navigation, for one). `cache`, keyed by `id(frame)`, is a per-render cache the caller keeps so a
    page making many same-origin fetches from one frame evaluates it once. Only called for the header-
    absent fallback path, so the evaluation (a page round-trip) runs rarely."""
    key = id(frame) if cache is not None else None
    if key is not None and key in cache:
        return cache[key]
    result = _frame_origin_uncached(frame)
    if key is not None:
        cache[key] = result
    return result


def _frame_origin_uncached(frame: Any) -> tuple[str, str, int] | None:
    try:
        value = frame.evaluate("window.origin")
    except Exception:
        pass
    else:
        if value == "null":
            return None  # opaque origin
        origin = origin_of(value)
        if origin is not None:
            return origin
        # an unreadable value: fall through to the URL walk below
    current = frame
    for _hop in range(20):  # a real frame tree is shallow; this only guards a broken frame chain
        if current is None:
            return None
        try:
            origin = origin_of(current.url)
        except Exception:
            return None
        if origin is not None:
            return origin
        try:
            current = current.parent_frame
        except Exception:
            return None
    return None


class _Interceptor:
    """The route handler's state for one render: what was refused, what failed, what navigated."""

    def __init__(
        self,
        page: Any,
        guard: Guard | None,
        throttle: Throttle | None,
        max_bytes: int,
        classifier: CachedClassifier,
        page_url: str,
    ) -> None:
        self.page = page
        self.guard = guard
        self.throttle = throttle
        self.max_bytes = max_bytes
        self.classifier = classifier
        page_class = classifier(urlsplit(page_url).hostname or "")
        # unresolvable counts as public, the strictest class for deciding what this render may reach.
        self.page_class = PUBLIC if page_class == UNRESOLVABLE else page_class
        self.refused_initial: list[tuple[str, str]] = []
        self.refused_later: list[tuple[str, str]] = []
        self.refusals: dict[str, list[str]] = {}  # group -> capped display list, for the warning
        self.refusal_counts: dict[str, int] = {}  # group -> unique count
        self.refused_urls: set[tuple[str, str]] = set()  # (group, redacted url), dedupe
        self.dropped_requests: list[str] = []  # subresources dropped: too many redirects or over the size limit
        self.refused_websockets: list[str] = []
        self.websocket_routes: list[Any] = []
        self.failures: list[str] = []
        self.main_navigations: list[str] = []
        self.throttled: set[str] = set()
        self.document_body: bytes | None = None  # the served body of the last main-frame document
        self.document_url: str | None = None
        self._frame_origins: dict[int, tuple[str, str, int] | None] = {}  # frame_origin cache, per render

    def refusal(self, target: str, *, skip_network: bool = False) -> tuple[str, str] | None:
        """(group, detail) for a request that may not go out: the scheme allow-list, then, unless
        `skip_network` (true only for the user's own first URL), the private-network rule, then
        robots.txt. None when the request may proceed."""
        scheme = urlsplit(target).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            return "http(s) rule", f"only http and https URLs are fetched ({scheme or 'schemeless'} refused)"
        if not skip_network:
            reason = self.network_refusal(target)
            if reason:
                return reason
        if self.guard is not None:
            detail = self.guard(target)
            if detail:
                return "robots.txt", detail
        return None

    def network_refusal(self, target: str) -> tuple[str, str] | None:
        """Refuses a request that would take this render from its own class of network to a more private
        one. The host is resolved once per render by the cached classifier; a DNS rebinding between that
        lookup and the browser's own connection to the resolved address is not caught here."""
        host = urlsplit(target).hostname or ""
        target_class = self.classifier(host)
        if may_reach(self.page_class, target_class):
            return None
        shown = host or "(no host)"
        return "private network", f"{target_class} address {shown} refused from a {self.page_class} page"

    def cross_origin_refusal(self, request: Any, target: str, frame: Any) -> tuple[str, str] | None:
        """Refuses a cross-origin request the page makes in CORS mode: `fetch`, XHR and EventSource
        calls, module scripts, web fonts, and anything requested with the `crossorigin` attribute or
        as a `<link rel=preload>` with `crossorigin`. Mode is read from the `origin` request header,
        which Playwright 1.62 does carry at route time for exactly those requests; `sec-fetch-mode` is
        checked first for a future Playwright that exposes it, and `resource_type` stands in only for
        the one case that carries neither header, a same-origin `fetch`/XHR/EventSource (which still
        needs comparing, and passes, being same-origin). An `origin` value of `null` marks a request
        from an opaque origin, a sandboxed frame or a `data:`/`srcdoc` document, and is refused the
        same way a real cross-origin mismatch is. A no-cors subresource (plain image, classic script,
        stylesheet, same-origin preload without `crossorigin`) carries none of these signals and is
        left to the robots and network rules instead."""
        mode_header = request.headers.get("sec-fetch-mode")
        origin_header = request.headers.get("origin")
        cors_mode = (
            mode_header == "cors"
            or origin_header is not None
            or request.resource_type in ("fetch", "xhr", "eventsource")
        )
        if not cors_mode:
            return None
        target_origin = origin_of(target)
        if target_origin is None:
            return None  # not a web URL; the scheme rule refuses it
        if origin_header == "null":
            return (
                "cross-origin script request",
                f"{redact(target)} requested from an opaque origin (sandboxed frame or data: document)",
            )
        if origin_header is not None:
            source_origin = origin_of(origin_header)  # None here means malformed: falls through to refuse below
        elif frame is None:
            return "cross-origin script request", f"{redact(target)}: initiating frame unknown"
        else:
            source_origin = frame_origin(frame, self._frame_origins)
        if source_origin is None or source_origin != target_origin:
            return "cross-origin script request", f"{redact(target)} is cross-origin from the requesting page"
        return None

    def wait_turn(self, target: str) -> None:
        """Per-host delay before a page request, once per URL within a render."""
        if self.throttle is not None and target not in self.throttled:
            self.throttled.add(target)
            self.throttle(target)

    def refuse_request(self, route: Any, target: str, group: str, detail: str) -> None:
        """A subresource or page-driven navigation the page asked for but may not have: aborted, grouped
        and deduplicated for the warning."""
        key = (group, redact(target))
        if key not in self.refused_urls:
            self.refused_urls.add(key)
            self.refusal_counts[group] = self.refusal_counts.get(group, 0) + 1
            display = self.refusals.setdefault(group, [])
            if len(display) < REFUSED_LIST_CAP:
                display.append(redact(target))
        route.abort("blockedbyclient")

    def drop_request(self, route: Any, target: str, why: str) -> None:
        """A subresource that cannot be served under the rules (hop cap, size cap): aborted, noted."""
        key = f"{redact(target)} ({why})"
        if key not in self.dropped_requests and len(self.dropped_requests) < REFUSED_LIST_CAP:
            self.dropped_requests.append(key)
        route.abort("failed")

    def handle_websocket(self, ws_route: Any) -> None:
        """A routed WebSocket is never connected to the server unless the handler asks for it, and this
        handler never does. Closing it from inside the handler deadlocks the sync API, so the route is
        kept and closed from the render loop once the page has loaded."""
        url = redact(getattr(ws_route, "url", "") or "")
        if url and url not in self.refused_websockets and len(self.refused_websockets) < REFUSED_LIST_CAP:
            self.refused_websockets.append(url)
        self.websocket_routes.append(ws_route)

    def close_websockets(self) -> None:
        for ws_route in self.websocket_routes:
            try:
                ws_route.close(code=1008, reason="WebSockets are not followed")
            except Exception:
                pass
        self.websocket_routes.clear()

    def warning(self) -> str | None:
        parts: list[str] = []
        if self.refused_later:
            first_url, first_reason = self.refused_later[0]
            text = f"navigation to {redact(first_url)} refused ({first_reason}); the page loaded before it is kept"
            if len(self.refused_later) > 1:
                text += f"; {len(self.refused_later) - 1} more navigation(s) refused"
            parts.append(text)
        if self.refusal_counts:
            total = sum(self.refusal_counts.values())
            clauses: list[str] = []
            for group in REFUSAL_GROUP_ORDER:
                count = self.refusal_counts.get(group, 0)
                if not count:
                    continue
                listed = self.refusals.get(group, [])
                clause = f"{group}: {', '.join(listed)}"
                more = count - len(listed)
                if more:
                    clause += f" and {more} more"
                clauses.append(clause)
            parts.append(f"{total} request(s) refused, " + "; ".join(clauses))
        if self.dropped_requests:
            parts.append(f"{len(self.dropped_requests)} request(s) dropped: {', '.join(self.dropped_requests)}")
        if self.refused_websockets:
            parts.append(f"{len(self.refused_websockets)} WebSocket(s) refused: {', '.join(self.refused_websockets)}")
        return "; ".join(parts) or None

    def handle_subresource(self, route: Any, request: Any) -> None:
        """A non-navigation request: refused, dropped, or fetched hop by hop and handed to the browser."""
        from playwright import sync_api

        target = request.url
        if request.method not in ("GET", "HEAD"):
            self.refuse_request(
                route, target, "method", f"{request.method} not allowed; pages may only make GET and HEAD requests"
            )
            return
        try:
            frame = request.frame
        except Exception:
            frame = None
        reason = self.cross_origin_refusal(request, target, frame)
        if reason:
            self.refuse_request(route, target, *reason)
            return
        reason = self.refusal(target)
        if reason:
            self.refuse_request(route, target, *reason)
            return
        current = target
        try:
            response = route.fetch(max_redirects=0)
            hops = 0
            while response.status in REDIRECT_STATUSES and response.headers.get("location"):
                hops += 1
                if hops > MAX_REDIRECTS:
                    self.drop_request(route, target, f"more than {MAX_REDIRECTS} redirects")
                    return
                next_url = urljoin(current, response.headers["location"].strip())
                reason = self.cross_origin_refusal(request, next_url, frame)
                if reason:
                    self.refuse_request(route, next_url, *reason)
                    return
                reason = self.refusal(next_url)
                if reason:
                    self.refuse_request(route, next_url, *reason)
                    return
                current = next_url
                response = route.fetch(url=next_url, max_redirects=0)
            if len(response.body()) > self.max_bytes:
                self.drop_request(route, current, f"over the {self.max_bytes} byte limit")
                return
            route.fulfill(response=response)
        except sync_api.Error:
            try:  # a subresource the browser could not get is the page's problem, not the render's
                route.abort("failed")
            except sync_api.Error:
                pass

    def _refuse(self, route: Any, target: str, reason: str, initial: bool) -> None:
        if initial:
            self.refused_initial.append((target, reason))
            route.abort("blockedbyclient")
        else:
            self.refused_later.append((target, reason))
            route.fulfill(status=204, body="")  # a 204 answer to a navigation leaves the current page in place

    def handle(self, route: Any) -> None:
        from playwright import sync_api

        request = route.request
        try:
            is_navigation = request.is_navigation_request()
        except Exception:  # cannot tell: treat it as a navigation, never let it through unguarded
            is_navigation = True
        if not is_navigation:
            self.handle_subresource(route, request)
            return
        try:
            frame = request.frame
            owner = frame.page
            is_main = frame == owner.main_frame
        except Exception:
            # Playwright raises here for a popup's first navigation: the request is issued before the new
            # page exists. That is a popup by definition.
            owner, is_main = None, False
        target = request.url
        kind = "popup" if owner is not self.page else ("page" if is_main else "frame")
        initial = kind == "page" and not self.main_navigations
        if kind == "popup":
            self._refuse(route, target, "popups are not followed", initial)
            return
        if not initial and request.method not in ("GET", "HEAD"):
            detail = f"{request.method} not allowed; pages may only make GET and HEAD requests"
            self._refuse(route, target, f"method: {detail}", initial=False)
            return
        reason = self.refusal(target, skip_network=initial)
        if reason:
            self._refuse(route, target, f"{reason[0]}: {reason[1]}", initial)
            return
        if not initial:
            self.wait_turn(target)  # the first navigation was throttled by the caller before the render
        if kind == "page":
            self.main_navigations.append(target)
        current = target
        try:
            response = route.fetch(max_redirects=0)
            hops = 0
            while response.status in REDIRECT_STATUSES and response.headers.get("location"):
                hops += 1
                if hops > MAX_REDIRECTS:
                    self.failures.append(f"{redact(target)}: more than {MAX_REDIRECTS} redirects")
                    route.abort("failed")
                    return
                next_url = urljoin(current, response.headers["location"].strip())
                reason = self.refusal(next_url)  # every hop, first navigation's included, faces the network rule
                if reason:
                    self._refuse(route, next_url, f"{reason[0]}: {reason[1]}", initial)
                    return
                self.wait_turn(next_url)
                if kind == "page":
                    self.main_navigations.append(next_url)
                current = next_url
                response = route.fetch(url=next_url, max_redirects=0)
            body = response.body()
            if len(body) > self.max_bytes:
                self.failures.append(f"{redact(current)}: response exceeds the {self.max_bytes} byte limit")
                route.abort("failed")
                return
            if kind == "page":
                self.document_body, self.document_url = body, current
            if current == target:
                route.fulfill(response=response)
            else:
                # The browser now loads `current` itself. That request is its internal redirect follow, which
                # no handler sees; every hop and the final URL were checked and throttled above.
                route.fulfill(status=302, headers={"location": current})
        except sync_api.Error as err:
            self.failures.append(f"{redact(current)}: {_first_line(err)}")
            try:
                route.abort("failed")
            except sync_api.Error:
                pass


def _scheme_reason(url: str) -> str:
    scheme = urlsplit(url).scheme.lower()
    return f"only http and https URLs are fetched ({scheme or 'schemeless'} refused)"


def _content_or_empty(page: Any) -> str:
    try:
        return page.content()
    except Exception:  # the document may be mid-navigation; the later snapshot will cover it
        return ""


def _wait_for_load(page: Any, timeout: float, label: str, sync_api: Any) -> None:
    """Wait until the document has loaded, by polling `document.readyState`. A navigation refused with a 204
    while the document was still loading leaves Playwright's own load lifecycle without a signal although the
    document itself completes, so the document's readiness is what is waited for, not the lifecycle event."""
    deadline = max(1.0, timeout)
    try:
        page.wait_for_function("document.readyState === 'complete'", timeout=deadline * 1000, polling=100)
        return
    except sync_api.TimeoutError:
        pass
    except sync_api.Error as err:
        raise RenderError(f"{label}: {_first_line(err)}") from err
    try:
        state = page.evaluate("document.readyState")
    except sync_api.Error as err:
        raise RenderError(f"{label}: {_first_line(err)}") from err
    if state not in ("interactive", "complete"):
        raise RenderError(f"timeout after {deadline:g} s loading {redact(page.url)} ({label})")


def render(
    url: str,
    timeout: float = 60,
    user_agent: str = USER_AGENT,
    *,
    guard: Guard | None = None,
    throttle: Throttle | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    classifier: hostclass.Classifier | None = None,
) -> RenderResult:
    """Open `url` in the first browser that launches and return the rendered DOM.

    Raises RenderBlocked when the guard refuses the first navigation or one of its hops, BrowserUnavailable
    when no browser launches, and RenderError for everything else. Never raises anything else."""
    install_pipe_filter()
    try:
        return _render(url, timeout, user_agent, guard, throttle, max_bytes, classifier)
    except (RenderError, RenderBlocked):
        raise
    except Exception as err:  # Playwright errors, a running asyncio loop, anything unexpected
        raise RenderError(f"browser: {_first_line(err)}") from err


def _render(
    url: str,
    timeout: float,
    user_agent: str,
    guard: Guard | None,
    throttle: Throttle | None,
    max_bytes: int,
    classifier: hostclass.Classifier | None = None,
) -> RenderResult:
    from playwright import sync_api

    # Looked up on the module object, not imported by name, so a test can monkeypatch
    # `hostclass.classify_host` and have it take effect here.
    cached_classifier = CachedClassifier(classifier or hostclass.classify_host)
    last_missing: BaseException | None = None
    with sync_api.sync_playwright() as playwright:
        for channel, label in BROWSERS:
            options: dict[str, Any] = {"headless": True}
            if channel:
                options["channel"] = channel
            try:
                browser = playwright.chromium.launch(**options)
            except sync_api.Error as err:
                if is_missing_browser(err):
                    last_missing = err
                    continue
                raise RenderError(f"{label}: {_first_line(err)}") from err
            try:
                context = browser.new_context(user_agent=user_agent, service_workers="block")
                page = context.new_page()
                interceptor = _Interceptor(page, guard, throttle, max_bytes, cached_classifier, url)
                context.route("**/*", interceptor.handle)
                context.route_web_socket("**/*", interceptor.handle_websocket)
                try:
                    response = page.goto(url, wait_until="commit", timeout=timeout * 1000)
                except sync_api.Error as err:
                    if interceptor.refused_initial:
                        raise RenderBlocked(*interceptor.refused_initial[0]) from None
                    if interceptor.failures:
                        raise RenderError(f"{label}: {interceptor.failures[0]}") from None
                    if isinstance(err, sync_api.TimeoutError):
                        raise RenderError(f"timeout after {timeout:g} s loading {redact(url)} ({label})") from err
                    raise RenderError(f"{label}: {_first_line(err)}") from err
                if interceptor.refused_initial:
                    raise RenderBlocked(*interceptor.refused_initial[0])
                if interceptor.failures:
                    raise RenderError(f"{label}: {interceptor.failures[0]}")
                # The committed document is the allowed page; keep a copy in case a navigation the handler
                # cannot see (a blob: URL, for example) moves the browser off it before load completes.
                committed_url = page.url
                committed_html = _content_or_empty(page)
                _wait_for_load(page, timeout, label, sync_api)
                if interceptor.failures:
                    raise RenderError(f"{label}: {interceptor.failures[0]}")
                if _is_web(page.url):
                    loaded_url, snapshot = page.url, page.content()
                else:
                    loaded_url, snapshot = committed_url, committed_html
                    interceptor.refused_later.append((page.url, _scheme_reason(page.url)))
                try:
                    page.wait_for_load_state("networkidle", timeout=min(NETWORK_IDLE_CAP_MS, timeout * 1000))
                except sync_api.TimeoutError:
                    pass
                if interceptor.failures:
                    raise RenderError(f"{label}: {interceptor.failures[0]}")
                interceptor.close_websockets()
                final_url = page.url
                if _is_web(final_url):
                    reason = guard(final_url) if guard is not None else None
                    if reason:
                        raise RenderBlocked(final_url, reason)
                    html = page.content()
                else:
                    # The browser moved off the allowed page (an error page after a refused navigation, or a
                    # non-web URL such as blob:). Keep the allowed page: its rendered DOM if that was captured,
                    # else the DOM at commit, else the document as the server sent it.
                    served = interceptor.document_body.decode(errors="replace") if interceptor.document_body else ""
                    kept = snapshot or committed_html or served
                    if not _is_web(loaded_url) or not kept:
                        raise RenderError(f"{label}: the browser ended on {redact(final_url)}")
                    html, final_url = kept, loaded_url
                    if not any(u == page.url for u, _ in interceptor.refused_later):
                        interceptor.refused_later.append((page.url, _scheme_reason(page.url)))
                if len(html.encode()) > max_bytes:
                    raise RenderError(f"rendered page exceeds the {max_bytes} byte limit")
                warning = interceptor.warning()
                status = response.status if response is not None else None
                history = list(interceptor.main_navigations[:-1])
                return RenderResult(
                    html=html, url=final_url, status=status, browser=label, history=history, warning=warning
                )
            finally:
                browser.close()
    detail = _first_line(last_missing) if last_missing else "no launch attempted"
    raise BrowserUnavailable(f"no usable browser (bundled Chromium, Chrome, Edge): {detail}")


def probe_browsers(timeout: float = 30) -> list[tuple[str, bool, str]]:
    """Try to launch each browser in order. Returns (label, launched, detail) for each."""
    install_pipe_filter()
    from playwright import sync_api

    results: list[tuple[str, bool, str]] = []
    try:
        with sync_api.sync_playwright() as playwright:
            for channel, label in BROWSERS:
                options: dict[str, Any] = {"headless": True, "timeout": timeout * 1000}
                if channel:
                    options["channel"] = channel
                try:
                    browser = playwright.chromium.launch(**options)
                except sync_api.Error as err:
                    results.append((label, False, _first_line(err)))
                    continue
                try:
                    results.append((label, True, f"version {browser.version}"))
                finally:
                    browser.close()
    except Exception as err:
        results.append(("playwright", False, _first_line(err)))
    return results


def driver_package_dir() -> str:
    import playwright

    return os.path.join(os.path.dirname(os.path.abspath(playwright.__file__)), "driver", "package")


def browsers_path() -> str:
    """Where Playwright keeps its downloaded browsers on this machine."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        return os.path.join(driver_package_dir(), ".local-browsers")
    if configured:
        return configured
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ms-playwright")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "ms-playwright")


def bundled_status() -> dict[str, Any]:
    """What the installed Playwright expects and what is on disk.

    `registered` says whether this Playwright copy has a claim file in `<browsers path>/.links`. Without
    one, a `playwright install` run by any other Playwright copy on the machine treats the builds as
    orphans and deletes them. The fix is `python -m playwright install chromium` from this interpreter."""
    package_dir = driver_package_dir()
    base = browsers_path()
    expected: dict[str, str] = {}
    try:
        with open(os.path.join(package_dir, "browsers.json"), encoding="utf-8") as handle:
            for entry in json.load(handle).get("browsers", []):
                if entry.get("name") in ("chromium", "chromium-headless-shell"):
                    expected[entry["name"]] = str(entry.get("revision"))
    except (OSError, ValueError, KeyError):
        pass
    present: dict[str, bool] = {}
    for name, revision in expected.items():
        folder = os.path.join(base, f"{name.replace('-', '_')}-{revision}")
        present[name] = os.path.exists(os.path.join(folder, "INSTALLATION_COMPLETE"))
    registered = False
    links_dir = os.path.join(base, ".links")
    if os.path.isdir(links_dir):
        wanted = os.path.normcase(os.path.normpath(package_dir))
        for link in os.listdir(links_dir):
            try:
                with open(os.path.join(links_dir, link), encoding="utf-8", errors="replace") as handle:
                    claimed = os.path.normcase(os.path.normpath(handle.read().strip()))
            except OSError:
                continue
            if claimed == wanted:
                registered = True
                break
    return {
        "browsers_path": base,
        "links_path": links_dir,
        "package_dir": package_dir,
        "expected": expected,
        "present": present,
        "registered": registered,
    }
