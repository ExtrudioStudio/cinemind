"""Find stream sources by name: the built-ins plus plugins from the ``cinemind.sources`` group.

A plugin package registers a factory in its ``pyproject.toml``::

    [project.entry-points."cinemind.sources"]
    my-archive = "my_package.source:create"

where ``create(ctx: SourceContext) -> StreamSource``. Add the name to ``stream_sources`` in
the config to use it.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from functools import partial
from importlib.metadata import entry_points

from cinemind.sources.base import SourceContext, SourceFactory, StreamSource

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "cinemind.sources"
BUILTIN: dict[str, str] = {
    "internet-archive": "cinemind.sources.internet_archive:create",
    "stremio-addons": "cinemind.sources.stremio.streams:create",
}
# Short names for the built-ins, for `cinemind get --source`.
ALIASES: dict[str, str] = {
    "archive": "internet-archive",
    "ia": "internet-archive",
    "addons": "stremio-addons",
    "stremio": "stremio-addons",
}


def _import(ref: str) -> SourceFactory:
    module, _, attr = ref.partition(":")
    factory: SourceFactory = getattr(importlib.import_module(module), attr)
    return factory


def factories() -> dict[str, Callable[[], SourceFactory]]:
    """Every known source name, mapped to a loader for its factory (nothing is imported yet)."""
    known: dict[str, Callable[[], SourceFactory]] = {
        name: partial(_import, ref) for name, ref in BUILTIN.items()
    }
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in BUILTIN:
            log.warning("Ignoring plugin source %r: the name is taken by a built-in", ep.name)
            continue
        known[ep.name] = ep.load
    return known


def available() -> list[str]:
    return sorted(factories())


def resolve(names: list[str]) -> list[str]:
    """Source names as given on the command line: comma lists split, short names expanded,
    each name once. An unknown name is a ``ValueError`` that lists the available ones."""
    known = available()
    wanted = [part.strip().lower() for name in names for part in name.split(",") if part.strip()]
    resolved = [ALIASES.get(name, name) for name in wanted]
    unknown = [name for name, full in zip(wanted, resolved, strict=True) if full not in known]
    if unknown:
        short = ", ".join(f"{alias} = {full}" for alias, full in ALIASES.items())
        raise ValueError(
            f"Unknown stream source {', '.join(map(repr, unknown))}. "
            f"Available: {', '.join(known)} (short names: {short})."
        )
    return list(dict.fromkeys(resolved))


def load_sources(names: list[str], ctx: SourceContext) -> list[StreamSource]:
    """Build the named sources, in order. An unknown name is a configuration error."""
    known = factories()
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(
            f"Unknown stream source {', '.join(map(repr, unknown))}. "
            f"Available: {', '.join(sorted(known))}."
        )
    sources = []
    for name in dict.fromkeys(names):  # each once, first mention wins
        source = known[name]()(ctx)
        if not isinstance(source, StreamSource):
            raise TypeError(f"Stream source {name!r} does not implement StreamSource")
        sources.append(source)
    return sources
