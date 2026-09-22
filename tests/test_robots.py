import pytest

from scrapology import transport
from scrapology.robots import (
    CRAWL_DELAY_CAP,
    RobotsPolicy,
    RobotsRules,
    comparison_path,
    normalise_path,
    product_token,
)

WILDCARDS = """
User-agent: *
Disallow: /private*
Disallow: /*.pdf$
Disallow: /tmp/
Allow: /tmp/public
Disallow: /search
Allow: /search/about
"""


def allowed(text: str, url: str, agent: str = "Scrapology") -> bool:
    return RobotsRules.parse(text).can_fetch(agent, url)


def test_star_wildcard_blocks_prefix_variants() -> None:
    assert not allowed(WILDCARDS, "https://h.test/private")
    assert not allowed(WILDCARDS, "https://h.test/private-area/")
    assert not allowed(WILDCARDS, "https://h.test/private/x/y")
    assert allowed(WILDCARDS, "https://h.test/privat")
    assert allowed(WILDCARDS, "https://h.test/public/private")


def test_dollar_anchors_the_end() -> None:
    assert not allowed(WILDCARDS, "https://h.test/x/file.pdf")
    assert not allowed(WILDCARDS, "https://h.test/file.pdf")
    assert allowed(WILDCARDS, "https://h.test/x/file.pdf.html")
    assert allowed(WILDCARDS, "https://h.test/x/file.pdf?dl=1")  # the query is part of the compared path


def test_longest_match_wins_and_allow_wins_ties() -> None:
    assert not allowed(WILDCARDS, "https://h.test/tmp/private")
    assert allowed(WILDCARDS, "https://h.test/tmp/public/file")
    assert not allowed(WILDCARDS, "https://h.test/search?q=x")
    assert allowed(WILDCARDS, "https://h.test/search/about")
    tie = "User-agent: *\nDisallow: /page\nAllow: /page\n"
    assert allowed(tie, "https://h.test/page")
    tie_reversed = "User-agent: *\nAllow: /page\nDisallow: /page\n"
    assert allowed(tie_reversed, "https://h.test/page")


def test_dollar_counts_toward_specificity() -> None:
    rules = "User-agent: *\nDisallow: /page\nAllow: /page$\n"
    assert allowed(rules, "https://h.test/page")
    assert not allowed(rules, "https://h.test/page2")


def test_group_selection_by_product_token() -> None:
    text = """
User-agent: *
Disallow: /all
User-agent: scrapology
Disallow: /mine
Crawl-delay: 4
User-agent: OtherBot
Disallow: /
"""
    assert allowed(text, "https://h.test/all")
    assert not allowed(text, "https://h.test/mine")
    assert allowed(text, "https://h.test/mine", agent="Nobody")
    assert not allowed(text, "https://h.test/all", agent="Nobody")
    assert not allowed(text, "https://h.test/anything", agent="OtherBot/2.0")
    rules = RobotsRules.parse(text)
    assert rules.crawl_delay("Scrapology/0.1 (+url)") == 4.0
    assert rules.crawl_delay("Nobody") is None


def test_several_groups_for_one_token_are_merged() -> None:
    text = "User-agent: scrapology\nDisallow: /a\n\nUser-agent: Scrapology\nDisallow: /b\n"
    assert not allowed(text, "https://h.test/a") and not allowed(text, "https://h.test/b")


def test_consecutive_agent_lines_share_rules() -> None:
    text = "User-agent: a\nUser-agent: scrapology\nDisallow: /x\n"
    assert not allowed(text, "https://h.test/x")
    assert not allowed(text, "https://h.test/x", agent="A")


def test_rules_before_any_group_and_unknown_fields_are_ignored() -> None:
    text = "Disallow: /\nSitemap: https://h.test/map.xml\nUser-agent: *\nSitemap: https://h.test/m2\nDisallow: /z\n"
    assert allowed(text, "https://h.test/anything")
    assert not allowed(text, "https://h.test/z")


def test_empty_disallow_and_empty_file_allow_everything() -> None:
    assert allowed("User-agent: *\nDisallow:\n", "https://h.test/anything")
    assert allowed("", "https://h.test/anything")
    assert allowed("# only a comment\n", "https://h.test/anything")


