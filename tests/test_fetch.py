"""fetch() end to end against the fake network: outcomes, files, names and the receipt."""

import hashlib
import importlib.util
import json
import os
import sys

import pytest

from conftest import ARTICLE_HTML, CHALLENGE_HTML, LONG_TEXT, SCRIPTED_STUB_HTML, STUB_HTML, FakeNet
from scrapology import europepmc, provenance, transport, web
from scrapology.fetch import check_options, fetch
from scrapology.session import Session

ORIGIN = "https://site.test"
PAGE = f"{ORIGIN}/article"
EPMC = europepmc.EPMC
EMAIL = "someone" + chr(64) + "example.org"  # built at run time so no literal address sits in the repository


def net_response(url: str, status: int, body: str, content_type: str = "application/json") -> transport.Response:
    return transport.Response(status, url, body.encode(), {"content-type": content_type})


def _records(out: str) -> list[dict]:
    return provenance.read_records(out)


def test_web_fetched_writes_files_and_receipt(net: FakeNet, session: Session, tmp_path, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 200, ARTICLE_HTML)
    out = str(tmp_path)
    result = fetch(PAGE, out_dir=out, session=session, structured=True)
    assert result.outcome == "fetched" and result.exit_code == 0 and result.ok
    assert result.engine == "http" and result.browser is None and result.http_status == 200
    assert result.title == "A real article" and result.warning is None
    assert result.name == "site.test-article"
    assert no_browser == []
    raw, main, structured = result.files["raw"], result.files["main"], result.files["structured"]
    assert os.path.basename(raw) == "site.test-article.raw.html"
    assert "rendered" not in result.files
    assert open(raw, "rb").read() == ARTICLE_HTML.encode()
    text = open(main, encoding="utf-8").read()
    assert text.startswith(f"<!-- source: {PAGE} | url: {PAGE} | accessed: ")
    assert "# A real article" in text and "Author: Jane Doe" in text and "Site: site.test" in text
    assert "## Second part" in text
    data = json.load(open(structured, encoding="utf-8"))
    assert data["source"] == PAGE
    assert data["data"]["opengraph"][0]["og:title"] == "A real article"
    (record,) = _records(out)
    assert record["outcome"] == "fetched" and record["route"] == "web" and record["engine"] == "http"
    assert record["name"] == "site.test-article" and record["redirects"] == []
    assert record["sha256_raw"] == hashlib.sha256(ARTICLE_HTML.encode()).hexdigest()
    assert record["sha256_rendered"] is None
    assert record["files"]["main"] == main
    assert record["tool"].startswith("scrapology ") and "trafilatura" in record["versions"]
    assert net.calls == [f"{ORIGIN}/robots.txt", PAGE]


def test_robots_disallow_is_blocked_before_any_page_request(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN, "User-agent: *\nDisallow: /article\n")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and result.exit_code == 3
    assert "robots.txt" in result.note and result.files == {}
    assert net.calls == [f"{ORIGIN}/robots.txt"]


def test_robots_wildcard_is_applied(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN, "User-agent: *\nDisallow: /art*\n")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and net.calls == [f"{ORIGIN}/robots.txt"]


def test_robots_429_is_treated_as_unreachable(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN, "slow down", status=429)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and "429" in result.note


def test_crawl_delay_cap_is_noted(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN, "User-agent: *\nCrawl-delay: 900\n")
    net.add(PAGE, 200, ARTICLE_HTML)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "fetched" and "capped at 60" in result.note
    assert session.limiter.delay_for(PAGE) == 60.0


def test_redirect_refused_by_robots_is_blocked(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN)
    net.routes[PAGE] = transport.RedirectBlocked("https://site.test/secret?k=1", "disallowed by robots.txt")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and result.final_url == "https://site.test/secret"
    assert "refused" in result.note and result.files == {}


def test_http_403_is_blocked_and_never_rendered(net: FakeNet, session: Session, tmp_path, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 403, "<html><body>Forbidden</body></html>")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and "HTTP 403" in result.note
    assert no_browser == []
    assert "raw" in result.files and "main" not in result.files


def test_challenge_page_is_blocked_even_with_200(net: FakeNet, session: Session, tmp_path, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 200, CHALLENGE_HTML)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and "challenge" in result.note
    assert no_browser == []


