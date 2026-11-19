"""The stream-source plugin contract.

A stream source turns a title (and an episode, for series) into download candidates. The
built-in sources are the Internet Archive (public-domain films, direct HTTP) and the Stremio
stream addons listed in ``stream_addons`` (none by default). Other packages can add sources
through the ``cinemind.sources`` entry point; see :mod:`cinemind.sources.registry`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cinemind.core.config import Config
from cinemind.core.models import Candidate, EpisodeRef, Title
from cinemind.sources.stremio.client import Addon, AddonClient


@dataclass
class SourceContext:
    """What a source factory gets to build its source with."""

    cfg: Config
    client: AddonClient  # JSON over HTTP with retries, a concurrency limit and a disk cache
    stream_addons: list[Addon] = field(default_factory=list)  # loaded from cfg.stream_addons


@runtime_checkable
class StreamSource(Protocol):
    name: str

    def supports(self, title: Title) -> bool:
        """Whether this source can have anything for this title (cheap; no network)."""
        ...

    async def candidates(self, title: Title, ref: EpisodeRef | None) -> list[Candidate]:
        """Every offer for one episode (``ref``) or the movie (``ref`` is None).

        Return them unfiltered: the resolver drops unusable ones and ranks the rest. Raise on
        failure; the resolver logs it and carries on with the other sources.
        """
        ...


SourceFactory = Callable[[SourceContext], StreamSource]