def test_pattern_without_leading_slash_gets_one() -> None:
    assert not allowed("User-agent: *\nDisallow: secret\n", "https://h.test/secret/x")
    assert not allowed("User-agent: *\nDisallow: *.gif$\n", "https://h.test/img/a.gif")


def test_percent_encoding_normalisation() -> None:
    assert normalise_path("/caf%c3%a9") == "/caf%C3%A9"
    assert normalise_path("/café") == "/caf%C3%A9"
    assert normalise_path("/a%2Fb") == "/a%2Fb"
    assert normalise_path("/a%7Eb") == "/a~b"
    assert normalise_path("/a b") == "/a%20b"
    assert normalise_path("/a%zz") == "/a%25zz" or normalise_path("/a%zz") == "/a%zz"
    rules = "User-agent: *\nDisallow: /café\nDisallow: /wiki/Pok%c3%a9mon\n"
    assert not allowed(rules, "https://h.test/caf%C3%A9")
    assert not allowed(rules, "https://h.test/café/menu")
    assert not allowed(rules, "https://h.test/wiki/Pok%C3%A9mon")
    assert allowed(rules, "https://h.test/cafe")


def test_comparison_path_defaults_and_query() -> None:
    assert comparison_path("https://h.test") == "/"
    assert comparison_path("https://h.test/a?b=1") == "/a?b=1"


def test_product_token() -> None:
    assert product_token("Scrapology/0.1.0 (+https://x)") == "scrapology"
    assert product_token("  OtherBot  ") == "otherbot"
    assert product_token("") == ""


# ---------- policy: fetch outcomes ----------


def _fetcher(status: int = 200, text: str = WILDCARDS, fail: bool = False):
    calls: list[str] = []

    def fetch(url: str) -> transport.Response:
        calls.append(url)
        if fail:
            raise transport.NetworkError("boom")
        return transport.Response(status, url, text.encode(), {"content-type": "text/plain"})

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_policy_applies_wildcards() -> None:
    policy = RobotsPolicy(fetcher=_fetcher())
    assert policy.check("https://host.test/ok").allowed
    decision = policy.check("https://host.test/private-area/")
    assert not decision.allowed and "robots.txt" in decision.reason
    assert not policy.check("https://host.test/x/file.pdf").allowed
    assert policy.guard("https://host.test/x/file.pdf") is not None
    assert policy.guard("https://host.test/ok") is None


def test_missing_robots_allows_everything() -> None:
    policy = RobotsPolicy(fetcher=_fetcher(status=404, text="<html>no</html>"))
    decision = policy.check("https://host.test/anything")
    assert decision.allowed and "no robots.txt" in decision.reason


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_unreachable_statuses_disallow_for_the_run(status: int) -> None:
    policy = RobotsPolicy(fetcher=_fetcher(status=status))
    decision = policy.check("https://host.test/anything")
    assert not decision.allowed and str(status) in decision.reason


def test_network_error_disallows_for_the_run() -> None:
    policy = RobotsPolicy(fetcher=_fetcher(fail=True))
    decision = policy.check("https://host.test/anything")
    assert not decision.allowed and "could not be fetched" in decision.reason


def test_robots_is_fetched_once_per_host() -> None:
    fetch = _fetcher()
    policy = RobotsPolicy(fetcher=fetch)
    policy.check("https://host.test/a")
    policy.check("https://host.test/b")
    policy.check("https://HOST.test/c")
    policy.check("https://other.test/a")
    assert fetch.calls == ["https://host.test/robots.txt", "https://other.test/robots.txt"]


def test_crawl_delay_is_capped_with_a_note() -> None:
    policy = RobotsPolicy(fetcher=_fetcher(text="User-agent: *\nCrawl-delay: 600\n"))
    decision = policy.check("https://host.test/a")
    assert decision.allowed and decision.crawl_delay == CRAWL_DELAY_CAP
    assert decision.note and "capped" in decision.note
    policy = RobotsPolicy(fetcher=_fetcher(text="User-agent: *\nCrawl-delay: 3\n"))
    decision = policy.check("https://host.test/a")
    assert decision.crawl_delay == 3.0 and decision.note is None
