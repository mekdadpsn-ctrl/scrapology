from scrapology import provenance
from scrapology.cite import citation_lines, cite


def test_citation_lines_number_fetched_items_only() -> None:
    records = [
        {"outcome": "fetched", "title": "First", "final_url": "https://a.test/1",
         "accessed_utc": "2026-09-22T06:00:00+00:00", "route": "web"},
        {"outcome": "blocked", "title": "Nope", "url": "https://b.test/", "accessed_utc": "2026-09-22T06:00:00+00:00"},
        {"outcome": "fetched", "title": "", "target": "pmc:PMC1", "url": "https://api.test/PMC1",
         "citation_url": "https://doi.org/10.1/x", "accessed_utc": "2026-09-21T23:59:59+00:00", "route": "europepmc"},
        {"outcome": "fetched", "title": "First again", "final_url": "https://a.test/1",
         "accessed_utc": "2026-09-23T06:00:00+00:00", "route": "web"},
    ]
    lines = citation_lines(records)
    assert lines == [
        "1. First. https://a.test/1. Accessed 2026-09-22 (web).",
        "2. pmc:PMC1. https://doi.org/10.1/x. Accessed 2026-09-21 (europepmc).",
    ]


def test_cite_reads_folder(tmp_path) -> None:
    out = str(tmp_path)
    assert cite(out).startswith("no sources.jsonl")
    provenance.append_record(out, {"outcome": "blocked", "target": "x"})
    assert cite(out) == "no fetched sources to cite"
    provenance.append_record(
        out,
        {"outcome": "fetched", "title": "T", "final_url": "https://t.test/",
         "accessed_utc": "2026-01-02T00:00:00+00:00"},
    )
    assert cite(out) == "1. T. https://t.test/. Accessed 2026-01-02 (web)."