def test_http_404_is_failed(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 404, "<html><body>gone</body></html>")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "failed" and result.exit_code == 1 and result.note == "HTTP 404"
    assert result.files == {}


def test_network_error_and_oversized_body_are_failed(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN)
    net.routes[PAGE] = transport.NetworkError("https://site.test/article: OSError: name not resolved")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "failed" and "name not resolved" in result.note
    net.routes[PAGE] = transport.ResponseTooLarge("https://site.test/article: response exceeds the 1000 byte limit")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, max_bytes=1000)
    assert result.outcome == "failed" and "1000 byte limit" in result.note


def test_short_static_page_warns_without_a_browser(net: FakeNet, session: Session, tmp_path, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 200, STUB_HTML)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "fetched" and result.warning and "short page" in result.warning
    assert result.engine == "http" and no_browser == []


def test_thin_scripted_page_is_rendered_to_rendered_html(net: FakeNet, session: Session, tmp_path, fake_browser):
    net.robots(ORIGIN)
    net.add(PAGE, 200, SCRIPTED_STUB_HTML)
    rendered = fake_browser(ARTICLE_HTML)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert rendered == [PAGE]
    assert result.outcome == "fetched" and result.engine == "browser" and result.browser == "fake browser"
    assert result.warning is None and "rendered in a browser" in result.note
    assert "raw" not in result.files
    assert os.path.basename(result.files["rendered"]) == "site.test-article.rendered.html"
    assert open(result.files["rendered"], encoding="utf-8").read() == ARTICLE_HTML
    (record,) = _records(str(tmp_path))
    assert record["sha256_raw"] is None
    assert record["sha256_rendered"] == hashlib.sha256(ARTICLE_HTML.encode()).hexdigest()


def test_engine_http_never_renders(net: FakeNet, session: Session, tmp_path, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 200, SCRIPTED_STUB_HTML)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="http")
    assert result.outcome == "fetched" and result.engine == "http" and no_browser == []


def test_engine_browser_skips_the_plain_get(net: FakeNet, session: Session, tmp_path, fake_browser) -> None:
    net.robots(ORIGIN)
    rendered = fake_browser(ARTICLE_HTML)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="browser")
    assert rendered == [PAGE] and result.engine == "browser" and result.outcome == "fetched"
    assert net.calls == [f"{ORIGIN}/robots.txt"]


def test_browser_landing_on_a_disallowed_url_is_blocked(net: FakeNet, session: Session, tmp_path, fake_browser):
    net.robots(ORIGIN, "User-agent: *\nDisallow: /secret\n")
    fake_browser(ARTICLE_HTML, final_url=f"{ORIGIN}/secret")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="browser")
    assert result.outcome == "blocked" and "refused" in result.note and result.files == {}
    assert result.final_url == f"{ORIGIN}/secret"


def test_browser_redirect_history_is_recorded(net: FakeNet, session: Session, tmp_path, fake_browser) -> None:
    net.robots(ORIGIN)
    fake_browser(ARTICLE_HTML, final_url=f"{ORIGIN}/moved")
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="browser")
    assert result.outcome == "fetched" and result.final_url == f"{ORIGIN}/moved" and result.redirects == [PAGE]


def test_browser_block_is_blocked(net: FakeNet, session: Session, tmp_path, fake_browser) -> None:
    net.robots(ORIGIN)
    fake_browser(CHALLENGE_HTML, status=503)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="browser")
    assert result.outcome == "blocked" and "503" in result.note
    assert "rendered" in result.files and "main" not in result.files


def test_no_usable_browser_is_failed(net: FakeNet, session: Session, tmp_path, no_browser) -> None:
    net.robots(ORIGIN)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="browser")
    assert result.outcome == "failed" and "no usable browser" in result.note


def test_render_warning_reaches_the_result_and_receipt(net: FakeNet, session: Session, tmp_path, fake_browser):
    net.robots(ORIGIN)
    warning = "navigation to https://site.test/secret refused (disallowed); the page loaded before it is kept"
    fake_browser(ARTICLE_HTML, warning=warning)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session, engine="browser")
    assert result.outcome == "fetched" and result.warning and "site.test/secret" in result.warning
    (record,) = _records(str(tmp_path))
    assert record["outcome"] == "fetched" and "refused" in record["warning"]


# ---------- argument validation and safety ----------


