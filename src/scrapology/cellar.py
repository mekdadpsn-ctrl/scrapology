"""EU CELLAR: the official route to EU legal acts (EUR-Lex blocks headless browsers).

`https://publications.europa.eu/resource/celex/{CELEX}` serves the act when asked with both
`Accept: application/xhtml+xml` and `Accept-Language: eng`. `text/html` returns 404, and so does the
two-letter `en`. The `;type=simplified` variant is the second option. Older acts may be PDF only,
which this version does not fetch. The API is called under its own terms and is not gated on
robots.txt; the per-host delay still applies.
"""

import re
import urllib.parse
from typing import Any

from lxml import etree
from lxml import html as lxml_html

from scrapology import transport
from scrapology.ratelimit import HostRateLimiter

CELLAR = "https://publications.europa.eu/resource/celex/"
ACCEPTS = ("application/xhtml+xml", "application/xhtml+xml;type=simplified")
LANGUAGE = "eng"
HTML_PARSER = lxml_html.HTMLParser(no_network=True, remove_comments=True, remove_pis=True)

# EUR-Lex XHTML marks structure with classes on <p>, with or without an `oj-` prefix depending on the
# act's vintage. These become markdown headings. `doc-ti` paragraphs form the title block instead.
HEADING_CLASSES = {
    "ti-section-1": "##",
    "ti-section-2": "###",
    "ti-grseq-1": "##",
    "ti-art": "##",
    "sti-art": "###",
}
TITLE_CLASS = "doc-ti"
MAX_TITLE_PARTS = 4


def celex_url(celex: str) -> str:
    return CELLAR + urllib.parse.quote(celex.strip())


def fetch_xhtml(
    celex: str, *, timeout: float = 60, limiter: HostRateLimiter | None = None, max_bytes: int | None = None
) -> transport.Response:
    """The English XHTML of a CELEX document. Returns the last (404) response when neither form exists."""
    url = celex_url(celex)
    options: dict[str, Any] = {"timeout": timeout, "limiter": limiter, "accept_language": LANGUAGE}
    if max_bytes is not None:
        options["max_bytes"] = max_bytes
    response: transport.Response | None = None
    for accept in ACCEPTS:
        response = transport.get(url, accept=accept, **options)
        if response.status != 404:
            return response
    assert response is not None
    return response


def _text(element: Any) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _inside_table_cell(element: Any) -> bool:
    return any(isinstance(a.tag, str) and a.tag in ("td", "th") for a in element.iterancestors())


def _classes(element: Any) -> set[str]:
    """Class names with any `oj-` prefix removed, so old and new EUR-Lex markup read the same."""
    out = set()
    for name in (element.get("class") or "").split():
        out.add(name[3:] if name.startswith("oj-") else name)
    return out


def _heading_level(element: Any) -> str | None:
    for name in _classes(element):
        if name in HEADING_CLASSES:
            return HEADING_CLASSES[name]
    return None


def document_title(doc: Any) -> str:
    """The act's title from the leading `doc-ti` paragraphs ("REGULATION (EU) 2016/679 ... of 27 April 2016
    on the protection of natural persons..."), stopping before a bracketed note. Falls back to the <title>
    element, which CELLAR fills with the file name."""
    parts: list[str] = []
    for paragraph in doc.iter("p"):
        if TITLE_CLASS in _classes(paragraph):
            text = _text(paragraph)
            if not text:
                continue
            if parts and text.startswith("("):
                break
            parts.append(text)
            if len(parts) >= MAX_TITLE_PARTS:
                break
        elif parts:
            break
    if parts:
        return " ".join(parts)
    title_el = doc.find(".//title")
    return _text(title_el) if title_el is not None else ""


def xhtml_to_markdown(xhtml_bytes: bytes) -> tuple[dict[str, str], str]:
    """(metadata, markdown). Article headings, paragraphs and list items become text; table rows
    (annexes are tables) become `| a | b |` lines, and text inside a cell is not repeated as a paragraph.
    Raises ValueError for an empty or unparseable document."""
    if not xhtml_bytes or not xhtml_bytes.strip():
        raise ValueError("empty document")
    try:
        doc = lxml_html.fromstring(xhtml_bytes, parser=HTML_PARSER)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as err:
        raise ValueError(f"unparseable document: {err}") from err
    title = document_title(doc)
    out = [f"# {title or '[title not found]'}", ""]
    for element in doc.iter():
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag == "tr":
            cells = [_text(c) for c in element if isinstance(c.tag, str) and c.tag in ("td", "th")]
            if any(cells):
                out.append("| " + " | ".join(cells) + " |")
        elif tag in ("p", "h1", "h2", "h3", "h4", "li") and not _inside_table_cell(element):
            text = _text(element)
            if not text:
                continue
            level = _heading_level(element)
            if tag in ("h1", "h2", "h3", "h4"):
                level = "#" * int(tag[1])
            if level:
                out.extend([f"{level} {text}", ""])
            else:
                out.extend([text, ""])
    return {"title": title}, "\n".join(out).rstrip() + "\n"
