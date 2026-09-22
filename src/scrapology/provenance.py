"""The receipt: file names, headers, the sources.jsonl record, hashes and versions.

Also the public helper for plugins: `record(...)` writes one receipt line with the same fields and the
same hashing as the built-in routes.
"""

import datetime
import hashlib
import importlib.metadata
import json
import os
import re
from typing import Any

from scrapology._version import __version__

DEPENDENCIES = ("playwright", "trafilatura", "lxml", "extruct")
SOURCES_FILE = "sources.jsonl"
OUTPUT_SUFFIXES = (
    ".raw.html",
    ".raw.xml",
    ".raw.xhtml",
    ".raw.pdf",
    ".rendered.html",
    ".main.md",
    ".structured.json",
)
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)
MAX_SLUG = 80
HASH_CHARS = 8


def utc_now() -> str:
    """ISO 8601 UTC timestamp to the second, for example 2026-09-22T10:15:30+00:00."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def versions() -> dict[str, str]:
    """Installed versions of the tool and its dependencies (missing optional ones are left out)."""
    out = {"scrapology": __version__}
    for name in DEPENDENCIES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return out


def header(target: str, url: str, accessed_utc: str, route: str, tool: str) -> str:
    """Comment line at the top of every derived text file."""
    return f"<!-- source: {target} | url: {url} | accessed: {accessed_utc} | route: {route} | tool: {tool} -->\n\n"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:HASH_CHARS]


# ---------- names ----------


def validate_name(name: str) -> str:
    """A safe base name: letters, digits, dot, underscore and hyphen, starting with a letter or digit,
    at most 120 characters, no Windows reserved device name, no trailing dot."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ValueError(
            f"invalid name {name!r}: use letters, digits, '.', '_' and '-', start with a letter or digit, "
            "at most 120 characters"
        )
    if name.endswith("."):
        raise ValueError(f"invalid name {name!r}: must not end with a dot")
    if name.split(".", 1)[0].lower() in RESERVED_NAMES:
        raise ValueError(f"invalid name {name!r}: reserved device name on Windows")
    return name


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(target: str, max_length: int = MAX_SLUG) -> str:
    """A file-name-safe slug of a target: scheme dropped, unsafe runs replaced by a hyphen. A slug that
    had to be cut to `max_length` ends in a short hash of the whole target so cut slugs stay distinct."""
    text = target.strip()
    for prefix in ("pmc:", "doi:", "celex:"):
        if text.lower().startswith(prefix):
            text = prefix[:-1] + "-" + text[len(prefix) :]
            break
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)
    text = text.split("#", 1)[0]
    text = _UNSAFE.sub("-", text).strip("-.")
    text = re.sub(r"-{2,}", "-", text)
    if not text or not text[0].isalnum():
        text = "target-" + text if text else "target"
    if len(text) > max_length:
        text = text[: max_length - HASH_CHARS - 1].rstrip("-.") + "-" + short_hash(target)
    if text.split(".", 1)[0].lower() in RESERVED_NAMES:
        text = "target-" + text
    return text.rstrip(".")


def existing_names(out_dir: str) -> set[str]:
    """Case-folded base names that already own output files in `out_dir`."""
    taken: set[str] = set()
    try:
        entries = os.listdir(out_dir)
    except OSError:
        return taken
    for entry in entries:
        for suffix in OUTPUT_SUFFIXES:
            if entry.endswith(suffix):
                taken.add(entry[: -len(suffix)].casefold())
                break
    return taken


def unique_name(name: str, taken: set[str], target: str) -> str:
    """`name` if free, else `name-<hash of target>`, else `name-<hash>-2`, `-3`... Case-insensitive, and
    the chosen name is added to `taken`."""
    candidate = name
    n = 1
    while candidate.casefold() in taken:
        n += 1
        suffix = short_hash(target) if n == 2 else f"{short_hash(target)}-{n - 1}"
        candidate = f"{name[: 120 - len(suffix) - 1]}-{suffix}"
    taken.add(candidate.casefold())
    return candidate


