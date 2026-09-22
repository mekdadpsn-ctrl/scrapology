import pytest

from scrapology.cellar import xhtml_to_markdown
from scrapology.europepmc import clean_title, jats_to_markdown, paper_from_hit

JATS = b"""<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">
  <front>
    <journal-meta><journal-title-group><journal-title>Test Journal</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">10.1000/test.1</article-id>
      <title-group><article-title>Signal  and <italic>noise</italic></article-title></title-group>
      <contrib-group content-type="authors">
        <contrib><name><surname>Doe</surname><given-names>Jane</given-names></name></contrib>
        <contrib><name><surname>Roe</surname><given-names>Richard</given-names></name></contrib>
      </contrib-group>
      <contrib-group>
        <contrib contrib-type="editor"><name><surname>Editor</surname><given-names>Ed</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2021</year></pub-date>
      <abstract><p>First abstract paragraph.</p><p>Second abstract paragraph.</p></abstract>
    </article-meta>
  </front>
  <body>
    <sec><title>Introduction</title><p>Intro text.</p>
      <sec><title>Background</title><p>Nested text.</p>
        <list><list-item><p>one</p></list-item><list-item><p>two</p></list-item></list>
      </sec>
    </sec>
    <sec><title>Results</title>
      <table-wrap><label>Table 1</label><caption><p>Measurements</p></caption>
        <table><thead><tr><th>Sample</th><th>Value</th></tr></thead>
        <tbody><tr><td>A</td><td>2.1</td></tr></tbody></table>
      </table-wrap>
      <fig><label>Figure 1</label><caption><p>Photo of the setup.</p></caption></fig>
    </sec>
  </body>
</article>"""


def test_jats_metadata_and_authors() -> None:
    meta, markdown = jats_to_markdown(JATS)
    assert meta["title"] == "Signal and noise"
    assert meta["journal"] == "Test Journal"
    assert meta["year"] == "2021"
    assert meta["doi"] == "10.1000/test.1"
    assert meta["authors"] == "Doe Jane; Roe Richard"
    assert "Editor" not in markdown


def test_jats_body_structure() -> None:
    _, markdown = jats_to_markdown(JATS)
    lines = markdown.splitlines()
    assert lines[0] == "# Signal and noise"
    assert "Journal: Test Journal | Year: 2021 | DOI: 10.1000/test.1" in lines
    assert "Authors: Doe Jane; Roe Richard" in lines
    assert "## Abstract" in lines
    assert "First abstract paragraph." in lines and "Second abstract paragraph." in lines
    assert "## Introduction" in lines
    assert "### Background" in lines
    assert "- one" in lines and "- two" in lines
    assert "## Results" in lines
    assert "**Table 1 Measurements**" in lines
    assert "| Sample | Value |" in lines and "| A | 2.1 |" in lines
    assert "*Figure 1 Photo of the setup.*" in lines


def test_jats_without_body_or_authors() -> None:
    meta, markdown = jats_to_markdown(b"<article><front><article-meta></article-meta></front></article>")
    assert meta["title"] == ""
    assert markdown.startswith("# [title not found]")
    assert "Authors: [not found]" in markdown


def test_jats_rejects_empty_and_malformed_input() -> None:
    with pytest.raises(ValueError):
        jats_to_markdown(b"")
    with pytest.raises(ValueError):
        jats_to_markdown(b"<article><unclosed>")


def test_jats_parser_does_not_expand_entities() -> None:
    evil = (
        b'<?xml version="1.0"?><!DOCTYPE article [<!ENTITY big "a">]>'
        b"<article><front><article-meta><title-group><article-title>&big;</article-title></title-group>"
        b"</article-meta></front></article>"
    )
    try:
        meta, _ = jats_to_markdown(evil)
    except ValueError:
        return  # refusing the document is also acceptable
    assert "a" not in meta["title"] or meta["title"] in ("", "&big;")


def test_clean_title_strips_tags_and_entities() -> None:
    assert clean_title("Signal &amp; <i>noise</i>  in   data") == "Signal & noise in data"
    assert clean_title(None) == ""


def test_paper_from_hit() -> None:
    paper = paper_from_hit(
        {"pmcid": "PMC1", "pmid": "2", "doi": "10.1/x", "title": "T &amp; U", "authorString": "A B.",
         "journalTitle": "J", "pubYear": 2020, "isOpenAccess": "Y", "citedByCount": 3}
    )
    assert paper.pmcid == "PMC1" and paper.open_access and paper.year == "2020" and paper.title == "T & U"
    assert paper.to_dict()["cited_by"] == 3
    assert not paper_from_hit({"title": "x"}).open_access


XHTML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML//EN" "xhtml-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>L_2000001EN.01000101.xml</title></head>
<body>
<p class="oj-doc-ti">REGULATION (EU) 2000/1 OF THE EUROPEAN PARLIAMENT</p>
<p class="oj-doc-ti">of 1 January 2000</p>
<p class="oj-doc-ti">on testing procedures</p>
<p class="oj-doc-ti">(Text with EEA relevance)</p>
<p class="oj-normal">THE EUROPEAN PARLIAMENT,</p>
<p class="oj-ti-section-1">CHAPTER I</p>
<p class="ti-art">Article 1</p>
<p class="oj-sti-art">Subject matter</p>
<p>This Regulation lays down rules.</p>
<ol><li>First point.</li><li>Second point.</li></ol>
<p class="ti-grseq-1">ANNEX I</p>
<table>
<tr><td><p>A1</p></td><td><p>Alpha</p></td></tr>
<tr><td><p>A2</p></td><td><p>Beta</p></td></tr>
<tr><td></td><td></td></tr>
</table>
<p>   </p>
</body></html>"""


def test_xhtml_headings_paragraphs_and_tables() -> None:
    meta, markdown = xhtml_to_markdown(XHTML)
    assert meta["title"] == "REGULATION (EU) 2000/1 OF THE EUROPEAN PARLIAMENT of 1 January 2000 on testing procedures"
    lines = markdown.splitlines()
    assert lines[0] == "# " + meta["title"]
    assert lines.count("# " + meta["title"]) == 1
    assert "(Text with EEA relevance)" in lines
    assert "THE EUROPEAN PARLIAMENT," in lines
    assert "## CHAPTER I" in lines
    assert "## Article 1" in lines
    assert "### Subject matter" in lines
    assert "This Regulation lays down rules." in lines
    assert "First point." in lines and "Second point." in lines
    assert "## ANNEX I" in lines
    assert "| A1 | Alpha |" in lines and "| A2 | Beta |" in lines


def test_xhtml_title_falls_back_to_title_element() -> None:
    meta, markdown = xhtml_to_markdown(b"<html><head><title>Plain title</title></head><body><p>x</p></body></html>")
    assert meta["title"] == "Plain title" and markdown.startswith("# Plain title")
    meta, _ = xhtml_to_markdown(b"<html><body><p>x</p></body></html>")
    assert meta["title"] == ""


def test_xhtml_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        xhtml_to_markdown(b"")
    with pytest.raises(ValueError):
        xhtml_to_markdown(b"   \n")


def test_xhtml_cell_text_is_not_repeated_as_paragraphs() -> None:
    _, markdown = xhtml_to_markdown(XHTML)
    assert markdown.count("Alpha") == 1
    assert markdown.count("A2") == 1
    assert "|  |  |" not in markdown
