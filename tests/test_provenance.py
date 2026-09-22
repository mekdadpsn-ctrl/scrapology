import hashlib
import json
import os

import pytest

from scrapology import provenance
from scrapology._version import __version__


@pytest.mark.parametrize(
    "target, expected",
    [
        ("https://example.com/", "example.com"),
        ("https://www.rfc-editor.org/rfc/rfc9309.html", "www.rfc-editor.org-rfc-rfc9309.html"),
        ("pmc:PMC10663573", "pmc-PMC10663573"),
        ("doi:10.1038/s41598-023-47754-w", "doi-10.1038-s41598-023-47754-w"),
        ("celex:32016R0679", "celex-32016R0679"),
        ("https://www.google.com/search?q=x&hl=en#top", "www.google.com-search-q-x-hl-en"),
        ("///", "target"),
        ("https://con.test/", "target-con.test"),
        ("https://h.test/con", "h.test-con"),
    ],
)
def test_slugify(target: str, expected: str) -> None:
    assert provenance.slugify(target) == expected
    provenance.validate_name(provenance.slugify(target))


def test_slugify_truncation_adds_a_hash() -> None:
    long_a = "https://h.test/" + "a" * 500 + "1"
    long_b = "https://h.test/" + "a" * 500 + "2"
    slug_a, slug_b = provenance.slugify(long_a), provenance.slugify(long_b)
    assert len(slug_a) <= 80 and len(slug_b) <= 80
    assert slug_a != slug_b
    assert slug_a.endswith("-" + provenance.short_hash(long_a))


@pytest.mark.parametrize("name", ["page", "a.b-c_d", "X1", "pmc-PMC1", "x" * 120])
def test_validate_name_accepts(name: str) -> None:
    assert provenance.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "../x", "..", "a/b", "a\\b", "a:b", "-x", ".x", "x.", "con", "CON", "con.txt", "Prn.md", "aux", "nul",
     "com1", "COM9.x", "lpt1", "lpt9.md", "x" * 121, "a b", "aé", "a*b"],
)
def test_validate_name_rejects(name: str) -> None:
    with pytest.raises(ValueError):
        provenance.validate_name(name)


def test_unique_name_is_case_insensitive_and_hash_suffixed() -> None:
    taken = {"x"}
    assert provenance.unique_name("X", taken, "https://t.test/") == f"X-{provenance.short_hash('https://t.test/')}"
    assert provenance.unique_name("x", taken, "https://t.test/") == f"x-{provenance.short_hash('https://t.test/')}-2"
    assert provenance.unique_name("free", taken, "https://t.test/") == "free"
    assert "free" in taken and len(provenance.unique_name("y" * 120, {"y" * 120}, "t")) <= 120


def test_existing_names_reads_every_output_suffix(tmp_path) -> None:
    for name in ("One.raw.html", "two.main.md", "three.rendered.html", "four.structured.json",
                 "six.raw.pdf", "seven.raw.xml", "eight.raw.xhtml", "sources.jsonl", "unrelated.txt"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert provenance.existing_names(str(tmp_path)) == {"one", "two", "three", "four", "six", "seven", "eight"}
    assert provenance.existing_names(str(tmp_path / "missing")) == set()


def test_output_path_stays_inside_out_dir(tmp_path) -> None:
    path = provenance.output_path(str(tmp_path), "page", ".main.md")
    assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(str(tmp_path))
    with pytest.raises(ValueError):
        provenance.output_path(str(tmp_path), "../page", ".main.md")


def test_header_fields() -> None:
    line = provenance.header("pmc:PMC1", "https://api.test/PMC1", "2026-09-22T06:00:00+00:00", "europepmc", "tool 1")
    assert line.startswith("<!-- source: pmc:PMC1 | url: https://api.test/PMC1 | accessed: 2026-09-22T06:00:00+00:00")
    assert "route: europepmc" in line and "tool: tool 1" in line and line.endswith("-->\n\n")


def test_utc_now_format() -> None:
    stamp = provenance.utc_now()
    assert stamp.endswith("+00:00") and len(stamp) == 25


def test_sha256_file(tmp_path) -> None:
    path = tmp_path / "raw.bin"
    path.write_bytes(b"hello receipt")
    assert provenance.sha256_file(str(path)) == hashlib.sha256(b"hello receipt").hexdigest()


def test_versions_include_tool_and_deps() -> None:
    versions = provenance.versions()
    assert versions["scrapology"] == __version__
    assert "trafilatura" in versions and "lxml" in versions and "playwright" in versions


def test_new_record_fields() -> None:
    record = provenance.new_record("https://x.test/", "web", "2026-09-22T06:00:00+00:00", "x.test")
    for key in (
        "target", "name", "url", "final_url", "redirects", "route", "engine", "browser", "http_status",
        "accessed_utc", "tool", "versions", "outcome", "note", "warning", "title", "files", "sha256_raw",
        "sha256_rendered",
    ):
        assert key in record
    assert record["tool"] == f"scrapology {__version__}" and record["name"] == "x.test"


def test_append_and_read_records(tmp_path) -> None:
    out = str(tmp_path / "out")
    provenance.append_record(out, {"target": "a", "outcome": "fetched"})
    provenance.append_record(out, {"target": "b", "outcome": "blocked"})
    with open(os.path.join(out, provenance.SOURCES_FILE), "a", encoding="utf-8") as handle:
        handle.write("not json\n\n[1, 2]\n")
    records = provenance.read_records(out)
    assert [r["target"] for r in records] == ["a", "b"]
    assert provenance.read_records(str(tmp_path / "missing")) == []
    with open(os.path.join(out, provenance.SOURCES_FILE), encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    assert first == {"target": "a", "outcome": "fetched"}
