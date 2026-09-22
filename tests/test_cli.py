"""CLI exit codes and output with the network mocked."""

import importlib.util
import json
import os

import pytest

from conftest import ARTICLE_HTML, CHALLENGE_HTML, FakeNet
from scrapology import cli, doctor, europepmc, plugins, provenance, transport

ORIGIN = "https://site.test"


def run(*argv: str) -> int:
    return cli.main(list(argv))


@pytest.fixture(autouse=True)
def no_real_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "discover", lambda: [])


def test_no_command_is_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 2


def test_fetch_needs_exactly_one_source(tmp_path, capsys) -> None:
    assert run("fetch", "-o", str(tmp_path)) == 2
    assert run("fetch", "https://a.test/", "-i", "x.txt", "-o", str(tmp_path)) == 2
    assert run("fetch", "https://a.test/", "--delay", "-1", "-o", str(tmp_path)) == 2
    assert run("fetch", "https://a.test/", "--max-bytes", "0", "-o", str(tmp_path)) == 2
    assert run("fetch", "??", "-o", str(tmp_path)) == 2
    assert run("fetch", "https://a.test/", "--name", "../escape", "-o", str(tmp_path)) == 2
    assert "fetch:" in capsys.readouterr().err


def test_fetch_bad_engine_is_usage_error(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        run("fetch", "https://a.test/", "--engine", "magic", "-o", str(tmp_path))
    assert exc.value.code == 2


def test_missing_extras_are_usage_errors(tmp_path, monkeypatch, capsys) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "extruct" else real(name))
    assert run("fetch", "https://a.test/", "--structured", "-o", str(tmp_path)) == 2
    assert "structured" in capsys.readouterr().err