def test_bad_options_raise_before_any_network_work(net: FakeNet) -> None:
    with pytest.raises(ValueError):
        fetch("https://x.test/", engine="magic")
    with pytest.raises(ValueError):
        fetch("https://x.test/", timeout=0)
    with pytest.raises(ValueError):
        fetch("https://x.test/", delay=-1)
    with pytest.raises(ValueError):
        fetch("https://x.test/", max_bytes=0)
    with pytest.raises(ValueError):
        check_options(engine="nope")
    assert net.calls == []


def test_structured_without_extruct_is_a_value_error(monkeypatch, net: FakeNet) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "extruct" else real(name))
    with pytest.raises(ValueError) as exc:
        fetch("https://x.test/", structured=True)
    assert "structured" in str(exc.value) and net.calls == []


@pytest.mark.parametrize(
    "name",
    ["../escape", "..", "sub/dir", "sub\\dir", "a:b", "con", "CON.txt", "lpt1", "-leading", ".hidden", "x" * 121, ""],
)
def test_bad_names_are_rejected(net: FakeNet, session: Session, tmp_path, name: str) -> None:
    with pytest.raises(ValueError):
        fetch(PAGE, out_dir=str(tmp_path), name=name, session=session)
    assert net.calls == []


def test_absolute_name_is_rejected(net: FakeNet, session: Session, tmp_path) -> None:
    with pytest.raises(ValueError):
        fetch(PAGE, out_dir=str(tmp_path), name=str(tmp_path / "abs"), session=session)


def test_refetch_never_overwrites_and_keeps_the_first_receipt(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 200, ARTICLE_HTML)
    first = fetch(PAGE, out_dir=str(tmp_path), session=session)
    net.add(PAGE, 200, ARTICLE_HTML.replace("A real article", "Changed article"))
    second = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert first.name == "site.test-article"
    assert second.name == f"site.test-article-{provenance.short_hash(PAGE)}"
    assert open(first.files["raw"], "rb").read() == ARTICLE_HTML.encode()
    records = _records(str(tmp_path))
    assert records[0]["sha256_raw"] == hashlib.sha256(ARTICLE_HTML.encode()).hexdigest()
    assert records[1]["sha256_raw"] != records[0]["sha256_raw"]
    third = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert third.name == f"site.test-article-{provenance.short_hash(PAGE)}-2"


def test_explicit_name_collision_gets_a_suffix_case_insensitively(net: FakeNet, session: Session, tmp_path) -> None:
    net.robots(ORIGIN)
    net.add(PAGE, 200, ARTICLE_HTML)
    (tmp_path / "Custom.main.md").write_text("existing", encoding="utf-8")
    result = fetch(PAGE, out_dir=str(tmp_path), name="custom", session=session)
    assert result.name.startswith("custom-") and (tmp_path / "Custom.main.md").read_text(encoding="utf-8") == "existing"
    assert result.files["main"] == os.path.normpath(result.files["main"])


def test_unexpected_exception_is_recorded_as_failed(net: FakeNet, session: Session, tmp_path, monkeypatch) -> None:
    net.robots(ORIGIN)

    def boom(*_: object, **__: object) -> web.PageResult:
        raise RuntimeError("kaput")

    monkeypatch.setattr(sys.modules["scrapology.fetch"], "read_page", boom)
    result = fetch(PAGE, out_dir=str(tmp_path), session=session)
    assert result.outcome == "failed" and "RuntimeError" in result.note and "kaput" in result.note
    (record,) = _records(str(tmp_path))
    assert record["outcome"] == "failed"


# ---------- Europe PMC ----------


def _epmc_search(pmcid: str = "PMC1", oa: str = "Y", doi: str = "10.1000/x", hits: int = 1) -> str:
    result = [{"pmcid": pmcid, "doi": doi, "title": "Paper &amp; title", "isOpenAccess": oa, "pubYear": 2021}]
    return json.dumps({"hitCount": hits, "resultList": {"result": result if hits else []}})


JATS = (
    "<article><front><article-meta><title-group><article-title>Paper title</article-title></title-group>"
    '<article-id pub-id-type="doi">10.1000/x</article-id></article-meta></front>'
    "<body><sec><title>Intro</title><p>Body text.</p></sec></body></article>"
)