def output_path(out_dir: str, name: str, suffix: str) -> str:
    """`<out_dir>/<name><suffix>`, refusing anything that would resolve outside `out_dir`."""
    root = os.path.realpath(out_dir)
    path = os.path.realpath(os.path.join(out_dir, name + suffix))
    if os.path.dirname(path) != root:
        raise ValueError(f"output path {path} is not inside {root}")
    return os.path.normpath(os.path.join(out_dir, name + suffix))


def inside_dir(out_dir: str, path: str) -> bool:
    """True when `path` resolves to somewhere strictly inside `out_dir` (subfolders allowed)."""
    root = os.path.realpath(out_dir)
    target = os.path.realpath(path)
    try:
        return os.path.commonpath([root, target]) == root and target != root
    except ValueError:  # different drives on Windows
        return False


# ---------- records ----------


def append_record(out_dir: str, record: dict[str, Any]) -> str:
    """Append one JSON line to <out_dir>/sources.jsonl. Returns the file path."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, SOURCES_FILE)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_records(out_dir: str) -> list[dict[str, Any]]:
    """All records in <out_dir>/sources.jsonl, in order. Malformed lines are skipped."""
    path = os.path.join(out_dir, SOURCES_FILE)
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def new_record(target: str, route: str, accessed_utc: str, name: str | None = None) -> dict[str, Any]:
    """The record skeleton every route fills in. Keys stay in one fixed order for readability."""
    return {
        "target": target,
        "name": name,
        "url": None,
        "final_url": None,
        "redirects": [],
        "route": route,
        "engine": None,
        "browser": None,
        "http_status": None,
        "accessed_utc": accessed_utc,
        "tool": f"scrapology {__version__}",
        "versions": versions(),
        "outcome": None,
        "note": "",
        "warning": None,
        "title": None,
        "files": {},
        "sha256_raw": None,
        "sha256_rendered": None,
    }


def record(
    out_dir: str,
    *,
    target: str,
    url: str | None,
    route: str,
    outcome: str,
    final_url: str | None = None,
    name: str | None = None,
    engine: str | None = None,
    browser: str | None = None,
    http_status: int | None = None,
    note: str = "",
    warning: str | None = None,
    title: str | None = None,
    files: dict[str, str] | None = None,
    redirects: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    accessed_utc: str | None = None,
) -> dict[str, Any]:
    """Write one receipt line with the standard fields. For plugins and the built-in routes alike.

    `files` maps roles to paths (`raw`, `rendered`, `main`, `structured`, or a plugin's own role names);
    every path must resolve inside `out_dir`. `sha256_raw` is computed from `files["raw"]` and
    `sha256_rendered` from `files["rendered"]`, which must exist. `name` is validated like a
    `--name`. `extra` may not use a standard field name. `outcome` must be `fetched`, `blocked` or
    `failed`. Raises ValueError otherwise. Returns the record written."""
    if outcome not in ("fetched", "blocked", "failed"):
        raise ValueError("outcome must be fetched, blocked or failed")
    if name is not None:
        validate_name(name)
    entry = new_record(target, route, accessed_utc or utc_now(), name)
    if extra:
        clash = sorted(set(extra) & set(entry))
        if clash:
            raise ValueError(f"extra keys collide with standard fields: {', '.join(clash)}")
    files = dict(files or {})
    for role, path in files.items():
        if not isinstance(path, str) or not inside_dir(out_dir, path):
            raise ValueError(f"file {role!r} must be a path inside {out_dir}: {path!r}")
        if role in ("raw", "rendered") and not os.path.isfile(path):
            raise ValueError(f"file {role!r} does not exist, so it cannot be hashed: {path!r}")
    entry.update(
        {
            "url": url,
            "final_url": final_url if final_url is not None else url,
            "redirects": list(redirects or []),
            "engine": engine,
            "browser": browser,
            "http_status": http_status,
            "outcome": outcome,
            "note": note,
            "warning": warning,
            "title": title,
            "files": files,
        }
    )
    for role, key in (("raw", "sha256_raw"), ("rendered", "sha256_rendered")):
        path = files.get(role)
        if path and os.path.isfile(path):
            entry[key] = sha256_file(path)
    if extra:
        entry.update(extra)
    append_record(out_dir, entry)
    return entry