def test_fetch_ok(net: FakeNet, tmp_path, capsys, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(f"{ORIGIN}/page", 200, ARTICLE_HTML)
    code = run("fetch", f"{ORIGIN}/page", "-o", str(tmp_path), "--delay", "0", "--name", "page")
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("FETCHED") and "page.main.md" in out
    assert os.path.exists(tmp_path / "page.main.md")


def test_fetch_blocked_exit_3(net: FakeNet, tmp_path, capsys) -> None:
    net.robots(ORIGIN, "User-agent: *\nDisallow: /\n")
    code = run("fetch", f"{ORIGIN}/page", "-o", str(tmp_path), "--delay", "0")
    assert code == 3 and capsys.readouterr().out.startswith("BLOCKED")


def test_fetch_failed_exit_1(net: FakeNet, tmp_path, capsys) -> None:
    net.robots(ORIGIN)
    net.add(f"{ORIGIN}/page", 500, "boom")
    code = run("fetch", f"{ORIGIN}/page", "-o", str(tmp_path), "--delay", "0")
    assert code == 1 and capsys.readouterr().out.startswith("FAILED")


def test_batch_exit_codes_and_summary(net: FakeNet, tmp_path, capsys, no_browser) -> None:
    net.robots(ORIGIN)
    net.add(f"{ORIGIN}/ok", 200, ARTICLE_HTML)
    net.add(f"{ORIGIN}/wall", 200, CHALLENGE_HTML)
    net.add(f"{ORIGIN}/gone", 404, "gone")
    targets = tmp_path / "targets.txt"
    targets.write_text(f"# comment\n\n{ORIGIN}/ok\n{ORIGIN}/wall\n{ORIGIN}/gone\n{ORIGIN}/ok\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    code = run("fetch", "-i", str(targets), "-o", str(out_dir), "--delay", "0")
    out = capsys.readouterr().out
    assert code == 1
    assert "summary: 2 fetched, 1 blocked, 1 failed" in out
    names = sorted(p for p in os.listdir(out_dir) if p.endswith(".main.md"))
    assert names == [f"site.test-ok-{provenance.short_hash(f'{ORIGIN}/ok')}.main.md", "site.test-ok.main.md"]
    assert len(provenance.read_records(str(out_dir))) == 4

    targets.write_text(f"{ORIGIN}/ok\n{ORIGIN}/wall\n", encoding="utf-8")
    assert run("fetch", "-i", str(targets), "-o", str(out_dir), "--delay", "0") == 3
    targets.write_text(f"{ORIGIN}/ok\n", encoding="utf-8")
    assert run("fetch", "-i", str(targets), "-o", str(out_dir), "--delay", "0") == 0


def test_batch_missing_or_empty_file(tmp_path, capsys) -> None:
    assert run("fetch", "-i", str(tmp_path / "missing.txt"), "-o", str(tmp_path)) == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    assert run("fetch", "-i", str(empty), "-o", str(tmp_path)) == 2


def _search_json(hits: int = 2) -> str:
    result = [
        {"pmcid": "PMC1", "doi": "10.1/a", "title": "Surveillance &amp; <i>resistance</i>", "isOpenAccess": "Y",
         "pubYear": 2020},
        {"pmcid": None, "doi": "10.1/b", "title": "Closed paper", "isOpenAccess": "N", "pubYear": 2019},
    ]
    return json.dumps({"hitCount": hits, "resultList": {"result": result[:hits]}})


def _resp(url: str, body: str) -> transport.Response:
    return transport.Response(200, url, body.encode(), {"content-type": "application/json"})


def test_search_table_and_json(net: FakeNet, capsys) -> None:
    net.routes[f"{europepmc.EPMC}/search?*"] = lambda url: _resp(url, _search_json())
    assert run("search", "resistance surveillance", "--n", "2") == 0
    out = capsys.readouterr().out
    assert "Europe PMC: 2 hits for 'resistance surveillance'; showing 2" in out
    assert "OA PMC1" in out and "Surveillance & resistance" in out and "-- -" in out
    assert run("search", "resistance surveillance", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["hits"] == 2 and data["results"][0]["title"] == "Surveillance & resistance"
    assert run("search", "x", "--n", "0") == 2


def test_search_oa_flag_changes_query(net: FakeNet) -> None:
    net.routes[f"{europepmc.EPMC}/search?*"] = lambda url: _resp(url, _search_json())
    run("search", "surveillance", "--oa")
    assert "OPEN_ACCESS%3AY" in net.calls[0]


def test_cite_command(tmp_path, capsys) -> None:
    assert run("cite", str(tmp_path / "missing")) == 2
    provenance.append_record(
        str(tmp_path),
        {"outcome": "fetched", "title": "T", "final_url": "https://t.test/",
         "accessed_utc": "2026-01-02T00:00:00+00:00"},
    )
    assert run("cite", str(tmp_path)) == 0
    assert capsys.readouterr().out.strip() == "1. T. https://t.test/. Accessed 2026-01-02 (web)."


def test_doctor_formatting_and_exit(monkeypatch, capsys) -> None:
    def boom() -> tuple[str, str]:
        raise RuntimeError("kaput")

    checks = (("one", lambda: ("PASS", "fine")), ("two", lambda: ("WARN", "meh")), ("three", boom))
    results = doctor.run_checks(checks)
    assert [r.status for r in results] == ["PASS", "WARN", "FAIL"]
    assert "RuntimeError: kaput" in results[2].detail
    text = doctor.format_results(results)
    assert "3 checks: 1 pass, 1 warn, 1 fail" in text
    monkeypatch.setattr(doctor, "run_checks", lambda: results)
    assert run("doctor") == 1
    assert "FAIL  three" in capsys.readouterr().out
    monkeypatch.setattr(doctor, "run_checks", lambda: results[:2])
    assert run("doctor") == 0


def test_doctor_plugin_check() -> None:
    status, detail = doctor._plugins()
    assert status == "PASS" and "no plugins" in detail


def test_keyboard_interrupt_exits_130(monkeypatch, capsys, tmp_path) -> None:
    def interrupted(_args) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_cite", interrupted)
    assert run("cite", str(tmp_path)) == 130
    assert "interrupted" in capsys.readouterr().err


def test_command_exception_is_exit_1_with_one_line(monkeypatch, capsys, tmp_path) -> None:
    def broken(_args) -> int:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(cli, "cmd_cite", broken)
    assert run("cite", str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert err.strip() == "cite: RuntimeError: handler exploded"


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        run("--version")
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("scrapology ")
