"""Live tests: reach the public internet or need a real browser. Deselected by default; run with `pytest -m live`."""

import pytest

from scrapology import browser, cellar, europepmc
from scrapology.fetch import fetch
from scrapology.session import Session

pytestmark = pytest.mark.live

STATIC_PAGE = "https://www.rfc-editor.org/rfc/rfc9309.html"
PMCID = "PMC10663573"
GDPR = "32016R0679"


def test_example_com_is_fetched_with_a_short_page_warning(tmp_path) -> None:
    result = fetch("https://example.com/", out_dir=str(tmp_path), session=Session(delay=0))
    assert result.outcome == "fetched", result.note
    assert result.warning and "short page" in result.warning
    assert result.title == "Example Domain"


def test_static_page_over_http_engine(tmp_path) -> None:
    result = fetch(STATIC_PAGE, out_dir=str(tmp_path), session=Session(delay=0))
    assert result.outcome == "fetched", result.note
    assert result.engine == "http" and "raw" in result.files
    assert "Robots Exclusion Protocol" in (result.title or "")


def test_static_page_over_browser_engine(tmp_path) -> None:
    result = fetch(STATIC_PAGE, out_dir=str(tmp_path), session=Session(delay=0), engine="browser")
    assert result.outcome == "fetched", result.note
    assert result.engine == "browser" and "rendered" in result.files and "raw" not in result.files


def test_europepmc_search_and_full_text() -> None:
    papers = europepmc.search("antimicrobial resistance surveillance", n=3)
    assert papers and all(p.title for p in papers)
    response = europepmc.fulltext(PMCID)
    assert response.ok and b"<article" in response.body[:5000]
    meta, markdown = europepmc.jats_to_markdown(response.body)
    assert meta["title"] and "## Abstract" in markdown


def test_cellar_gdpr() -> None:
    response = cellar.fetch_xhtml(GDPR, timeout=60)
    assert response.ok and b"2016/679" in response.body
    meta, markdown = cellar.xhtml_to_markdown(response.body)
    assert meta["title"].startswith("REGULATION (EU) 2016/679")
    assert "## Article 1" in markdown


def test_eli_link_maps_to_the_same_act(tmp_path) -> None:
    result = fetch("https://eur-lex.europa.eu/eli/reg/2016/679/oj", out_dir=str(tmp_path), session=Session(delay=0))
    assert result.outcome == "fetched" and result.route == "cellar"


def test_bundled_status_shape() -> None:
    status = browser.bundled_status()
    assert set(status) == {"browsers_path", "links_path", "package_dir", "expected", "present", "registered"}
    assert "chromium" in status["expected"]


def test_some_browser_launches() -> None:
    results = browser.probe_browsers()
    assert any(ok for _, ok, _ in results), results
