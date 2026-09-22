"""A session: the per-host rate limiter and the robots.txt cache that every request shares.

One session per batch, or one per process for library use, so the per-host delay holds however many
targets point at the same host and robots.txt is read once per host.
"""

from dataclasses import dataclass, field

from scrapology.ratelimit import HostRateLimiter
from scrapology.robots import RobotsPolicy

ROBOTS_TIMEOUT_CAP = 30.0


@dataclass
class Session:
    delay: float = 2.0
    timeout: float = 60
    limiter: HostRateLimiter = field(init=False)
    robots: RobotsPolicy = field(init=False)

    def __post_init__(self) -> None:
        if self.delay < 0:
            raise ValueError("delay must be zero or positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.limiter = HostRateLimiter(self.delay)
        self.robots = RobotsPolicy(timeout=min(self.timeout, ROBOTS_TIMEOUT_CAP), limiter=self.limiter)

    def configure(self, delay: float, timeout: float) -> None:
        """Apply a later call's delay and timeout to the shared session."""
        if delay < 0:
            raise ValueError("delay must be zero or positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.delay = float(delay)
        self.timeout = float(timeout)
        self.limiter.delay = float(delay)
        self.robots.timeout = min(float(timeout), ROBOTS_TIMEOUT_CAP)


_default: Session | None = None


def default_session(delay: float = 2.0, timeout: float = 60) -> Session:
    """The process-wide session, created on first use and reconfigured on every later call."""
    global _default
    if _default is None:
        _default = Session(delay=delay, timeout=timeout)
    else:
        _default.configure(delay, timeout)
    return _default
