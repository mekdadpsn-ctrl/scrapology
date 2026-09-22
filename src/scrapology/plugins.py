"""Extra subcommands from installed packages, through the `scrapology.commands` entry-point group.

Each entry point names a callable `register(subparsers)` that adds one subparser and calls
`set_defaults(func=handler)`, where `handler(args)` returns the exit code (an int, or None for 0). A plugin that fails
to import or register, whatever it raises (SystemExit included), produces one warning line and never
stops the built-in commands. A handler that raises ends with exit code 1 and one line on stderr.
"""

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

GROUP = "scrapology.commands"


@dataclass
class PluginInfo:
    name: str
    value: str
    error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.error is None


def discover() -> list[Any]:
    """The entry points in the group, in the order the environment lists them."""
    try:
        found = entry_points(group=GROUP)
    except TypeError:  # very old importlib.metadata without the group keyword
        found = entry_points().get(GROUP, [])  # type: ignore[attr-defined]
    return list(found)


def register_all(
    subparsers: argparse._SubParsersAction, points: Iterable[Any] | None = None, stream: Any = None
) -> list[PluginInfo]:
    """Load every plugin and let it add its subparser. Failures are reported on `stream` (stderr)."""
    stream = stream or sys.stderr
    infos: list[PluginInfo] = []
    for point in discover() if points is None else points:
        info = PluginInfo(name=str(point.name), value=str(getattr(point, "value", "")))
        try:
            register = point.load()
            register(subparsers)
        except KeyboardInterrupt:
            raise
        except BaseException as err:  # a broken plugin, even one calling sys.exit(), must not take the CLI down
            info.error = f"{type(err).__name__}: {err}"
            print(f"warning: plugin {info.name} not loaded ({info.error})", file=stream)
        infos.append(info)
    return infos
