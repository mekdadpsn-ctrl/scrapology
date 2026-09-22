"""Per-host minimum delay between requests.

One limiter is shared across a batch and across library calls in the same process, so the delay
holds however many targets point at the same host. A robots.txt Crawl-delay raises the delay for
that host; it never lowers it below the configured minimum.
"""

import time
from collections.abc import Callable
from urllib.parse import urlsplit


def host_of(url_or_host: str) -> str:
    """Lower-cased host part of a URL, or the string itself when it holds no scheme."""
    if "://" in url_or_host:
        return urlsplit(url_or_host).netloc.lower()
    return url_or_host.lower()


class HostRateLimiter:
    def __init__(
        self,
        delay: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if delay < 0:
            raise ValueError("delay must be zero or positive")
        self.delay = float(delay)
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}

    def set_host_delay(self, url_or_host: str, delay: float | None) -> None:
        """Apply a Crawl-delay for one host. Only ever raises the delay above the default."""
        if delay is None:
            return
        host = host_of(url_or_host)
        self._host_delay[host] = max(self.delay, float(delay))

    def delay_for(self, url_or_host: str) -> float:
        return self._host_delay.get(host_of(url_or_host), self.delay)

    def wait(self, url_or_host: str) -> float:
        """Block until the host's delay has passed since its last request. Returns the seconds waited."""
        host = host_of(url_or_host)
        waited = 0.0
        last = self._last.get(host)
        if last is not None:
            due = last + self.delay_for(host)
            now = self._clock()
            if due > now:
                waited = due - now
                self._sleep(waited)
        self._last[host] = self._clock()
        return waited
