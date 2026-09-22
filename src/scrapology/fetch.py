"""`fetch()`: one target in, files and a receipt out.

Outcomes: `fetched` (files written, warnings allowed), `blocked` (robots.txt, a refused redirect or
navigation, a challenge page, a refusing status, or no open copy), `failed` (network, timeout, HTTP
error, oversized body, no usable browser, or anything unexpected). Bad arguments raise ValueError
before any network work; nothing else raises.
"""

import importlib.util
import json
import os
from dataclasses import dataclass, field
from typing import Any

from scrapology import cellar, detect, europepmc, provenance, transport, unpaywall
from scrapology.browser import RenderBlocked, RenderError
from scrapology.routes import BLOCKED, CELLAR, DOI, EUROPEPMC, WEB, Route, route_for
from scrapology.session import Session, default_session
from scrapology.transport import DEFAULT_MAX_BYTES
from scrapology.web import ENGINES, PageResult, main_markdown, read_page, structured_data

DEFAULT_OUT = "./scrapology-out"
EXIT_CODES = {"fetched": 0, "failed": 1, "blocked": 3}
EMAIL_VARIABLE = "SCRAPOLOGY_EMAIL"


@dataclass
class FetchResult:
    outcome: str
    route: str
    url: str | None
    final_url: str | None
    files: dict[str, str]
    note: str
    warning: str | None
    http_status: int | None
    browser: str | None
    engine: str | None = None
    title: str | None = None
    target: str = ""
    name: str = ""
    accessed_utc: str = ""
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "fetched"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.outcome, 1)


@dataclass
class _Draft:
    """What a route hands back before anything is written."""

    outcome: str
    route: str
    note: str = ""
    url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    engine: str | None = None
    browser: str | None = None
    raw: bytes | None = None  # byte-exact body, saved as .raw.<ext>
    raw_ext: str = "raw.html"
    rendered_html: str | None = None  # a rendered DOM, saved as .rendered.html
    main: str | None = None
    title: str | None = None
    warning: str | None = None
    html: str = ""
    tool: str = "scrapology"
    structured: dict[str, Any] | None = None
    redirects: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def check_options(
    *, engine: str = "auto", structured: bool = False, timeout: float = 60, delay: float = 2.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Validate the options that do not depend on the target. Raises ValueError with a clear message."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}, not {engine!r}")
    if structured and importlib.util.find_spec("extruct") is None:
        raise ValueError('--structured needs the structured extra: pip install "scrapology[structured]"')
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if delay < 0:
        raise ValueError("delay must be zero or positive")
    if int(max_bytes) < 1:
        raise ValueError("max_bytes must be at least 1")


