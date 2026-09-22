"""`scrapology doctor`: is this machine ready? PASS, WARN and FAIL lines; exit 1 on any FAIL."""

import argparse
import os
import platform
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass

from scrapology import browser, cellar, europepmc, plugins, provenance, transport
from scrapology.session import Session

Check = Callable[[], tuple[str, str]]
INSTALL_HINT = "python -m playwright install chromium"
CELLAR_PROBE = "32016R0679"  # Regulation (EU) 2016/679, the GDPR
SEARCH_PROBE = "surveillance"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    seconds: float


def _python() -> tuple[str, str]:
    versions = provenance.versions()
    parts = [f"Python {platform.python_version()} ({sys.executable})"]
    parts += [f"{k} {v}" for k, v in versions.items()]
    missing = [name for name in ("playwright", "trafilatura", "lxml") if name not in versions]
    if missing:
        return "FAIL", f"missing: {', '.join(missing)}; " + "; ".join(parts)
    if "extruct" not in versions:
        parts.append('extruct not installed (--structured needs pip install "scrapology[structured]")')
    return "PASS", "; ".join(parts)


def _bundled() -> tuple[str, str]:
    status = browser.bundled_status()
    expected = status["expected"]
    if not expected:
        return "WARN", f"could not read Playwright's browsers.json under {status['package_dir']}"
    present = status["present"]
    have = [name for name, ok in present.items() if ok]
    lacking = [f"{name} r{expected[name]}" for name, ok in present.items() if not ok]
    where = status["browsers_path"]
    links = status.get("links_path") or os.path.join(where, ".links")
    if lacking:
        return (
            "WARN",
            f"bundled build missing under {where}: {', '.join(lacking)}. Playwright's own `install` deletes any "
            "build that no registered Playwright copy claims, so another copy on this machine may have removed "
            f"it. Fix: {INSTALL_HINT}. Until then the installed Chrome or Edge is used.",
        )
    if not status["registered"]:
        return (
            "WARN",
            f"bundled build present ({', '.join(have)}) but this Playwright copy has no claim file in "
            f"{links}, so a `playwright install` run by another Playwright copy would delete it. "
            f"Fix: {INSTALL_HINT} (it registers the claim).",
        )
    return "PASS", f"bundled build present and registered ({', '.join(have)}) under {where}"


def _browsers() -> tuple[str, str]:
    results = browser.probe_browsers()
    working = [f"{label} ({detail})" for label, ok, detail in results if ok]
    broken = [f"{label}: {detail}" for label, ok, detail in results if not ok]
    if not working:
        return "FAIL", "no browser launches. " + " | ".join(broken) + f". Fix: {INSTALL_HINT}"
    first = results[0]
    if not first[1]:
        return "WARN", f"bundled Chromium does not launch, falling back to {working[0]}. Fix: {INSTALL_HINT}"
    return "PASS", "launches: " + ", ".join(working)


def _plugins() -> tuple[str, str]:
    throwaway = argparse.ArgumentParser(prog="scrapology").add_subparsers()
    infos = plugins.register_all(throwaway)
    if not infos:
        return "PASS", f"no plugins installed (entry-point group {plugins.GROUP})"
    loaded = [f"{p.name} ({p.value})" for p in infos if p.loaded]
    broken = [f"{p.name}: {p.error}" for p in infos if not p.loaded]
    detail = "loaded: " + (", ".join(loaded) or "none")
    if broken:
        return "WARN", detail + "; not loaded: " + "; ".join(broken)
    return "PASS", detail


def _europepmc() -> tuple[str, str]:
    total, _hits = europepmc.search_raw(SEARCH_PROBE, n=1, timeout=30)
    if not total:
        return "FAIL", "Europe PMC answered with 0 hits for a common word twice"
    return "PASS", f"Europe PMC search answers ({total} hits for {SEARCH_PROBE!r})"


def _cellar() -> tuple[str, str]:
    response = cellar.fetch_xhtml(CELLAR_PROBE, timeout=30)
    if not response.ok:
        return "FAIL", f"CELLAR answered HTTP {response.status}"
    if b"2016/679" not in response.body:
        return "FAIL", "CELLAR answered but not with Regulation (EU) 2016/679"
    return "PASS", f"CELLAR serves English XHTML ({len(response.body)} bytes for {CELLAR_PROBE})"


def _example() -> tuple[str, str]:
    from scrapology.fetch import fetch

    with tempfile.TemporaryDirectory(prefix="scrapology-doctor-") as tmp:
        result = fetch("https://example.com/", out_dir=tmp, session=Session(delay=0), timeout=45)
    if result.outcome != "fetched":
        return "FAIL", f"{result.outcome}: {result.note}"
    engine = result.engine or "?"
    detail = f"fetched example.com via {engine}" + (f" ({result.browser})" if result.browser else "")
    if result.warning:
        detail += f"; expected warning: {result.warning}"
    return "PASS", detail


CHECKS: tuple[tuple[str, Check], ...] = (
    ("python and packages", _python),
    ("playwright bundled build", _bundled),
    ("browsers", _browsers),
    ("plugins", _plugins),
    ("europe pmc", _europepmc),
    ("eu cellar", _cellar),
    ("test fetch", _example),
)


def run_checks(checks: tuple[tuple[str, Check], ...] = CHECKS) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, check in checks:
        started = time.monotonic()
        try:
            status, detail = check()
        except transport.TransportError as err:
            status, detail = "FAIL", str(err)
        except Exception as err:  # a check that raises is a FAIL, not a crash
            status, detail = "FAIL", f"{type(err).__name__}: {str(err).splitlines()[0] if str(err) else ''}"
        results.append(CheckResult(name, status, detail, time.monotonic() - started))
    return results


def format_results(results: list[CheckResult]) -> str:
    width = max((len(r.name) for r in results), default=10)
    lines = [f"{r.status:<4}  {r.name:<{width}}  {r.seconds:5.1f}s  {r.detail}" for r in results]
    passed = sum(1 for r in results if r.status == "PASS")
    warned = sum(1 for r in results if r.status == "WARN")
    failed = sum(1 for r in results if r.status == "FAIL")
    lines.append("")
    lines.append(f"{len(results)} checks: {passed} pass, {warned} warn, {failed} fail")
    return "\n".join(lines)


def doctor() -> int:
    results = run_checks()
    print(format_results(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0
