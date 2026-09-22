"""The `scrapology.commands` entry-point hook and the public receipt helper for plugins."""

import argparse
import hashlib
import io
from dataclasses import dataclass
from typing import Any

import pytest

from scrapology import cli, plugins, provenance


@dataclass
class FakePoint:
    name: str
    value: str
    target: Any

    def load(self) -> Any:
        if isinstance(self.target, BaseException):
            raise self.target
        return self.target


def _register_hello(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("hello", help="say hello")
    parser.add_argument("--code", type=int, default=7)
    parser.set_defaults(func=lambda args: args.code)


def _register_badly(_subparsers: argparse._SubParsersAction) -> None:
    raise RuntimeError("registration exploded")


def test_plugin_subcommand_runs_and_returns_its_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(plugins, "discover", lambda: [FakePoint("hello", "pkg.mod:register", _register_hello)])
    assert cli.main(["hello"]) == 7
    assert cli.main(["hello", "--code", "3"]) == 3


def _register_exiting(_subparsers: argparse._SubParsersAction) -> None:
    raise SystemExit(9)


def _register_failing_handler(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("boom", help="a handler that raises")

    def handler(_args: argparse.Namespace) -> int:
        raise RuntimeError("plugin handler exploded")

    parser.set_defaults(func=handler)


def test_broken_plugins_warn_once_and_leave_the_cli_working(monkeypatch, capsys, tmp_path) -> None:
    points = [
        FakePoint("broken-import", "missing.mod:register", ImportError("No module named 'missing'")),
        FakePoint("broken-register", "pkg.mod:register", _register_badly),
        FakePoint("exits-at-load", "pkg.mod:register", _register_exiting),
        FakePoint("exits-at-import", "pkg.mod:register", SystemExit(3)),
        FakePoint("hello", "pkg.mod:register", _register_hello),
    ]
    monkeypatch.setattr(plugins, "discover", lambda: points)
    assert cli.main(["hello"]) == 7
    err = capsys.readouterr().err
    assert err.count("warning: plugin") == 4
    assert "broken-import" in err and "No module named" in err
    assert "broken-register" in err and "registration exploded" in err
    assert "exits-at-load" in err and "SystemExit: 9" in err
    assert "exits-at-import" in err and "SystemExit: 3" in err
    assert cli.main(["cite", str(tmp_path)]) == 0


def test_plugin_handler_returning_none_is_exit_0(monkeypatch) -> None:
    def register(subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser("quiet")
        parser.set_defaults(func=lambda _args: None)

    monkeypatch.setattr(plugins, "discover", lambda: [FakePoint("quiet", "pkg.mod:register", register)])
    assert cli.main(["quiet"]) == 0


def test_plugin_handler_exception_is_exit_1_with_one_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(plugins, "discover", lambda: [FakePoint("boom", "pkg.mod:register", _register_failing_handler)])
    assert cli.main(["boom"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "boom: RuntimeError: plugin handler exploded"


def test_plugin_keyboard_interrupt_in_handler_is_130(monkeypatch, capsys) -> None:
    def register(subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser("stop")

        def handler(_args: argparse.Namespace) -> int:
            raise KeyboardInterrupt

        parser.set_defaults(func=handler)

    monkeypatch.setattr(plugins, "discover", lambda: [FakePoint("stop", "pkg.mod:register", register)])
    assert cli.main(["stop"]) == 130


def test_keyboard_interrupt_during_registration_is_not_swallowed(monkeypatch) -> None:
    def register(_subparsers: argparse._SubParsersAction) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(plugins, "discover", lambda: [FakePoint("stop", "pkg.mod:register", register)])
    with pytest.raises(KeyboardInterrupt):
        plugins.register_all(argparse.ArgumentParser().add_subparsers())


def test_register_all_reports_infos() -> None:
    stream = io.StringIO()
    subparsers = argparse.ArgumentParser().add_subparsers()
    infos = plugins.register_all(
        subparsers, [FakePoint("hello", "pkg:register", _register_hello), FakePoint("bad", "x:y", ValueError("nope"))],
        stream=stream,
    )
    assert [(i.name, i.loaded) for i in infos] == [("hello", True), ("bad", False)]
    assert "ValueError: nope" in infos[1].error and "warning: plugin bad" in stream.getvalue()


def test_discover_returns_a_list() -> None:
    assert isinstance(plugins.discover(), list)


def test_record_helper_writes_a_standard_receipt(tmp_path) -> None:
    raw = tmp_path / "thing.raw.html"
    raw.write_bytes(b"<html>hi</html>")
    entry = provenance.record(
        str(tmp_path),
        target="plugin:thing",
        url="https://plugin.test/thing",
        route="plugin-route",
        outcome="fetched",
        name="thing",
        engine="plugin",
        http_status=200,
        note="from a plugin",
        title="Thing",
        files={"raw": str(raw), "main": str(tmp_path / "thing.main.md")},
        extra={"plugin_field": 42},
    )
    (read_back,) = provenance.read_records(str(tmp_path))
    assert read_back == entry
    assert entry["sha256_raw"] == hashlib.sha256(b"<html>hi</html>").hexdigest()
    assert entry["sha256_rendered"] is None and entry["plugin_field"] == 42
    assert entry["final_url"] == "https://plugin.test/thing" and entry["tool"].startswith("scrapology ")
    for key in ("target", "name", "url", "final_url", "redirects", "route", "engine", "browser", "http_status",
                "accessed_utc", "tool", "versions", "outcome", "note", "warning", "title", "files"):
        assert key in entry
    with pytest.raises(ValueError):
        provenance.record(str(tmp_path), target="x", url=None, route="r", outcome="maybe")


def test_record_helper_refuses_false_receipts(tmp_path) -> None:
    out = str(tmp_path)
    inside = tmp_path / "ok.raw.html"
    inside.write_bytes(b"x")
    outside = tmp_path.parent / "elsewhere.raw.html"
    common = {"target": "t", "url": "https://p.test/", "route": "plugin", "outcome": "fetched"}
    with pytest.raises(ValueError):
        provenance.record(out, name="../escape", **common)
    with pytest.raises(ValueError):
        provenance.record(out, name="con", **common)
    with pytest.raises(ValueError):
        provenance.record(out, extra={"outcome": "fetched"}, **common)
    with pytest.raises(ValueError):
        provenance.record(out, extra={"sha256_raw": "0" * 64}, **common)
    with pytest.raises(ValueError):
        provenance.record(out, files={"raw": str(outside)}, **common)
    with pytest.raises(ValueError):
        provenance.record(out, files={"raw": str(tmp_path)}, **common)
    with pytest.raises(ValueError):
        provenance.record(out, files={"raw": 42}, **common)  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        provenance.record(out, files={"raw": str(tmp_path / "missing.raw.html")}, **common)
    with pytest.raises(ValueError):
        provenance.record(out, files={"rendered": str(tmp_path / "missing.rendered.html")}, **common)
    assert provenance.read_records(out) == []
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.txt").write_text("x", encoding="utf-8")
    entry = provenance.record(out, name="ok", files={"raw": str(inside), "note": str(sub / "deep.txt")}, **common)
    assert entry["sha256_raw"] and len(provenance.read_records(out)) == 1
