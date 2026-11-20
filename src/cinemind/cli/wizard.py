"""The search → overview → select → plan → download flow (the wizard and `cinemind get`)."""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import questionary
from rich.markup import escape
from rich.status import Status
from rich.text import Text

from cinemind import service
from cinemind.cli.ui import (
    console,
    delivery_summary,
    human_size,
    movie_resolutions,
    plan_table,
    search_table,
    selection_line,
    title_overview,
)
from cinemind.core.config import Config, data_dir
from cinemind.core.models import Key, MediaType, Title
from cinemind.core.store import ATTENTION, Store
from cinemind.pipeline.postprocess import log_folder
from cinemind.pipeline.runner import DownloadRunner
from cinemind.planning import quality as q
from cinemind.planning.library import free_space
from cinemind.planning.planner import Plan
from cinemind.planning.selection import Selection, SelectionError, parse_selection
from cinemind.service import Session

SYNTAX_HINT = (
    "Examples: [cyan]s1[/]  ·  [cyan]s1e5[/]  ·  [cyan]s1 eps10 - s2 eps 3[/]  ·  "
    "[cyan]s2e1-5, s4e2[/]  ·  [cyan]all[/]   then optionally [cyan]/4k[/], [cyan]/full hd[/], "
    "[cyan]/720p[/], [cyan]/best[/]"
)


SEARCH_AGAIN = "search-again"


class Cancelled(Exception):
    """The user backed out of a prompt."""


@asynccontextmanager
async def open_session(cfg: Config) -> AsyncIterator[Session]:
    """A :func:`cinemind.service.open_session`, with a spinner while the addons load."""
    status = Status("Loading addons…", console=console)
    status.start()

    def warn(message: str) -> None:
        console.print(f"[yellow]{escape(message)}[/]")

    try:
        async with service.open_session(cfg, warn=warn) as sess:
            status.stop()
            yield sess
    finally:
        status.stop()


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


async def _ask(question: questionary.Question) -> Any:
    answer = await question.ask_async()
    if answer is None:
        raise Cancelled
    return answer


# -- step 1: find the title ------------------------------------------------------------------


async def pick_title(
    sess: Session,
    query: str,
    type_: MediaType | None = None,
    first: bool = False,
    prompt: bool = True,
) -> Title | None:
    if service.is_imdb_id(query):
        try:
            return await service.load_title(sess, query, type_)
        except LookupError as exc:
            console.print(f"[red]{escape(str(exc))}.[/]")
            return None

    with Status(f"Searching for “{query}”…", console=console):
        results = await service.search_titles(sess, query, type_)
    if not results:
        console.print(f"[yellow]Nothing found for “{query}”.[/]")
        return None
    chosen = results[0]
    if not first and prompt and len(results) > 1:
        choices = [questionary.Choice(r.label, value=r) for r in results]
        choices.append(questionary.Choice("↩  search again", value=SEARCH_AGAIN))
        chosen = await _ask(questionary.select("Which one?", choices=choices))
        if chosen == SEARCH_AGAIN:
            return None
    elif not prompt and not first and len(results) > 1:
        exact = [r for r in results if r.name.casefold() == query.casefold()]
        if len(exact) != 1:
            console.print(search_table(results[:10]))
            console.print("[yellow]Several matches; pass --first or an IMDb id (tt…).[/]")
            return None
        chosen = exact[0]

    with Status(f"Loading {chosen.name}…", console=console):
        return await service.load_title(sess, chosen.id, chosen.type)


# -- step 2: overview ------------------------------------------------------------------------


async def show_overview(sess: Session, title: Title, sample: bool = True) -> None:
    if title.type == "movie":
        console.print(title_overview(title))
        with Status("Checking available streams…", console=console):
            cands = await sess.resolver.candidates(title, None)
        console.print(movie_resolutions(Counter(c.resolution for c in cands)))
        return
    if not title.episodes:
        console.print(title_overview(title))
        console.print("[yellow]No aired episodes listed for this show.[/]")
        return
    matrix = None
    if sample:
        seasons = len(title.seasons())
        with Status(f"Sampling available resolutions across {seasons} seasons…", console=console):
            matrix = await sess.resolver.resolution_matrix(title)
    console.print(title_overview(title, matrix))


# -- step 3: selection -----------------------------------------------------------------------


async def ask_selection(sess: Session, title: Title) -> Selection:
    if title.type == "movie":
        text = await _ask(questionary.text(
            "Quality?", default=sess.cfg.default_quality,
            instruction="(4k, 1080p/full hd, 720p, sd, best)",
        ))  # fmt: skip
        return parse_selection(text, title, sess.cfg.default_quality)
    console.print(SYNTAX_HINT)
    while True:
        text = await _ask(questionary.text("What do you want?"))
        try:
            selection = parse_selection(text, title, sess.cfg.default_quality)
        except (SelectionError, ValueError) as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            continue
        console.print(selection_line(selection.refs, selection.quality or ""))
        return selection


# -- step 4: streams and plan ----------------------------------------------------------------


