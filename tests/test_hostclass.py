"""Offline unit tests for the network classifier: no socket, no browser, no local server, except the
two tests that deliberately monkeypatch `socket.getaddrinfo` to prove the resolver path."""

import socket

import pytest

from scrapology import hostclass
from scrapology.hostclass import (
    LINK_LOCAL,
    LOOPBACK,
    PRIVATE,
    PUBLIC,
    UNRESOLVABLE,
    UNSPECIFIED,
    CachedClassifier,
    classify_address,
    classify_host,
    may_reach,
)

ADDRESS_CASES = [
    ("10.1.2.3", PRIVATE),
    ("172.16.0.1", PRIVATE),
    ("172.31.255.255", PRIVATE),
    ("172.32.0.1", PUBLIC),
    ("192.168.1.1", PRIVATE),
    ("127.0.0.1", LOOPBACK),
    ("127.0.0.2", LOOPBACK),
    ("169.254.169.254", LINK_LOCAL),
    ("100.64.0.1", PRIVATE),
    ("100.127.255.255", PRIVATE),
    ("100.128.0.1", PUBLIC),
    ("::1", LOOPBACK),
    ("fc00::1", PRIVATE),
    ("fd12::1", PRIVATE),
    ("fe80::1", LINK_LOCAL),
    ("0.0.0.0", UNSPECIFIED),
    ("::", UNSPECIFIED),
    ("8.8.8.8", PUBLIC),
    ("2606:4700::1111", PUBLIC),
    ("::ffff:10.0.0.1", PRIVATE),
]


@pytest.mark.parametrize("address, expected", ADDRESS_CASES)
def test_classify_address(address: str, expected: str) -> None:
    assert classify_address(address) == expected


@pytest.mark.parametrize("address, expected", ADDRESS_CASES)
def test_classify_host_on_an_ip_literal(address: str, expected: str) -> None:
    assert classify_host(address) == expected


def test_classify_address_rejects_a_name() -> None:
    with pytest.raises(ValueError):
        classify_address("localhost")


@pytest.mark.parametrize("host", ["localhost", "api.localhost", "LOCALHOST."])
def test_localhost_is_loopback_without_a_lookup(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("localhost must not be resolved")

    monkeypatch.setattr(hostclass.socket, "getaddrinfo", fail)
    assert classify_host(host) == LOOPBACK


def test_unresolvable_name_is_unresolvable() -> None:
    assert classify_host("nonexistent.invalid") == UNRESOLVABLE


def test_resolver_failure_is_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(hostclass.socket, "getaddrinfo", fail)
    assert classify_host("example.test") == UNRESOLVABLE


def test_a_name_resolving_to_several_classes_takes_the_least_public(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(name: str, port: object, *_args: object, **_kwargs: object):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]

    monkeypatch.setattr(hostclass.socket, "getaddrinfo", fake_getaddrinfo)
    assert classify_host("mixed.test") == PRIVATE


MAY_REACH_CASES = [
    # a public page reaches only public hosts
    (PUBLIC, PUBLIC, True),
    (PUBLIC, LOOPBACK, False),
    (PUBLIC, PRIVATE, False),
    (PUBLIC, LINK_LOCAL, False),
    (PUBLIC, UNSPECIFIED, False),
    (PUBLIC, UNRESOLVABLE, False),
    # a loopback page reaches loopback and public, not private
    (LOOPBACK, LOOPBACK, True),
    (LOOPBACK, PUBLIC, True),
    (LOOPBACK, PRIVATE, False),
    (LOOPBACK, LINK_LOCAL, False),
    (LOOPBACK, UNSPECIFIED, False),
    (LOOPBACK, UNRESOLVABLE, False),
    # a private-network page reaches its own class and public
    (PRIVATE, PRIVATE, True),
    (PRIVATE, PUBLIC, True),
    (PRIVATE, LOOPBACK, False),
    (PRIVATE, LINK_LOCAL, False),
    # a link-local page reaches its own class and public
    (LINK_LOCAL, LINK_LOCAL, True),
    (LINK_LOCAL, PUBLIC, True),
    (LINK_LOCAL, PRIVATE, False),
    # no page, whatever its own class, reaches unspecified or unresolvable
    (UNSPECIFIED, UNSPECIFIED, False),
    (UNSPECIFIED, PUBLIC, True),
    (PRIVATE, UNRESOLVABLE, False),
    (LOOPBACK, UNSPECIFIED, False),
]


@pytest.mark.parametrize("page_class, target_class, expected", MAY_REACH_CASES)
def test_may_reach(page_class: str, target_class: str, expected: bool) -> None:
    assert may_reach(page_class, target_class) is expected


def test_cached_classifier_resolves_each_host_once_case_insensitively() -> None:
    calls: list[str] = []

    def counting(host: str) -> str:
        calls.append(host)
        return PUBLIC

    cached = CachedClassifier(counting)
    assert cached("Example.test") == PUBLIC
    assert cached("example.test") == PUBLIC
    assert cached("EXAMPLE.TEST") == PUBLIC
    assert calls == ["Example.test"]


def test_cached_classifier_defaults_to_classify_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostclass, "classify_host", lambda h: "public" if h == "127.0.0.2" else PRIVATE)
    cached = CachedClassifier(hostclass.classify_host)
    assert cached("127.0.0.2") == PUBLIC
    assert cached("10.0.0.5") == PRIVATE
