"""The web route: robots.txt, a plain GET, and a browser render only when the page needs it.

Engine `auto`: a plain HTTP GET with the honest User-Agent first. A 200 with thin main text on a page
that runs scripts is rendered in a browser. A 401/403/429/503 or a challenge page is a block: the
tool stops there and never retries with the browser, which would be working around the site's
decision. Engine `http` never opens a browser. Engine `browser` skips the plain GET.

Both engines apply the same rules: robots.txt for the first URL and for every redirect or
navigation after it, the per-host delay for every page request, the scheme allow-list, the size cap,
and the same block detection on the answer.
"""

import importlib.util
import json
from dataclasses import dataclass, field
from typing import Any

import trafilatura

from scrapology import browser, detect, transport
from scrapology.session import Session
from scrapology.transport import DEFAULT_MAX_BYTES

ENGINES = ("auto", "http", "browser")
ACCEPT_HTML = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"


@dataclass
class PageResult:
    outcome: str  # fetched, blocked, failed
    note: str = ""
    url: str = ""
    final_url: str = ""
    http_status: int | None = None
    engine: str | None = None
    browser: str | None = None
    html: str = ""
    raw: bytes | None = None  # byte-exact body from the plain GET
    rendered: bool = False  # html is a rendered DOM, not the bytes served
    text: str = ""
    markdown: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None
    redirects: list[str] = field(default_factory=list)


def structured_available() -> bool:
    return importlib.util.find_spec("extruct") is not None


def extract_main(html: str, url: str) -> tuple[dict[str, Any], str, str]:
    """(metadata, plain text, markdown) from trafilatura. Empty values when it finds no main content."""
    if not html or not html.strip():
        return {}, "", ""
    document = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_tables=True)
    meta: dict[str, Any] = {}
    text = ""
    if document is not None:
        meta = {
            "title": document.title,
            "author": document.author,
            "date": document.date,
            "sitename": document.sitename,
            "hostname": document.hostname,
            "description": document.description,
        }
        text = document.text or ""
    markdown = (
        trafilatura.extract(html, url=url, output_format="markdown", with_metadata=False, include_tables=True) or ""
    )
    return meta, text, markdown


def main_markdown(meta: dict[str, Any], markdown: str, fallback_title: str) -> str:
    """The `.main.md` body: title, a metadata line, then trafilatura's markdown (without a repeated title)."""
    title = meta.get("title") or fallback_title
    body = markdown.strip()
    first, _, rest = body.partition("\n")
    if first.strip() == f"# {title}":
        body = rest.strip()
    lines = [
        f"# {title}",
        "",
        f"Date: {meta.get('date') or '[not found]'} | Author: {meta.get('author') or '[not found]'} | "
        f"Site: {meta.get('sitename') or '[not found]'}",
        "",
        body or "(no main text found by trafilatura; read the raw file)",
    ]
    return "\n".join(lines).rstrip() + "\n"


def structured_data(html: str, url: str) -> dict[str, Any]:
    """JSON-LD, microdata and OpenGraph via extruct (the `structured` extra)."""
    try:
        import extruct
    except ImportError as err:
        message = "structured output needs the `structured` extra: pip install scrapology[structured]"
        raise RuntimeError(message) from err
    if not html:
        return {}
    data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata", "opengraph"], uniform=True)
    json.dumps(data)  # fail early on anything not serialisable
    return data


def _join(*notes: str | None) -> str:
    return "; ".join(n for n in notes if n)


def _finish(
    result: PageResult, status: int | None, html: str, text: str, render_warning: str | None = None
) -> PageResult:
    """Apply block detection, the HTTP status rule and the warnings to a fetched page."""
    reason = detect.block_reason(status, html, text)
    if reason:
        result.outcome, result.note = "blocked", _join(result.note, reason)
        return result
    if status is not None and not 200 <= status < 300:
        result.outcome, result.note = "failed", _join(result.note, f"HTTP {status}")
        return result
    result.warning = _join(render_warning, detect.short_page_warning(text)) or None
    return result


def read_page(
    url: str, session: Session, *, engine: str = "auto", timeout: float = 60, max_bytes: int = DEFAULT_MAX_BYTES
) -> PageResult:
    """Fetch one page under the rules. Never raises for a block or a failure: read `outcome` and `note`."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}, not {engine!r}")
    decision = session.robots.check(url)
    notes: list[str] = [decision.note] if decision.note else []
    if decision.crawl_delay:
        session.limiter.set_host_delay(url, decision.crawl_delay)
    if not decision.allowed:
        return PageResult("blocked", _join(*notes, f"robots.txt: {decision.reason}"), url=url, final_url=url)
    guard = session.robots.guard

    if engine in ("auto", "http"):
        try:
            response = transport.get(
                url, accept=ACCEPT_HTML, timeout=timeout, limiter=session.limiter, guard=guard, max_bytes=max_bytes
            )
        except transport.RedirectBlocked as err:
            return PageResult("blocked", _join(*notes, str(err)), url=url, final_url=err.url, engine="http")
        except transport.TransportError as err:
            return PageResult("failed", _join(*notes, str(err)), url=url, final_url=url, engine="http")
        html = response.text()
        meta, text, markdown = extract_main(html, response.url)
        result = PageResult(
            "fetched",
            note=_join(*notes),
            url=url,
            final_url=response.url,
            http_status=response.status,
            engine="http",
            html=html,
            raw=response.body,
            text=text,
            markdown=markdown,
            meta=meta,
            redirects=list(response.history),
        )
        result = _finish(result, response.status, html, text)
        if result.outcome != "fetched" or engine == "http" or not detect.needs_render(html, text):
            return result
        notes.append(f"plain GET gave {len(text.strip())} chars of main text on a scripted page; rendered in a browser")

    session.limiter.wait(url)
    try:
        rendered = browser.render(
            url, timeout=timeout, guard=guard, throttle=session.limiter.wait, max_bytes=max_bytes
        )
    except browser.RenderBlocked as err:
        return PageResult("blocked", _join(*notes, str(err)), url=url, final_url=err.url, engine="browser")
    except browser.RenderError as err:
        return PageResult("failed", _join(*notes, str(err)), url=url, final_url=url, engine="browser")
    meta, text, markdown = extract_main(rendered.html, rendered.url)
    result = PageResult(
        "fetched",
        note=_join(*notes),
        url=url,
        final_url=rendered.url,
        http_status=rendered.status,
        engine="browser",
        browser=rendered.browser,
        html=rendered.html,
        rendered=True,
        text=text,
        markdown=markdown,
        meta=meta,
        redirects=list(rendered.history),
    )
    return _finish(result, rendered.status, rendered.html, text, rendered.warning)
