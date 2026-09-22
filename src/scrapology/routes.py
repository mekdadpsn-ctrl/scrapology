"""Pick the route for a target: the official API where one exists, the open web otherwise.

Targets:
- `pmc:PMC123`, PubMed Central and Europe PMC article URLs   -> europepmc
- `doi:10.x/...`, doi.org URLs, Wiley URLs carrying a DOI     -> doi (Europe PMC, then Unpaywall)
- `celex:...`, EUR-Lex URLs with a CELEX number or an ELI path -> cellar
- anything else with an http(s) scheme, or a bare host          -> web
Hosts are matched on the parsed hostname, never on substrings. Wiley, EUR-Lex and PubMed Central
URLs that carry no identifier are reported as blocked: those sites refuse headless browsers, and
the tool never tries to get past that.
"""

import re
import urllib.parse
from dataclasses import dataclass
from urllib.parse import urlsplit

WEB, EUROPEPMC, DOI, CELLAR, BLOCKED = "web", "europepmc", "doi", "cellar", "blocked"

PMC_HOSTS = frozenset({"pmc.ncbi.nlm.nih.gov", "europepmc.org", "www.europepmc.org"})
NCBI_HOST = "www.ncbi.nlm.nih.gov"
DOI_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})
WILEY_HOSTS = frozenset({"onlinelibrary.wiley.com"})
EURLEX_HOSTS = frozenset({"eur-lex.europa.eu"})

_PMC_PATH = re.compile(r"^/(?:pmc/)?articles?/(PMC\d+)", re.I)
_EUROPEPMC_PATH = re.compile(r"^/(?:article/PMC/|articles/|abstract/PMC/)(PMC\d+)", re.I)
_DOI = re.compile(r"10\.\d{4,9}/[^\s?#]+", re.I)
_WILEY_PATH = re.compile(r"^/doi/(?:abs/|full/|epdf/|pdf/|epub/)?(10\.\d{4,9}/[^?#\s]+)", re.I)
_CELEX_IN_URL = re.compile(r"CELEX(?::|%3A)([0-9A-Z()]+)", re.I)
_ELI = re.compile(r"/eli/(reg|dir|dec)(?:_impl|_del)?/(\d{4})/(\d+)(?:/oj)?(?:[/?#]|$)", re.I)
_ELI_TYPE = {"reg": "R", "dir": "L", "dec": "D"}


@dataclass(frozen=True)
class Route:
    kind: str
    value: str
    note: str = ""


def _clean_doi(doi: str) -> str:
    doi = urllib.parse.unquote(doi.strip())
    return doi.rstrip(".,;:)")


def eli_to_celex(eli_type: str, year: str, number: str) -> str:
    """ELI `/eli/reg/2016/679/oj` maps to CELEX `32016R0679`: sector 3, year, type letter, number padded to 4."""
    return f"3{year}{_ELI_TYPE[eli_type.lower()]}{int(number):04d}"


def _pmcid(value: str) -> str:
    value = value.strip().upper()
    if value.isdigit():
        value = "PMC" + value
    if not re.fullmatch(r"PMC\d+", value):
        raise ValueError(f"not a PMCID: {value!r}")
    return value


def route_for(target: str) -> Route:
    text = target.strip()
    if not text:
        raise ValueError("empty target")
    lower = text.lower()

    if lower.startswith("pmc:"):
        return Route(EUROPEPMC, _pmcid(text[4:]))
    if lower.startswith("celex:"):
        value = text[6:].strip().upper()
        if not value:
            raise ValueError("empty CELEX number")
        return Route(CELLAR, value)
    if lower.startswith("doi:"):
        value = _clean_doi(text[4:])
        if not _DOI.fullmatch(value):
            raise ValueError(f"not a DOI: {value!r}")
        return Route(DOI, value)

    if re.match(r"^[a-z][a-z0-9+.-]*://", lower):
        url = text
    elif re.match(r"^[a-z0-9.-]+(?::\d+)?(?:/|$)", lower):
        url = "https://" + text
    else:
        raise ValueError(f"not a URL or a known identifier: {text!r}")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme in {text!r}: only http and https are fetched")
    host = (parts.hostname or "").lower()
    path = parts.path or "/"

    if host in PMC_HOSTS or (host == NCBI_HOST and path.lower().startswith("/pmc/")):
        match = _EUROPEPMC_PATH.match(path) if host.endswith("europepmc.org") else _PMC_PATH.match(path)
        if match:
            return Route(EUROPEPMC, match.group(1).upper())
        return Route(
            BLOCKED,
            url,
            "PubMed Central or Europe PMC page that is not an article: the site blocks headless browsers, "
            "use pmc:<PMCID> for an article or `scrapology search` for a query",
        )

    if host in DOI_HOSTS:
        value = _clean_doi(path.lstrip("/"))
        if _DOI.fullmatch(value):
            return Route(DOI, value)
        if not value:
            return Route(WEB, url)
        raise ValueError(f"doi.org URL without a DOI: {text}")

    if host in WILEY_HOSTS:
        match = _WILEY_PATH.match(path)
        if match:
            return Route(DOI, _clean_doi(match.group(1)))
        return Route(BLOCKED, url, "Wiley URL without a DOI: Wiley blocks headless browsers, give the DOI instead")

    if host in EURLEX_HOSTS:
        match = _CELEX_IN_URL.search(url)
        if match:
            return Route(CELLAR, urllib.parse.unquote(match.group(1)).upper())
        match = _ELI.search(path)
        if match:
            return Route(CELLAR, eli_to_celex(match.group(1), match.group(2), match.group(3)))
        return Route(
            BLOCKED,
            url,
            "EUR-Lex URL without a CELEX number or ELI path: EUR-Lex blocks headless browsers, use celex:<number>",
        )

    return Route(WEB, url)
