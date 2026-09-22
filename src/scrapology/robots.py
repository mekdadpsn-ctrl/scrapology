"""robots.txt, always obeyed, no override. Matching follows RFC 9309 and is implemented here.

The standard library's `urllib.robotparser` ignores the `*` and `$` operators, so `Disallow: /private*`
would let `/private-area/` through. This module implements the RFC 9309 rules itself:

- group selection by product token (case-insensitive), falling back to the `*` group;
- `*` matches any run of characters and a trailing `$` anchors the end of the path;
- the rule with the longest matching pattern wins, and Allow wins a tie;
- percent-encoding is normalised before comparison on both sides;
- the URL's query string is part of the compared path.

Fetch outcomes, following the RFC's guidance:
- 2xx: parse and apply.
- 4xx other than 429: no robots.txt, everything is allowed.
- 429, 5xx or a network error: the host cannot state its rules, so it is treated as disallowed for this run.
- Crawl-delay for our agent (or `*`) raises the per-host delay, capped at 60 seconds.

robots.txt governs web resources: pages, PDFs and landing pages a browser or a plain GET would read.
The documented REST APIs this tool calls (Europe PMC, CELLAR, Unpaywall) are used under their own terms.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from scrapology import transport
from scrapology.ratelimit import HostRateLimiter

AGENT_TOKEN = "Scrapology"
CRAWL_DELAY_CAP = 60.0
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = "0123456789abcdefABCDEF"


def product_token(value: str) -> str:
    """`Scrapology/0.1 (+url)` and `scrapology` both give `scrapology`."""
    head = value.strip().split("/", 1)[0].split(None, 1)
    return head[0].lower() if head else ""


def normalise_path(text: str) -> str:
    """Percent-encoding normalisation for comparison: decode percent-encoded unreserved characters,
    upper-case the hex digits of every other escape, and percent-encode non-ASCII, space and control
    characters as UTF-8 octets. `*` and `$` pass through untouched."""
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "%" and i + 2 < length and text[i + 1] in _HEX and text[i + 2] in _HEX:
            pair = text[i + 1 : i + 3]
            decoded = chr(int(pair, 16))
            out.append(decoded if decoded in UNRESERVED else "%" + pair.upper())
            i += 3
            continue
        code = ord(ch)
        if code < 33 or code > 126:
            out.extend(f"%{octet:02X}" for octet in ch.encode())
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "^" + ".*".join(re.escape(piece) for piece in body.split("*")) + ("$" if anchored else "")
    return re.compile(regex, re.S)


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def comparison_path(url: str) -> str:
    """The part of a URL that robots.txt rules are matched against: path plus query, normalised."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return normalise_path(path)


class RobotsRules:
    """A parsed robots.txt."""

    def __init__(self, groups: list[Group]) -> None:
        self.groups = groups

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        groups: list[Group] = []
        current: Group | None = None
        collecting_agents = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            name, _, value = line.partition(":")
            name = name.strip().lower()
            value = value.strip()
            if name == "user-agent":
                if current is None or not collecting_agents:
                    current = Group()
                    groups.append(current)
                current.agents.append(product_token(value) or "*")
                collecting_agents = True
            elif name in ("allow", "disallow"):
                collecting_agents = False
                if current is None or not value:
                    continue
                pattern = normalise_path(value)
                if not pattern.startswith(("/", "*")):
                    pattern = "/" + pattern
                current.rules.append(Rule(name == "allow", pattern, compile_pattern(pattern)))
            elif name == "crawl-delay":
                collecting_agents = False
                if current is None:
                    continue
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    continue
        return cls(groups)

    def select(self, agent: str) -> tuple[list[Rule], float | None]:
        """The rules and Crawl-delay that apply to `agent`: its own groups merged, else the `*` groups."""
        token = product_token(agent)
        chosen = [g for g in self.groups if token in g.agents]
        if not chosen:
            chosen = [g for g in self.groups if "*" in g.agents]
        rules = [rule for group in chosen for rule in group.rules]
        delay = next((g.crawl_delay for g in chosen if g.crawl_delay is not None), None)
        return rules, delay

    def can_fetch(self, agent: str, url: str) -> bool:
        rules, _ = self.select(agent)
        path = comparison_path(url)
        best: Rule | None = None
        for rule in rules:
            if not rule.regex.match(path):
                continue
            if best is None or len(rule.pattern) > len(best.pattern):
                best = rule
            elif len(rule.pattern) == len(best.pattern) and rule.allow:
                best = rule
        return True if best is None else best.allow

    def crawl_delay(self, agent: str) -> float | None:
        _, delay = self.select(agent)
        return delay


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    reason: str
    crawl_delay: float | None = None
    note: str | None = None


