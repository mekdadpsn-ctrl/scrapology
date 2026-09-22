"""`scrapology cite`: a numbered markdown source list from sources.jsonl, fetched items only, each once."""

from typing import Any

from scrapology import provenance


def citation_url(record: dict[str, Any]) -> str:
    """The URL a reader should follow: a DOI or EUR-Lex link for the API routes, else the final page URL."""
    return record.get("citation_url") or record.get("final_url") or record.get("url") or record.get("target") or ""


def citation_lines(records: list[dict[str, Any]]) -> list[str]:
    """One numbered line per fetched source. A source fetched more than once appears once, at its first fetch."""
    lines: list[str] = []
    seen: set[str] = set()
    for record in records:
        if record.get("outcome") != "fetched":
            continue
        url = citation_url(record)
        if url in seen:
            continue
        seen.add(url)
        title = (record.get("title") or "").strip() or record.get("target") or "[untitled]"
        accessed = (record.get("accessed_utc") or "")[:10] or "[unknown date]"
        route = record.get("route") or "web"
        lines.append(f"{len(lines) + 1}. {title}. {url}. Accessed {accessed} ({route}).")
    return lines


def cite(out_dir: str) -> str:
    """The list as one string, or an explanatory line when there is nothing to cite."""
    records = provenance.read_records(out_dir)
    lines = citation_lines(records)
    if not lines:
        if not records:
            return f"no {provenance.SOURCES_FILE} in {out_dir}"
        return "no fetched sources to cite"
    return "\n".join(lines)
