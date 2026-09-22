import io

import pytest

from scrapology import transport, unpaywall
from scrapology._version import __version__

EMAIL = "someone" + chr(64) + "example.org"  # built at run time so no literal address sits in the repository


def test_user_agent_is_honest() -> None:
    assert transport.USER_AGENT.startswith(f"Scrapology/{__version__} (+https://github.com/")


def _sequence(*items):
    """A fake `_single_get` that plays the items in order: an int status, a (status, headers) pair, or an
    exception to raise. Records the URLs and headers it was asked for."""
    queue = list(items)
    seen: list[tuple[str, dict[str, str]]] = []

    def single(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> transport.Response:
        seen.append((url, dict(headers)))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, extra = (item, {}) if isinstance(item, int) else item
        response_headers = {"content-type": "text/html; charset=utf-8", **extra}
        return transport.Response(status, url, b"body", response_headers)

    single.seen = seen  # type: ignore[attr-defined]
    return single


def _install(monkeypatch: pytest.MonkeyPatch, *items):
    single = _sequence(*items)
    monkeypatch.setattr(transport, "_single_get", single)
    return single


def test_retries_once_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, transport.NetworkError("reset"), 200)
    paused: list[float] = []
    response = transport.get("https://h.test/", sleep=paused.append)
    assert response.status == 200 and len(single.seen) == 2 and paused == [transport.RETRY_PAUSE]


def test_gives_up_after_second_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, transport.NetworkError("a"), transport.NetworkError("b"))
    with pytest.raises(transport.NetworkError):
        transport.get("https://h.test/", sleep=lambda _: None)


def test_retries_once_on_a_plain_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, 502, 200)
    response = transport.get("https://h.test/", sleep=lambda _: None)
    assert response.status == 200 and len(single.seen) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_never_retries_a_refusal(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    single = _install(monkeypatch, status, 200)
    response = transport.get("https://h.test/", sleep=lambda _: None)
    assert response.status == status and len(single.seen) == 1


def test_never_retries_a_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, 404, 200)
    response = transport.get("https://h.test/", sleep=lambda _: None)
    assert response.status == 404 and len(single.seen) == 1


def test_headers_carry_agent_accept_and_language(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, 200)
    transport.get("https://h.test/", accept="application/xhtml+xml", accept_language="eng")
    headers = single.seen[0][1]
    assert headers["User-Agent"] == transport.USER_AGENT
    assert headers["Accept"] == "application/xhtml+xml" and headers["Accept-Language"] == "eng"


# ---------- redirects ----------


def test_redirect_followed_with_guard_and_history(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, (302, {"location": "/next"}), (301, {"location": "https://other.test/end"}), 200)
    checked: list[str] = []

    def guard(url: str) -> str | None:
        checked.append(url)
        return None

    response = transport.get("https://h.test/start", guard=guard)
    assert response.status == 200 and response.url == "https://other.test/end"
    assert [u for u, _ in single.seen] == ["https://h.test/start", "https://h.test/next", "https://other.test/end"]
    assert checked == ["https://h.test/next", "https://other.test/end"]
    assert response.history == ["https://h.test/start", "https://h.test/next"]


def test_redirect_refused_by_guard_is_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, (302, {"location": "/secret?token=abc"}), 200)
    with pytest.raises(transport.RedirectBlocked) as exc:
        transport.get("https://h.test/open", guard=lambda url: "disallowed by robots.txt" if "secret" in url else None)
    assert [u for u, _ in single.seen] == ["https://h.test/open"]
    assert exc.value.url == "https://h.test/secret" and "token" not in str(exc.value)
    assert "disallowed by robots.txt" in str(exc.value)


def test_redirect_to_ftp_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, (302, {"location": "ftp://files.test/pub/x"}), 200)
    with pytest.raises(transport.RedirectError) as exc:
        transport.get("https://h.test/open")
    assert len(single.seen) == 1 and "ftp" in str(exc.value)


def test_initial_scheme_must_be_http_or_https(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, 200)
    with pytest.raises(transport.RedirectError):
        transport.get("ftp://files.test/pub/x")
    with pytest.raises(transport.RedirectError):
        transport.get("file:///etc/hosts")
    assert single.seen == []


def test_too_many_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, *([(302, {"location": "/again"})] * 10))
    with pytest.raises(transport.RedirectError) as exc:
        transport.get("https://h.test/loop")
    assert "more than 5 redirects" in str(exc.value)


def test_3xx_without_location_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, 301)
    assert transport.get("https://h.test/x").status == 301


# ---------- size cap ----------


def test_read_capped_stops_past_the_limit() -> None:
    stream = io.BytesIO(b"x" * 3000)
    with pytest.raises(transport.ResponseTooLarge) as exc:
        transport._read_capped(stream, 2000, "https://h.test/big?secret=1")
    assert "2000 byte limit" in str(exc.value) and "secret" not in str(exc.value)
    assert transport._read_capped(io.BytesIO(b"y" * 1500), 2000, "https://h.test/ok") == b"y" * 1500


def test_oversized_body_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    single = _install(monkeypatch, transport.ResponseTooLarge("too big"), 200)
    with pytest.raises(transport.ResponseTooLarge):
        transport.get("https://h.test/big", sleep=lambda _: None)
    assert len(single.seen) == 1


# ---------- redaction ----------


def test_error_messages_carry_no_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> transport.Response:
        raise transport.NetworkError(f"{transport.redact(url)}: OSError: reset")

    monkeypatch.setattr(transport, "_single_get", broken)
    with pytest.raises(transport.NetworkError) as exc:
        transport.get(f"https://api.test/v2/10.1/x?email={EMAIL}", sleep=lambda _: None)
    assert EMAIL not in str(exc.value) and "email=" not in str(exc.value)


def test_single_get_wraps_socket_errors_without_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def open(self, request, timeout):
            raise OSError("connection reset")

    monkeypatch.setattr(transport, "_OPENER", Boom())
    with pytest.raises(transport.NetworkError) as exc:
        transport._single_get(f"https://api.test/v2/x?email={EMAIL}", {}, 5, 1000)
    assert EMAIL not in str(exc.value) and "connection reset" in str(exc.value)


def test_unpaywall_lookup_never_leaks_the_email(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_get(url: str, **_: object) -> transport.Response:
        seen.append(url)
        raise transport.NetworkError(f"{url}: OSError: reset")  # deliberately unredacted, worst case

    monkeypatch.setattr(transport, "get", fake_get)
    with pytest.raises(transport.NetworkError) as exc:
        unpaywall.lookup("10.1/x", EMAIL)
    assert seen and "email=" in seen[0]  # the address did go to Unpaywall
    message = str(exc.value)
    assert EMAIL not in message and "email=" not in message
    assert unpaywall.lookup_url("10.1/x") in message


def test_redact() -> None:
    assert transport.redact("https://h.test/p?x=1#f") == "https://h.test/p"
    assert transport.redact("https://h.test") == "https://h.test"


def test_response_text_decoding() -> None:
    latin = transport.Response(200, "u", "caf\xe9".encode("latin-1"), {"content-type": "text/html; charset=iso-8859-1"})
    assert latin.text() == "caf\xe9"
    broken = transport.Response(200, "u", b"\xff\xfe\x00bad", {"content-type": "text/html; charset=nonsense"})
    assert isinstance(broken.text(), str)
    assert transport.Response(204, "u", b"").ok and not transport.Response(404, "u", b"").ok