async def make_plan(
    sess: Session,
    title: Title,
    selection: Selection,
    prompt: bool = True,
    prefer_packs: bool | None = None,
) -> Plan:
    cfg = sess.cfg
    total = len(selection.refs) if title.type == "series" else 1
    done = 0
    with Status("Finding streams…", console=console) as status:

        def progress(_key: Key) -> None:
            nonlocal done
            done += 1
            status.update(f"Finding streams… {done}/{total}")

        offers = await service.find_offers(sess, title, selection, on_progress=progress)

    requested = offers.requested
    on_missing = "skip" if cfg.on_missing == "ask" else cfg.on_missing
    plan = offers.plan(cfg, on_missing, prefer_packs)

    lower_available = offers.missing_at_requested(plan)
    if cfg.on_missing == "ask" and lower_available and requested != q.BEST:
        n = len(lower_available)
        what = "This title has" if title.type == "movie" else f"{n} episode{'s' if n > 1 else ''}"
        if prompt:
            use_lower = await _ask(questionary.select(
                f"{what} no {requested} streams. What should I do?",
                choices=[
                    questionary.Choice("Use the next lower quality for them", value=True),
                    questionary.Choice("Skip them", value=False),
                ],
            ))  # fmt: skip
        else:
            use_lower = False
            console.print(
                f"[yellow]{what} no {requested} streams; skipping them "
                '(set on_missing = "lower" to fall back automatically).[/]'
            )
        if use_lower:
            plan = offers.plan(cfg, "lower", prefer_packs)
    return plan


def _library_root(cfg: Config, title: Title) -> Path:
    return Path(cfg.movie_root if title.type == "movie" else cfg.tv_root)


async def confirm_plan(sess: Session, plan: Plan, prompt: bool, assume_yes: bool) -> bool:
    console.print(plan_table(plan))
    if not plan.jobs:
        console.print("[yellow]Nothing to download.[/]")
        return False
    root = _library_root(sess.cfg, plan.title)
    needed = int(plan.total_size * 1.05)
    free = free_space(root)
    console.print(f"[dim]Library: {root}  ·  free space {human_size(free)}[/]")
    if needed > free:
        console.print(f"[red]Not enough disk space: need about {human_size(needed)}.[/]")
        if not prompt:
            return assume_yes
    if assume_yes:
        return True
    if not prompt:
        console.print("[yellow]Pass --yes to start without confirmation.[/]")
        return False
    return bool(await _ask(questionary.confirm(
        f"Download {plan.file_count} files ({human_size(plan.total_size)})?", default=True
    )))  # fmt: skip


# -- step 5/6: download ----------------------------------------------------------------------


async def download(cfg: Config, plan: Plan) -> str:
    store = Store(data_dir() / "state.db")
    try:
        job_id = store.create_job(plan)
        console.print(f"[dim]Job #{job_id} · Ctrl+C pauses; `cinemind resume` continues.[/]")
        runner = DownloadRunner(cfg, store, job_id, plan)
        status = await runner.run(store.open_torrents(job_id))
        report(store, job_id, status, runner)
        return status
    finally:
        store.close()


async def resume(cfg: Config, job_id: int | None = None) -> None:
    store = Store(data_dir() / "state.db")
    try:
        rows = store.jobs(include_finished=False)
        if job_id is not None:
            rows = [r for r in rows if r.id == job_id]
        if not rows:
            console.print("Nothing to resume.")
            return
        for row in reversed(rows):
            torrents = store.open_torrents(row.id)
            if not torrents and not store.open_deliveries(row.id):
                store.finish_job(row.id)
                continue
            console.print(f"[bold]Resuming job #{row.id}: {row.title}[/]")
            store.set_job_status(row.id, "running")
            runner = DownloadRunner(cfg, store, row.id, store.load_plan(row.id))
            status = await runner.run(torrents)
            report(store, row.id, status, runner)
    finally:
        store.close()


def report(store: Store, job_id: int, status: str, runner: DownloadRunner | None = None) -> None:
    row = next((r for r in store.jobs() if r.id == job_id), None)
    if row is None:
        return
    done = row.counts.get("done", 0)
    failed = row.counts.get("failed", 0)
    color = {"done": "green", "partial": "yellow", "failed": "red"}.get(status, "cyan")
    console.print(f"[{color}]Job #{job_id} {status}[/]: {done} downloaded, {failed} failed.")
    if row.deliveries:
        attention = sum(row.deliveries.get(s, 0) for s in ATTENTION)
        line = Text("Delivery: ").append(delivery_summary(row.deliveries))
        if attention:
            line.append("  (`cinemind review` shows what needs you)", style="dim")
        console.print(line)
    if runner and runner.handed_off:
        count = len(runner.handed_off)
        console.print(
            f"[cyan]{count} file{'s' if count != 1 else ''} handed to the post-download "
            f"command[/] [dim](it keeps running in the background; `cinemind status` shows how it "
            f"ended; logs: {log_folder()})[/]"
        )
    if runner and runner.engine.listener_errors:
        count = len(runner.engine.listener_errors)
        console.print(
            f"[yellow]{count} internal error{'s' if count != 1 else ''} while recording "
            f"progress[/]; the downloads continued. Details: cinemind logs --level error"
        )


# -- the interactive wizard ------------------------------------------------------------------


async def wizard(cfg: Config, query: str | None = None) -> None:
    async with open_session(cfg) as sess:
        title = None
        while title is None:
            if not query:
                query = await _ask(questionary.text("What do you want to download?"))
                if not query.strip():
                    query = None
                    continue
            title = await pick_title(sess, query.strip())
            query = None
        await show_overview(sess, title)
        selection = await ask_selection(sess, title)
        plan = await make_plan(sess, title, selection)
        if not await confirm_plan(sess, plan, prompt=True, assume_yes=False):
            return
    await download(cfg, plan)