def test_pmc_fetched(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search())
    net.add(europepmc.fulltext_url("PMC1"), 200, JATS, "application/xml")
    result = fetch("pmc:PMC1", out_dir=str(tmp_path), session=session)
    assert result.outcome == "fetched" and result.route == "europepmc" and result.title == "Paper title"
    assert os.path.basename(result.files["raw"]) == "pmc-PMC1.raw.xml"
    assert open(result.files["raw"], "rb").read() == JATS.encode()
    text = open(result.files["main"], encoding="utf-8").read()
    assert "route: europepmc" in text and "## Intro" in text and "Body text." in text
    (record,) = _records(str(tmp_path))
    assert record["pmcid"] == "PMC1" and record["doi"] == "10.1000/x"
    assert record["citation_url"] == "https://doi.org/10.1000/x"


def test_pmc_not_open_access_is_blocked(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(oa="N"))
    result = fetch("pmc:PMC1", out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and "not open access" in result.note
    assert europepmc.fulltext_url("PMC1") not in net.calls


def test_pmc_without_full_text_is_blocked(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search())
    net.add(europepmc.fulltext_url("PMC1"), 404, "", "text/plain")
    result = fetch("pmc:PMC1", out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and "no open full text" in result.note


def test_pmc_malformed_or_empty_xml_is_failed(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search())
    net.add(europepmc.fulltext_url("PMC1"), 200, "<article><unclosed>", "application/xml")
    result = fetch("pmc:PMC1", out_dir=str(tmp_path), session=session)
    assert result.outcome == "failed" and "unusable" in result.note
    net.add(europepmc.fulltext_url("PMC1"), 200, "", "application/xml")
    result = fetch("pmc:PMC1", out_dir=str(tmp_path), session=session)
    assert result.outcome == "failed" and "empty" in result.note


def test_pmc_zero_hits_retries_once_then_tries_full_text(net: FakeNet, session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(europepmc, "ZERO_HIT_PAUSE", 0)
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(hits=0))
    net.add(europepmc.fulltext_url("PMC1"), 200, JATS, "application/xml")
    result = fetch("pmc:PMC1", out_dir=str(tmp_path), session=session)
    assert result.outcome == "fetched"
    assert sum(1 for c in net.calls if "/search?" in c) == 2


# ---------- CELLAR ----------

XHTML = (
    "<html><head><title>Regulation (EU) 2016/679</title></head>"
    "<body><p class='ti-art'>Article 1</p><p>Rules.</p></body></html>"
)
CELEX_URL = "https://publications.europa.eu/resource/celex/32016R0679"


def test_celex_fetched(net: FakeNet, session: Session, tmp_path) -> None:
    net.add(CELEX_URL, 200, XHTML, "application/xhtml+xml")
    result = fetch("https://eur-lex.europa.eu/eli/reg/2016/679/oj", out_dir=str(tmp_path), session=session)
    assert result.outcome == "fetched" and result.route == "cellar"
    assert result.title == "Regulation (EU) 2016/679"
    assert result.files["raw"].endswith(".raw.xhtml")
    assert "## Article 1" in open(result.files["main"], encoding="utf-8").read()
    (record,) = _records(str(tmp_path))
    assert record["celex"] == "32016R0679"
    assert record["citation_url"] == "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679"


def test_celex_without_xhtml_is_blocked(net: FakeNet, session: Session, tmp_path) -> None:
    net.add("https://publications.europa.eu/resource/celex/31970R0001", 404, "", "text/plain")
    result = fetch("celex:31970R0001", out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and "PDF only" in result.note
    assert net.calls.count("https://publications.europa.eu/resource/celex/31970R0001") == 2


def test_celex_empty_body_is_failed(net: FakeNet, session: Session, tmp_path) -> None:
    net.add(CELEX_URL, 200, "", "application/xhtml+xml")
    result = fetch("celex:32016R0679", out_dir=str(tmp_path), session=session)
    assert result.outcome == "failed" and "empty" in result.note and result.files == {}


# ---------- DOI ----------

DOI = "10.1002/anie.202214722"
UNPAYWALL = f"https://api.unpaywall.org/v2/{DOI}?email=someone%40example.org"


def test_doi_with_open_copy_in_europe_pmc(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(doi=DOI))
    net.add(europepmc.fulltext_url("PMC1"), 200, JATS, "application/xml")
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session)
    assert result.outcome == "fetched" and result.route == "europepmc" and f"DOI {DOI}" in result.note


def test_doi_without_email_is_blocked(net: FakeNet, session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCRAPOLOGY_EMAIL", raising=False)
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(oa="N", doi=DOI))
    result = fetch(f"https://onlinelibrary.wiley.com/doi/{DOI}", out_dir=str(tmp_path), session=session)
    assert result.outcome == "blocked" and result.exit_code == 3
    assert "no open copy" in result.note and "SCRAPOLOGY_EMAIL" in result.note
    assert not any("unpaywall" in c for c in net.calls)


def test_doi_unpaywall_no_copy(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(hits=0))
    net.add(UNPAYWALL, 200, json.dumps({"doi": DOI, "is_oa": False, "best_oa_location": None}), "application/json")
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session, email=EMAIL)
    assert result.outcome == "blocked" and "Unpaywall" in result.note and result.route == "unpaywall"
    (record,) = _records(str(tmp_path))
    assert "email=" not in json.dumps(record) and EMAIL not in json.dumps(record)


def test_doi_unpaywall_network_error_never_leaks_the_email(net: FakeNet, session: Session, tmp_path) -> None:
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(hits=0))
    net.routes[UNPAYWALL] = transport.NetworkError(f"{UNPAYWALL}: OSError: reset")
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session, email=EMAIL)
    assert result.outcome == "failed"
    dumped = json.dumps(_records(str(tmp_path))[0]) + result.note
    assert "email=" not in dumped and EMAIL not in dumped and "example.org" not in dumped


