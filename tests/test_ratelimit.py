import pytest

from scrapology.ratelimit import HostRateLimiter, host_of


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make(delay: float = 2.0) -> tuple[HostRateLimiter, FakeClock]:
    clock = FakeClock()
    return HostRateLimiter(delay=delay, clock=clock, sleep=clock.sleep), clock


def test_first_request_does_not_wait() -> None:
    limiter, clock = make()
    assert limiter.wait("https://a.test/x") == 0.0
    assert clock.slept == []


def test_second_request_to_same_host_waits_the_remaining_delay() -> None:
    limiter, clock = make(2.0)
    limiter.wait("https://a.test/x")
    clock.now += 0.5
    waited = limiter.wait("https://a.test/y")
    assert waited == pytest.approx(1.5)
    assert clock.slept == [pytest.approx(1.5)]


def test_no_wait_once_delay_has_passed() -> None:
    limiter, clock = make(2.0)
    limiter.wait("https://a.test/x")
    clock.now += 3
    assert limiter.wait("https://a.test/y") == 0.0


def test_hosts_are_independent() -> None:
    limiter, clock = make(2.0)
    limiter.wait("https://a.test/x")
    assert limiter.wait("https://b.test/x") == 0.0
    assert clock.slept == []


def test_crawl_delay_raises_but_never_lowers() -> None:
    limiter, clock = make(2.0)
    limiter.set_host_delay("https://slow.test/", 5)
    limiter.set_host_delay("https://fast.test/", 0.1)
    assert limiter.delay_for("https://slow.test/a") == 5.0
    assert limiter.delay_for("https://fast.test/a") == 2.0
    limiter.set_host_delay("https://slow.test/", None)
    assert limiter.delay_for("https://slow.test/a") == 5.0
    limiter.wait("https://slow.test/a")
    limiter.wait("https://slow.test/b")
    assert clock.slept == [pytest.approx(5.0)]


def test_zero_delay_never_sleeps() -> None:
    limiter, clock = make(0)
    for _ in range(3):
        limiter.wait("https://a.test/x")
    assert clock.slept == []


def test_negative_delay_rejected() -> None:
    with pytest.raises(ValueError):
        HostRateLimiter(delay=-1)


def test_host_of() -> None:
    assert host_of("https://A.Test:8443/path") == "a.test:8443"
    assert host_of("plain.host") == "plain.host"
