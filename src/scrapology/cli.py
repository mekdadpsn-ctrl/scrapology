"""Command line: fetch, search, cite, doctor, plus any subcommands installed plugins add.

Exit codes: 0 fetched (warnings allowed), 1 failed, 2 usage error, 3 blocked or not permitted,
130 interrupted. Batch mode: 1 if any target failed, else 3 if any was blocked, else 0.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence

from scrapology import doctor, europepmc, plugins, provenance, transport
from scrapology._version import __version__
from scrapology.cite import cite
from scrapology.fetch import DEFAULT_OUT, EMAIL_VARIABLE, FetchResult, check_options, fetch
from scrapology.session import Session
from scrapology.transport import DEFAULT_MAX_BYTES
from scrapology.web import ENGINES

EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def build_parser(load_plugins: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrapology",
        description="Fetch web pages, open-access papers and EU law the polite way, with a receipt for every page.",
    )
    parser.add_argument("--version", action="version", version=f"scrapology {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True

    p_fetch = commands.add_parser("fetch", help="fetch one target, or a file of targets with -i")
    p_fetch.add_argument("target", nargs="?", help="URL, pmc:PMC123, doi:10.x/..., or celex:32016R0679")
    p_fetch.add_argument("-i", "--input", metavar="FILE", help="batch: one target per line, # starts a comment")
    p_fetch.add_argument("-o", "--out", default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    p_fetch.add_argument("--name", help="base name for the output files (default: a slug of the target)")
    p_fetch.add_argument("--structured", action="store_true", help="also write <name>.structured.json (extruct)")
    p_fetch.add_argument("--engine", choices=ENGINES, default="auto", help="auto (default), http or browser")
    p_fetch.add_argument("--timeout", type=float, default=60, help="seconds per request (default 60)")
    p_fetch.add_argument("--delay", type=float, default=2.0, help="minimum seconds between page requests per host")
    p_fetch.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
        help=f"largest response accepted, in bytes (default {DEFAULT_MAX_BYTES}, 50 MB)",
    )
    p_fetch.add_argument("--email", help=f"contact email for Unpaywall (or set {EMAIL_VARIABLE})")
    p_fetch.set_defaults(func=cmd_fetch)

    p_search = commands.add_parser("search", help="search Europe PMC")
    p_search.add_argument("query", help="Europe PMC query, for example: antimicrobial resistance surveillance")
    p_search.add_argument("--n", type=int, default=10, help="number of results (default 10)")
    p_search.add_argument("--oa", action="store_true", help="open-access results only")
    p_search.add_argument("--json", action="store_true", help="print the results as JSON")
    p_search.set_defaults(func=cmd_search)

    p_cite = commands.add_parser("cite", help="numbered source list from an output folder's sources.jsonl")
    p_cite.add_argument("out_dir", help="output folder holding sources.jsonl")
    p_cite.set_defaults(func=cmd_cite)

    p_doctor = commands.add_parser("doctor", help="check Python, browsers, Europe PMC, CELLAR, plugins, a test fetch")
    p_doctor.set_defaults(func=cmd_doctor)

    if load_plugins:
        plugins.register_all(commands)
    return parser


def _reconfigure_stdout() -> None:
    """Titles and notes carry non-ASCII text; never let a narrow console encoding crash the run."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def read_targets(path: str) -> list[str]:
    targets: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            targets.append(line)
    return targets


def _report(result: FetchResult) -> None:
    label = result.outcome.upper()
    if result.outcome == "fetched":
        main = result.files.get("main") or result.files.get("raw") or result.files.get("rendered") or ""
        print(f"{label}  {result.target}  -> {main}")
        if result.note:
            print(f"        note: {result.note}")
        if result.warning:
            print(f"        WARNING: {result.warning}")
    else:
        print(f"{label}  {result.target}: {result.note}")


def cmd_fetch(args: argparse.Namespace) -> int:
    if bool(args.target) == bool(args.input):
        print("fetch: give one target, or -i FILE, not both and not neither", file=sys.stderr)
        return EXIT_USAGE
    try:
        check_options(
            engine=args.engine, structured=args.structured, timeout=args.timeout, delay=args.delay,
            max_bytes=args.max_bytes,
        )
        if args.name is not None:
            provenance.validate_name(args.name)
        session = Session(delay=args.delay, timeout=args.timeout)
    except ValueError as err:
        print(f"fetch: {err}", file=sys.stderr)
        return EXIT_USAGE
    common = {
        "out_dir": args.out,
        "structured": args.structured,
        "engine": args.engine,
        "timeout": args.timeout,
        "delay": args.delay,
        "email": args.email,
        "session": session,
        "max_bytes": args.max_bytes,
    }
    if args.target:
        try:
            result = fetch(args.target, name=args.name, **common)
        except ValueError as err:
            print(f"fetch: {err}", file=sys.stderr)
            return EXIT_USAGE
        _report(result)
        return result.exit_code

    try:
        targets = read_targets(args.input)
    except OSError as err:
        print(f"fetch: cannot read {args.input}: {err}", file=sys.stderr)
        return EXIT_USAGE
    if not targets:
        print(f"fetch: no targets in {args.input}", file=sys.stderr)
        return EXIT_USAGE
    if args.name:
        print("fetch: --name is ignored in batch mode, names are derived from each target", file=sys.stderr)
    counts = {"fetched": 0, "blocked": 0, "failed": 0}
    for target in targets:
        try:
            result = fetch(target, **common)
        except ValueError as err:
            print(f"FAILED  {target}: {err}")
            counts["failed"] += 1
            continue
        _report(result)
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    print(f"summary: {counts['fetched']} fetched, {counts['blocked']} blocked, {counts['failed']} failed")
    if counts["failed"]:
        return 1
    if counts["blocked"]:
        return 3
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if args.n < 1:
        print("search: --n must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    try:
        total, hits = europepmc.search_raw(args.query, n=args.n, oa_only=args.oa)
    except transport.TransportError as err:
        print(f"search: {err}", file=sys.stderr)
        return 1
    papers = [europepmc.paper_from_hit(h) for h in hits]
    if args.json:
        payload = {"query": args.query, "hits": total, "results": [p.to_dict() for p in papers]}
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0
    print(f"Europe PMC: {total} hits for {args.query!r}; showing {len(papers)}")
    for paper in papers:
        oa = "OA" if paper.open_access else "--"
        print(f"{oa} {paper.pmcid or '-':<12} {paper.year or '?':<4} {paper.doi or '-':<32} {paper.title[:110]}")
    return 0


def cmd_cite(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.out_dir):
        print(f"cite: not a folder: {args.out_dir}", file=sys.stderr)
        return EXIT_USAGE
    print(cite(args.out_dir))
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    return doctor.doctor()


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_stdout()
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        func = getattr(args, "func", None)
        if func is None:
            parser.print_usage(sys.stderr)
            return EXIT_USAGE
        try:
            code = func(args)
            return 0 if code is None else int(code)
        except Exception as err:  # a failing command (a plugin's or ours) is exit 1 with one line, never a traceback
            detail = str(err).splitlines()[0] if str(err) else ""
            print(f"{args.command}: {type(err).__name__}: {detail}", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