def fetch(
    target: str,
    out_dir: str = DEFAULT_OUT,
    name: str | None = None,
    structured: bool = False,
    engine: str = "auto",
    timeout: float = 60,
    delay: float = 2.0,
    email: str | None = None,
    *,
    session: Session | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchResult:
    """Fetch one target into `out_dir` and append its record to `<out_dir>/sources.jsonl`.

    `name` is the base name of the output files (validated; a name whose files already exist gets a
    hash suffix so nothing is overwritten). `email` (or SCRAPOLOGY_EMAIL) enables the Unpaywall lookup.
    Bad arguments raise ValueError; everything else is reported in the result."""
    check_options(engine=engine, structured=structured, timeout=timeout, delay=delay, max_bytes=max_bytes)
    route = route_for(target)
    session = session or default_session(delay, timeout)
    email = email or os.environ.get(EMAIL_VARIABLE) or None
    accessed = provenance.utc_now()
    os.makedirs(out_dir, exist_ok=True)
    requested = provenance.validate_name(name) if name is not None else provenance.slugify(target)
    name = provenance.unique_name(requested, provenance.existing_names(out_dir), target)
    provenance.output_path(out_dir, name, ".main.md")  # refuses anything outside out_dir

    try:
        draft = _run(
            route, session, engine=engine, timeout=timeout, email=email, structured=structured, max_bytes=max_bytes
        )
    except transport.RedirectBlocked as err:
        draft = _Draft("blocked", route.kind, str(err), url=route.value, final_url=err.url)
    except RenderBlocked as err:
        draft = _Draft("blocked", route.kind, str(err), url=route.value, final_url=err.url)
    except transport.TransportError as err:
        draft = _Draft("failed", route.kind, str(err), url=route.value)
    except RenderError as err:
        draft = _Draft("failed", route.kind, str(err), url=route.value)
    except Exception as err:  # the receipt is still written; the outcome says what went wrong
        draft = _Draft("failed", route.kind, f"unexpected {type(err).__name__}: {str(err)[:300]}", url=route.value)

    record = provenance.new_record(target, draft.route, accessed, name)
    files: dict[str, str] = {}
    header = provenance.header(target, draft.final_url or draft.url or target, accessed, draft.route, draft.tool)
    keep_bodies = draft.outcome in ("fetched", "blocked")
    if draft.raw is not None and keep_bodies:
        raw_path = provenance.output_path(out_dir, name, "." + draft.raw_ext)
        with open(raw_path, "wb") as handle:
            handle.write(draft.raw)
        files["raw"] = raw_path
        record["sha256_raw"] = provenance.sha256_file(raw_path)
    if draft.rendered_html is not None and keep_bodies:
        rendered_path = provenance.output_path(out_dir, name, ".rendered.html")
        with open(rendered_path, "wb") as handle:
            handle.write(draft.rendered_html.encode())
        files["rendered"] = rendered_path
        record["sha256_rendered"] = provenance.sha256_file(rendered_path)
    if draft.outcome == "fetched" and draft.main is not None:
        main_path = provenance.output_path(out_dir, name, ".main.md")
        with open(main_path, "w", encoding="utf-8") as handle:
            handle.write(header + draft.main)
        files["main"] = main_path
    if draft.outcome == "fetched" and draft.structured is not None:
        structured_path = provenance.output_path(out_dir, name, ".structured.json")
        payload = {
            "source": target,
            "url": draft.final_url or draft.url,
            "accessed_utc": accessed,
            "route": draft.route,
            "extruct": provenance.versions().get("extruct"),
            "data": draft.structured,
        }
        with open(structured_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        files["structured"] = structured_path

    record.update(
        {
            "url": draft.url,
            "final_url": draft.final_url,
            "redirects": list(draft.redirects),
            "engine": draft.engine,
            "browser": draft.browser,
            "http_status": draft.http_status,
            "outcome": draft.outcome,
            "note": draft.note,
            "warning": draft.warning,
            "title": draft.title,
            "files": files,
        }
    )
    record.update(draft.extra)
    provenance.append_record(out_dir, record)
    return FetchResult(
        outcome=draft.outcome,
        route=draft.route,
        url=draft.url,
        final_url=draft.final_url,
        files=files,
        note=draft.note,
        warning=draft.warning,
        http_status=draft.http_status,
        browser=draft.browser,
        engine=draft.engine,
        title=draft.title,
        target=target,
        name=name,
        accessed_utc=accessed,
        redirects=list(draft.redirects),
    )


def _run(
    route: Route, session: Session, *, engine: str, timeout: float, email: str | None, structured: bool,
    max_bytes: int,
) -> _Draft:
    if route.kind == BLOCKED:
        return _Draft("blocked", "web", route.note, url=route.value, final_url=route.value)
    if route.kind == WEB:
        return _web(route.value, session, engine=engine, timeout=timeout, structured=structured, max_bytes=max_bytes)
    if route.kind == EUROPEPMC:
        return _europepmc(route.value, session, timeout=timeout, max_bytes=max_bytes)
    if route.kind == CELLAR:
        return _cellar(route.value, session, timeout=timeout, max_bytes=max_bytes)
    if route.kind == DOI:
        return _doi(
            route.value, session, engine=engine, timeout=timeout, email=email, structured=structured,
            max_bytes=max_bytes,
        )
    raise ValueError(f"unknown route {route.kind!r}")


def _from_page(page: PageResult, route: str, fallback_title: str, structured: bool) -> _Draft:
    tool = f"trafilatura {provenance.versions().get('trafilatura', '?')}"
    draft = _Draft(
        page.outcome,
        route,
        page.note,
        url=page.url,
        final_url=page.final_url,
        http_status=page.http_status,
        engine=page.engine,
        browser=page.browser,
        raw=page.raw,
        raw_ext="raw.html",
        rendered_html=page.html if page.rendered and page.html else None,
        title=page.meta.get("title") if page.meta else None,
        warning=page.warning,
        html=page.html,
        tool=tool,
        redirects=list(page.redirects),
    )
    if page.outcome == "fetched":
        draft.main = main_markdown(page.meta, page.markdown, fallback_title)
        if structured:
            draft.structured = structured_data(page.html, page.final_url or page.url)
    return draft


def _web(url: str, session: Session, *, engine: str, timeout: float, structured: bool, max_bytes: int) -> _Draft:
    page = read_page(url, session, engine=engine, timeout=timeout, max_bytes=max_bytes)
    return _from_page(page, "web", url, structured)


def _europepmc(
    pmcid: str, session: Session, *, timeout: float, max_bytes: int, note_prefix: str = ""
) -> _Draft:
    url = europepmc.fulltext_url(pmcid)
    record = europepmc.record_for_pmcid(pmcid, timeout=timeout, limiter=session.limiter)
    if record is not None and not record.open_access:
        return _Draft(
            "blocked",
            "europepmc",
            f"{note_prefix}{pmcid} is not open access in Europe PMC; only open-access full text is fetched",
            url=url,
            final_url=url,
            title=record.title,
            engine="api",
        )
    response = europepmc.fulltext(pmcid, timeout=timeout, limiter=session.limiter, max_bytes=max_bytes)
    draft = _Draft(
        "failed", "europepmc", url=url, final_url=response.url, http_status=response.status, engine="api",
        tool="Europe PMC fullTextXML", raw_ext="raw.xml", redirects=list(response.history),
    )
    if response.status == 404:
        draft.outcome = "blocked"
        draft.note = f"{note_prefix}Europe PMC holds no open full text for {pmcid}"
        return draft
    if not response.ok:
        draft.note = f"{note_prefix}Europe PMC answered HTTP {response.status}"
        return draft
    try:
        meta, markdown = europepmc.jats_to_markdown(response.body)
    except ValueError as err:
        draft.note = f"{note_prefix}Europe PMC full text unusable: {err}"
        return draft
    draft.outcome = "fetched"
    draft.note = f"{note_prefix}open-access full text from Europe PMC".strip()
    draft.raw = response.body
    draft.main = markdown
    draft.title = meta.get("title") or None
    doi = meta.get("doi") or None
    draft.extra = {
        "pmcid": pmcid,
        "doi": doi,
        "citation_url": f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/PMC/{pmcid}",
    }
    return draft


def eurlex_url(celex: str) -> str:
    """The human-facing EUR-Lex page for a CELEX number, used in citations."""
    return f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"


def _cellar(celex: str, session: Session, *, timeout: float, max_bytes: int) -> _Draft:
    response = cellar.fetch_xhtml(celex, timeout=timeout, limiter=session.limiter, max_bytes=max_bytes)
    draft = _Draft(
        "failed", "cellar", url=cellar.celex_url(celex), final_url=response.url, http_status=response.status,
        engine="api", tool="EU CELLAR XHTML (eng)", raw_ext="raw.xhtml", redirects=list(response.history),
        extra={"celex": celex, "citation_url": eurlex_url(celex)},
    )
    if response.status == 404:
        draft.outcome = "blocked"
        draft.note = (
            f"CELLAR holds no English XHTML for {celex} (older acts may be PDF only, not fetched in this version)"
        )
        return draft
    if not response.ok:
        draft.note = f"CELLAR answered HTTP {response.status}"
        return draft
    try:
        meta, markdown = cellar.xhtml_to_markdown(response.body)
    except ValueError as err:
        draft.note = f"CELLAR answered HTTP {response.status} but the body is unusable: {err}"
        return draft
    draft.outcome = "fetched"
    draft.note = "English XHTML from EU CELLAR"
    draft.raw = response.body
    draft.main = markdown
    draft.title = meta.get("title") or None
    return draft


def _doi(
    doi: str, session: Session, *, engine: str, timeout: float, email: str | None, structured: bool, max_bytes: int
) -> _Draft:
    prefix = f"DOI {doi}: "
    record = europepmc.record_for_doi(doi, timeout=timeout, limiter=session.limiter)
    if record is not None and record.pmcid and record.open_access:
        draft = _europepmc(record.pmcid, session, timeout=timeout, max_bytes=max_bytes, note_prefix=prefix)
        draft.extra.setdefault("doi", doi)
        if draft.outcome != "blocked":
            return draft
    if not email:
        return _Draft(
            "blocked",
            "doi",
            f"{prefix}no open copy in Europe PMC; Unpaywall not consulted "
            f"(set --email or {EMAIL_VARIABLE} to enable it)",
            url=f"https://doi.org/{doi}",
            final_url=f"https://doi.org/{doi}",
            title=record.title if record else None,
            extra={"doi": doi},
        )
    copy = unpaywall.lookup(doi, email, timeout=timeout, limiter=session.limiter)
    lookup_url = unpaywall.lookup_url(doi)
    extra = {"doi": doi, "unpaywall": lookup_url, "citation_url": f"https://doi.org/{doi}"}
    if copy is None or not copy.is_oa or not copy.any_url:
        return _Draft(
            "blocked",
            "unpaywall",
            f"{prefix}no open copy found (Europe PMC and Unpaywall)",
            url=lookup_url,
            final_url=lookup_url,
            title=(copy.title if copy else None) or (record.title if record else None),
            engine="api",
            extra={"doi": doi},
        )
    extra.update({"license": copy.license, "version": copy.version})
    if copy.pdf_url:
        pdf = _open_pdf(copy.pdf_url, session, timeout=timeout, max_bytes=max_bytes)
        if pdf is not None:
            pdf.title = copy.title or (record.title if record else None)
            pdf.note = f"{prefix}{pdf.note}"
            pdf.extra = dict(extra)
            if pdf.outcome != "failed" or not copy.landing_url:
                return pdf
    if copy.landing_url:
        page = read_page(copy.landing_url, session, engine=engine, timeout=timeout, max_bytes=max_bytes)
        draft = _from_page(page, "web", copy.title or doi, structured)
        draft.note = f"{prefix}Unpaywall best open copy (landing page)" + (f"; {draft.note}" if draft.note else "")
        draft.title = draft.title or copy.title
        draft.extra.update(extra)
        return draft
    return _Draft(
        "blocked", "unpaywall", f"{prefix}no open copy found (Europe PMC and Unpaywall)", url=lookup_url,
        final_url=lookup_url, engine="api", extra={"doi": doi},
    )


def _open_pdf(url: str, session: Session, *, timeout: float, max_bytes: int) -> _Draft | None:
    """Download an open PDF named by Unpaywall, under robots.txt (first URL and every redirect). Returns
    None when the location is not a PDF at all (a landing page in disguise), so the caller can try the
    landing page instead."""
    decision = session.robots.check(url)
    if decision.crawl_delay:
        session.limiter.set_host_delay(url, decision.crawl_delay)
    if not decision.allowed:
        return _Draft("blocked", "unpaywall", f"robots.txt: {decision.reason}", url=url, final_url=url, engine="http")
    try:
        response = transport.get(
            url, accept="application/pdf,*/*;q=0.8", timeout=timeout, limiter=session.limiter,
            guard=session.robots.guard, max_bytes=max_bytes,
        )
    except transport.RedirectBlocked as err:
        return _Draft("blocked", "unpaywall", str(err), url=url, final_url=err.url, engine="http")
    except transport.TransportError as err:
        return _Draft("failed", "unpaywall", str(err), url=url, final_url=url, engine="http")
    draft = _Draft(
        "failed", "unpaywall", url=url, final_url=response.url, http_status=response.status, engine="http",
        tool="Unpaywall open PDF", raw_ext="raw.pdf", redirects=list(response.history),
    )
    is_pdf = response.body[:5] == b"%PDF-" or "application/pdf" in response.content_type
    body_text = "" if is_pdf else response.text()
    reason = detect.block_reason(response.status, body_text, "")
    if reason:
        draft.outcome, draft.note = "blocked", reason
        draft.raw = None if is_pdf else response.body
        draft.raw_ext = "raw.html"
        return draft
    if not response.ok:
        draft.note = f"HTTP {response.status}"
        return draft
    if not is_pdf:
        return None
    draft.outcome = "fetched"
    draft.raw = response.body
    draft.note = "open PDF saved as .raw.pdf; PDF text extraction is not included in this version, so no .main.md"
    return draft
