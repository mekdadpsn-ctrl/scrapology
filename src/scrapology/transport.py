"""Plain HTTP GET on the standard library: honest User-Agent, guarded redirects, capped size, one retry.

Rules:
- Only http and https, for the first request and for every redirect hop.
- Redirects are followed by hand, at most 5 hops, and every hop is passed to the caller's guard
  (robots.txt) before it is requested. A refused hop raises RedirectBlocked and nothing from the
  refused page is read.
- Bodies are read in chunks and abandoned past `max_bytes` (50 MB by default).
- One retry after a short pause on a network error or a 5xx that is not a refusal. 401, 403, 429
  and 503 are the site's decision and are never retried; no 4xx is retried.
- Error messages carry URLs without their query string, so nothing passed as a parameter (for
  example the Unpaywall email address) can reach a receipt or the console.
"""

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from scrapology import detect
from scrapology._version import __version__
from scrapology.ratelimit import HostRateLimiter

USER_AGENT = f"Scrapology/{__version__} (+https://github.com/mekdadpsn-ctrl/scrapology)"
RETRY_PAUSE = 2.0
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
CHUNK = 1 << 20
ALLOWED_SCHEMES = frozenset({"http", "https"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

Guard = Callable[[str], str | None]


class TransportError(Exception):
    """No usable answer: network failure, oversized body, bad redirect, or a refused redirect."""


class NetworkError(TransportError):
    """The request could not complete (DNS, connection, TLS, timeout, malformed URL), even after the retry."""


class ResponseTooLarge(TransportError):
    """The body exceeded `max_bytes`."""


class RedirectError(TransportError):
    """Too many hops, or a hop to a scheme other than http or https."""


class RedirectBlocked(TransportError):
    """The guard refused a redirect hop (robots.txt). Nothing from the refused URL was read."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = redact(url)
        self.reason = reason
        super().__init__(f"redirect to {self.url} refused: {reason}")


def redact(url: str) -> str:
    """The URL without its query string and fragment, for messages and receipts."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


@dataclass
class Response:
    """What came back. `status` and `url` describe the final response; `history` lists the URLs redirected from."""

    status: int
    url: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    def charset(self, default: str = "utf-8") -> str:
        ctype = self.content_type
        if "charset=" in ctype:
            value = ctype.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"')
            if value:
                return value
        return default

    def text(self) -> str:
        for enc in (self.charset(), "utf-8", "latin-1"):
            try:
                return self.body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode(errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand every 3xx back as a response so the caller decides what to do with it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _read_capped(stream: object, max_bytes: int, url: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read = getattr(stream, "read")
    while True:
        chunk = read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(f"{redact(url)}: response exceeds the {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _single_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response:
    try:
        request = urllib.request.Request(url, headers=headers)
        with _OPENER.open(request, timeout=timeout) as reply:
            body = _read_capped(reply, max_bytes, url)
            return Response(reply.status, reply.geturl(), body, {k.lower(): v for k, v in reply.headers.items()})
    except urllib.error.HTTPError as err:
        try:
            body = _read_capped(err, max_bytes, url) if err.fp is not None else b""
        except ResponseTooLarge:
            raise
        except (OSError, http.client.HTTPException):
            body = b""
        return Response(err.code, err.geturl() or url, body, {k.lower(): v for k, v in err.headers.items()})
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as err:
        raise NetworkError(f"{redact(url)}: {type(err).__name__}: {_short(err)}") from err


def _short(err: BaseException) -> str:
    text = str(err).splitlines()[0] if str(err) else ""
    return text[:200]


def _attempt(
    url: str,
    headers: dict[str, str],
    timeout: float,
    limiter: HostRateLimiter | None,
    retry: bool,
    sleep: Callable[[float], None],
    max_bytes: int,
) -> Response:
    attempts = 2 if retry else 1
    last_error: NetworkError | None = None
    for attempt in range(1, attempts + 1):
        if limiter is not None:
            limiter.wait(url)
        try:
            response = _single_get(url, headers, timeout, max_bytes)
        except NetworkError as err:
            last_error = err
            if attempt < attempts:
                sleep(RETRY_PAUSE)
            continue
        retryable = response.status >= 500 and response.status not in detect.BLOCK_STATUSES
        if retryable and attempt < attempts:
            sleep(RETRY_PAUSE)
            continue
        return response
    assert last_error is not None
    raise last_error


def get(
    url: str,
    *,
    accept: str | None = None,
    accept_language: str | None = None,
    timeout: float = 60,
    limiter: HostRateLimiter | None = None,
    retry: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    guard: Guard | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> Response:
    """GET `url`, following up to `max_redirects` hops, each one checked by `guard` first.

    Returns a Response for any final HTTP status. Raises a TransportError subclass otherwise."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise RedirectError(f"{redact(url)}: only http and https URLs are fetched")
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if accept_language:
        headers["Accept-Language"] = accept_language
    history: list[str] = []
    current = url
    for _hop in range(max_redirects + 1):
        response = _attempt(current, headers, timeout, limiter, retry, sleep, max_bytes)
        location = response.headers.get("location")
        if response.status not in REDIRECT_STATUSES or not location:
            response.history = history
            return response
        next_url = urllib.parse.urljoin(current, location.strip())
        next_scheme = urlsplit(next_url).scheme.lower()
        if next_scheme not in ALLOWED_SCHEMES:
            raise RedirectError(
                f"{redact(current)}: redirect to a {next_scheme or 'schemeless'} URL not followed ({redact(next_url)})"
            )
        if guard is not None:
            reason = guard(next_url)
            if reason:
                raise RedirectBlocked(next_url, reason)
        history.append(current)
        current = next_url
    raise RedirectError(f"{redact(url)}: more than {max_redirects} redirects")
