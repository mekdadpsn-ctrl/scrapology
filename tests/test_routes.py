import pytest

from scrapology.routes import BLOCKED, CELLAR, DOI, EUROPEPMC, WEB, eli_to_celex, route_for


@pytest.mark.parametrize(
    "target, kind, value",
    [
        ("pmc:PMC10663573", EUROPEPMC, "PMC10663573"),
        ("pmc:10663573", EUROPEPMC, "PMC10663573"),
        ("PMC:pmc10663573", EUROPEPMC, "PMC10663573"),
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC10663573/", EUROPEPMC, "PMC10663573"),
        ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10663573/", EUROPEPMC, "PMC10663573"),
        ("https://europepmc.org/article/PMC/PMC10663573", EUROPEPMC, "PMC10663573"),
        ("https://europepmc.org/articles/PMC10663573", EUROPEPMC, "PMC10663573"),
        ("doi:10.1038/s41598-023-47754-w", DOI, "10.1038/s41598-023-47754-w"),
        ("https://doi.org/10.1038/s41598-023-47754-w", DOI, "10.1038/s41598-023-47754-w"),
        ("http://dx.doi.org/10.1038/s41598-023-47754-w", DOI, "10.1038/s41598-023-47754-w"),
        ("https://doi.org/10.1000/abc(1)2.", DOI, "10.1000/abc(1)2"),
        ("https://onlinelibrary.wiley.com/doi/10.1002/anie.202214722", DOI, "10.1002/anie.202214722"),
        ("https://onlinelibrary.wiley.com/doi/full/10.1002/anie.202214722?x=1", DOI, "10.1002/anie.202214722"),
        ("https://onlinelibrary.wiley.com/doi/epdf/10.1002/anie.202214722", DOI, "10.1002/anie.202214722"),
        ("celex:32016R0679", CELLAR, "32016R0679"),
        ("celex:32016r0679", CELLAR, "32016R0679"),
        ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679", CELLAR, "32016R0679"),
        ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32016R0679", CELLAR, "32016R0679"),
        ("https://eur-lex.europa.eu/eli/reg/2016/679/oj", CELLAR, "32016R0679"),
        ("https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng", CELLAR, "32016R0679"),
        ("https://eur-lex.europa.eu/eli/dir/2011/91/oj", CELLAR, "32011L0091"),
        ("https://eur-lex.europa.eu/eli/dec/2020/1", CELLAR, "32020D0001"),
        ("https://eur-lex.europa.eu/eli/reg_impl/2019/1793/oj", CELLAR, "32019R1793"),
        ("https://eur-lex.europa.eu/eli/reg_del/2021/630/oj", CELLAR, "32021R0630"),
        ("https://www.google.com/search?q=x", WEB, "https://www.google.com/search?q=x"),
        ("http://example.com/page", WEB, "http://example.com/page"),
        ("example.com", WEB, "https://example.com"),
        ("example.com/path?q=1", WEB, "https://example.com/path?q=1"),
        ("https://doi.org/", WEB, "https://doi.org/"),
        ("https://www.ncbi.nlm.nih.gov/books/NBK1/", WEB, "https://www.ncbi.nlm.nih.gov/books/NBK1/"),
        ("https://pubmed.ncbi.nlm.nih.gov/12345/", WEB, "https://pubmed.ncbi.nlm.nih.gov/12345/"),
    ],
)
def test_route_for(target: str, kind: str, value: str) -> None:
    route = route_for(target)
    assert (route.kind, route.value) == (kind, value)


@pytest.mark.parametrize(
    "target",
    [
        "https://notwiley.test/onlinelibrary.wiley.com/doi/10.1002/x",
        "https://eur-lex.europa.eu.evil.test/eli/reg/2016/679/oj",
        "https://evil.test/?next=https://pmc.ncbi.nlm.nih.gov/articles/PMC1/",
        "https://europepmc.org.evil.test/articles/PMC1",
    ],
)
def test_hosts_are_matched_on_hostname_not_substring(target: str) -> None:
    assert route_for(target).kind == WEB


def test_wiley_without_doi_is_blocked() -> None:
    route = route_for("https://onlinelibrary.wiley.com/journal/15214095")
    assert route.kind == BLOCKED
    assert "Wiley" in route.note


def test_eurlex_without_identifier_is_blocked() -> None:
    route = route_for("https://eur-lex.europa.eu/homepage.html")
    assert route.kind == BLOCKED
    assert "CELEX" in route.note


@pytest.mark.parametrize(
    "target",
    [
        "https://pmc.ncbi.nlm.nih.gov/",
        "https://pmc.ncbi.nlm.nih.gov/search/?term=x",
        "https://europepmc.org/search?query=x",
        "https://www.ncbi.nlm.nih.gov/pmc/",
    ],
)
def test_pmc_pages_that_are_not_articles_are_blocked_with_a_hint(target: str) -> None:
    route = route_for(target)
    assert route.kind == BLOCKED and "pmc:<PMCID>" in route.note


@pytest.mark.parametrize(
    "target",
    ["", "   ", "pmc:", "pmc:abc", "doi:", "doi:not-a-doi", "celex:", "ftp://example.com/file", "??",
     "https://doi.org/not-a-doi"],
)
def test_bad_targets_raise(target: str) -> None:
    with pytest.raises(ValueError):
        route_for(target)


def test_eli_mapping() -> None:
    assert eli_to_celex("reg", "2016", "679") == "32016R0679"
    assert eli_to_celex("dir", "2011", "91") == "32011L0091"
    assert eli_to_celex("DEC", "2020", "7") == "32020D0007"
