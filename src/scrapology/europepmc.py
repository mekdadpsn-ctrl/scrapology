"""Europe PMC: the official route to open-access papers (PubMed Central blocks headless browsers).

REST reference: https://europepmc.org/RestfulWebService
Only open-access full text is fetched, one article per call. Europe PMC does not permit automated
bulk download of other content, so a record that is not open access is reported as blocked. The
API is called under its own terms and is not gated on robots.txt; the per-host delay still applies.
"""

import html as htmllib
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from lxml import etree

from scrapology import transport
from scrapology.ratelimit import HostRateLimiter
from scrapology.session import default_session

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ZERO_HIT_PAUSE = 2.0
XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


@dataclass
class Paper:
    """One search hit, from the `lite` result type."""

    pmcid: str | None
    pmid: str | None
    doi: str | None
    title: str
    authors: str
    journal: str | None
    year: str | None
    open_access: bool
    cited_by: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_title(raw: str | None) -> str:
    """Titles come back with entities and occasional tags (<i>, <sup>): strip both."""
    text = htmllib.unescape(raw or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def paper_from_hit(hit: dict[str, Any]) -> Paper:
    return Paper(
        pmcid=hit.get("pmcid") or None,
        pmid=hit.get("pmid") or None,
        doi=hit.get("doi") or None,
        title=clean_title(hit.get("title")),
        authors=hit.get("authorString") or "",
        journal=hit.get("journalTitle") or None,
        year=str(hit["pubYear"]) if hit.get("pubYear") else None,
        open_access=hit.get("isOpenAccess") == "Y",
        cited_by=hit.get("citedByCount"),
    )


def search_url(query: str, n: int, oa_only: bool) -> str:
    q = f"({query}) AND OPEN_ACCESS:Y" if oa_only else query
    params = {"query": q, "format": "json", "pageSize": max(1, min(int(n), 1000)), "resultType": "lite"}
    return f"{EPMC}/search?" + urllib.parse.urlencode(params)


def _limiter(limiter: HostRateLimiter | None) -> HostRateLimiter:
    return limiter if limiter is not None else default_session().limiter


def search_raw(
    query: str,
    n: int = 10,
    oa_only: bool = False,
    *,
    timeout: float = 60,
    limiter: HostRateLimiter | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, list[dict[str, Any]]]:
    """(hitCount, hits). Retries once, and only once, when the answer is 0 hits: the service has
    returned 0 and then a full result set a minute apart for the same query."""
    url = search_url(query, n, oa_only)
    limiter = _limiter(limiter)
    data = _json(transport.get(url, accept="application/json", timeout=timeout, limiter=limiter))
    if not data.get("hitCount"):
        sleep(ZERO_HIT_PAUSE)
        data = _json(transport.get(url, accept="application/json", timeout=timeout, limiter=limiter))
    hits = data.get("resultList", {}).get("result", []) or []
    return int(data.get("hitCount") or 0), hits


def search(
    query: str,
    n: int = 10,
    oa_only: bool = False,
    *,
    timeout: float = 60,
    limiter: HostRateLimiter | None = None,
) -> list[Paper]:
    """Search Europe PMC. Its query syntax works: TITLE:"surveillance", AUTH:"Smith J", PUB_YEAR:[2020 TO 2026]."""
    _, hits = search_raw(query, n, oa_only, timeout=timeout, limiter=limiter)
    return [paper_from_hit(h) for h in hits]


def record_for_doi(doi: str, *, timeout: float = 60, limiter: HostRateLimiter | None = None) -> Paper | None:
    _, hits = search_raw(f'DOI:"{doi}"', n=1, timeout=timeout, limiter=limiter)
    return paper_from_hit(hits[0]) if hits else None


def record_for_pmcid(pmcid: str, *, timeout: float = 60, limiter: HostRateLimiter | None = None) -> Paper | None:
    _, hits = search_raw(f"PMCID:{pmcid}", n=1, timeout=timeout, limiter=limiter)
    return paper_from_hit(hits[0]) if hits else None


def fulltext_url(pmcid: str) -> str:
    return f"{EPMC}/{urllib.parse.quote(pmcid)}/fullTextXML"


def fulltext(
    pmcid: str, *, timeout: float = 60, limiter: HostRateLimiter | None = None, max_bytes: int | None = None
) -> transport.Response:
    """Full text JATS XML. HTTP 404 means Europe PMC holds no open full text for the article."""
    options: dict[str, Any] = {"accept": "application/xml", "timeout": timeout, "limiter": _limiter(limiter)}
    if max_bytes is not None:
        options["max_bytes"] = max_bytes
    return transport.get(fulltext_url(pmcid), **options)


def _json(response: transport.Response) -> dict[str, Any]:
    where = transport.redact(response.url)
    if not response.ok:
        raise transport.NetworkError(f"Europe PMC answered HTTP {response.status} for {where}")
    try:
        data = json.loads(response.body)
    except json.JSONDecodeError as err:
        raise transport.NetworkError(f"Europe PMC returned no JSON for {where}: {err}") from err
    return data if isinstance(data, dict) else {}


# ---------- JATS XML to markdown ----------


def _text(element: Any) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _is_author(contrib: Any) -> bool:
    """Authors often carry no contrib-type; the enclosing contrib-group carries content-type instead.
    Missing means author. Editors and other roles are excluded."""
    kind = contrib.get("contrib-type")
    if not kind:
        parent = contrib.getparent()
        kind = parent.get("content-type") if parent is not None else None
    kind = (kind or "author").lower()
    return kind.startswith("author")


def _article_doi(root: Any) -> Any:
    for article_id in root.iterfind(".//article-meta/article-id"):
        if (article_id.get("pub-id-type") or "").lower() == "doi":
            return article_id
    return None


def jats_metadata(root: Any) -> dict[str, str]:
    authors = []
    for contrib in root.findall(".//article-meta//contrib"):
        name = contrib.find("name")
        if name is None or not _is_author(contrib):
            continue
        full = f"{_text(name.find('surname'))} {_text(name.find('given-names'))}".strip()
        if full:
            authors.append(full)
    return {
        "title": _text(root.find(".//article-meta//article-title")),
        "journal": _text(root.find(".//journal-title")),
        "year": _text(root.find(".//article-meta//pub-date/year")),
        "doi": _text(_article_doi(root)),
        "authors": "; ".join(authors),
    }


def jats_to_markdown(xml_bytes: bytes) -> tuple[dict[str, str], str]:
    """(metadata, markdown): title block, abstract, sections, paragraphs, lists, tables, figure captions.
    Raises ValueError for an empty or malformed document."""
    if not xml_bytes or not xml_bytes.strip():
        raise ValueError("empty document")
    try:
        root = etree.fromstring(xml_bytes, parser=XML_PARSER)
    except etree.XMLSyntaxError as err:
        raise ValueError(f"not well-formed XML: {err}") from err
    meta = jats_metadata(root)
    out = [
        f"# {meta['title'] or '[title not found]'}",
        "",
        f"Journal: {meta['journal'] or '[not found]'} | Year: {meta['year'] or '[not found]'} | "
        f"DOI: {meta['doi'] or '[not found]'}",
        f"Authors: {meta['authors'] or '[not found]'}",
        "",
    ]
    for abstract in root.findall(".//article-meta/abstract"):
        out += ["## Abstract", ""] + [_text(p) for p in abstract.iter("p")] + [""]

    def walk(element: Any, depth: int) -> None:
        for child in element:
            tag = child.tag if isinstance(child.tag, str) else ""
            if tag == "sec":
                title = child.find("title")
                if title is not None:
                    out.extend(["#" * min(depth, 6) + " " + _text(title), ""])
                walk(child, depth + 1)
            elif tag == "p":
                out.extend([_text(child), ""])
            elif tag == "list":
                out.extend(["- " + _text(item) for item in child.findall("list-item")] + [""])
            elif tag == "table-wrap":
                caption = f"{_text(child.find('label'))} {_text(child.find('caption'))}".strip()
                if caption:
                    out.append(f"**{caption}**")
                for row in child.iter("tr"):
                    cells = [_text(c) for c in row if isinstance(c.tag, str) and c.tag in ("td", "th")]
                    out.append("| " + " | ".join(cells) + " |")
                out.append("")
            elif tag == "fig":
                caption = f"{_text(child.find('label'))} {_text(child.find('caption'))}".strip()
                if caption:
                    out.extend([f"*{caption}*", ""])
            elif tag == "title":
                continue
            else:
                walk(child, depth)

    body = root.find(".//body")
    if body is not None:
        walk(body, 2)
    return meta, "\n".join(out).rstrip() + "\n"