@dataclass
class _HostRules:
    rules: RobotsRules | None
    status: str  # "parsed", "absent", "unavailable"
    detail: str


Fetcher = Callable[[str], transport.Response]


class RobotsPolicy:
    """Caches one robots.txt per scheme and host for the life of the policy."""

    def __init__(
        self,
        agent: str = AGENT_TOKEN,
        timeout: float = 30,
        limiter: HostRateLimiter | None = None,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.agent = agent
        self.timeout = timeout
        self.limiter = limiter
        self._fetcher = fetcher
        self._hosts: dict[str, _HostRules] = {}

    def _fetch(self, url: str) -> transport.Response:
        if self._fetcher is not None:
            return self._fetcher(url)
        return transport.get(url, accept="text/plain,*/*;q=0.8", timeout=self.timeout, limiter=self.limiter)

    def _rules_for(self, url: str) -> _HostRules:
        parts = urlsplit(url)
        key = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        cached = self._hosts.get(key)
        if cached is not None:
            return cached
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            response = self._fetch(robots_url)
        except transport.TransportError as err:
            detail = f"robots.txt could not be fetched ({err}); host treated as disallowed"
            entry = _HostRules(None, "unavailable", detail)
        else:
            if response.ok:
                rules = RobotsRules.parse(response.text())
                entry = _HostRules(rules, "parsed", f"robots.txt read (HTTP {response.status})")
            elif response.status == 429 or response.status >= 500:
                detail = f"robots.txt answered HTTP {response.status}; host treated as disallowed for this run"
                entry = _HostRules(None, "unavailable", detail)
            elif 400 <= response.status < 500:
                entry = _HostRules(None, "absent", f"no robots.txt (HTTP {response.status}); everything allowed")
            else:
                detail = f"robots.txt answered HTTP {response.status}; host treated as disallowed for this run"
                entry = _HostRules(None, "unavailable", detail)
        self._hosts[key] = entry
        return entry

    def check(self, url: str) -> RobotsDecision:
        """Whether `url` may be fetched, plus the Crawl-delay (capped) the host asks for."""
        entry = self._rules_for(url)
        if entry.status == "absent":
            return RobotsDecision(True, entry.detail)
        if entry.status == "unavailable" or entry.rules is None:
            return RobotsDecision(False, entry.detail)
        delay = entry.rules.crawl_delay(self.agent)
        note = None
        if delay is not None and delay > CRAWL_DELAY_CAP:
            note = f"robots.txt Crawl-delay {delay:g} s capped at {CRAWL_DELAY_CAP:g} s"
            delay = CRAWL_DELAY_CAP
        if delay is not None and delay < 0:
            delay = None
        if not entry.rules.can_fetch(self.agent, url):
            return RobotsDecision(False, f"disallowed by robots.txt for agent {self.agent}", delay, note)
        return RobotsDecision(True, "allowed by robots.txt", delay, note)

    def guard(self, url: str) -> str | None:
        """For redirect and navigation guards: the reason `url` may not be fetched, or None when it may."""
        decision = self.check(url)
        if decision.crawl_delay and self.limiter is not None:
            self.limiter.set_host_delay(url, decision.crawl_delay)
        return None if decision.allowed else decision.reason