def test_doi_unpaywall_pdf_saved(net: FakeNet, session: Session, tmp_path) -> None:
    pdf_url = "https://repo.test/paper.pdf"
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(hits=0))
    net.add(
        UNPAYWALL, 200,
        json.dumps({"doi": DOI, "is_oa": True, "title": "Open paper",
                    "best_oa_location": {"url_for_pdf": pdf_url, "url_for_landing_page": "https://repo.test/paper",
                                         "license": "cc-by", "version": "publishedVersion"}}),
        "application/json",
    )
    net.robots("https://repo.test")
    net.add(pdf_url, 200, b"%PDF-1.4 fake", "application/pdf")
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session, email=EMAIL)
    assert result.outcome == "fetched" and result.route == "unpaywall" and result.title == "Open paper"
    assert result.files["raw"].endswith(".raw.pdf") and "main" not in result.files
    assert "PDF text extraction is not included" in result.note
    (record,) = _records(str(tmp_path))
    assert record["license"] == "cc-by" and record["sha256_raw"] == hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
    assert record["citation_url"] == f"https://doi.org/{DOI}"


def test_doi_unpaywall_pdf_under_robots(net: FakeNet, session: Session, tmp_path) -> None:
    pdf_url = "https://repo.test/files/paper.pdf"
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(hits=0))
    net.add(
        UNPAYWALL, 200,
        json.dumps({"doi": DOI, "is_oa": True, "best_oa_location": {"url_for_pdf": pdf_url}}), "application/json",
    )
    net.robots("https://repo.test", "User-agent: *\nDisallow: /*.pdf$\n")
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session, email=EMAIL)
    assert result.outcome == "blocked" and "robots.txt" in result.note and pdf_url not in net.calls


def test_doi_unpaywall_landing_page_goes_through_web_route(net: FakeNet, session: Session, tmp_path) -> None:
    landing = "https://repo.test/paper"
    net.routes[f"{EPMC}/search?*"] = lambda url: net_response(url, 200, _epmc_search(hits=0))
    net.add(
        UNPAYWALL, 200,
        json.dumps({"doi": DOI, "is_oa": True, "title": "Open paper",
                    "best_oa_location": {"url_for_pdf": None, "url_for_landing_page": landing}}),
        "application/json",
    )
    net.robots("https://repo.test", "User-agent: *\nDisallow: /paper\n")
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session, email=EMAIL)
    assert result.outcome == "blocked" and "robots.txt" in result.note and result.route == "web"
    net.robots("https://repo.test")
    session2 = Session(delay=0)
    net.add(landing, 200, ARTICLE_HTML)
    result = fetch(f"doi:{DOI}", out_dir=str(tmp_path), session=session2, email=EMAIL)
    assert result.outcome == "fetched" and result.route == "web" and "Unpaywall best open copy" in result.note
    assert LONG_TEXT[:40] in open(result.files["main"], encoding="utf-8").read()
