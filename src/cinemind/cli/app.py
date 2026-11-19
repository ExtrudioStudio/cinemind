"""Command-line entry point: ``cinemind``."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import sys
from collections.abc import Coroutine
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Any

import tomli_w
import typer
from rich.markup import escape

from cinemind import __version__, service
from cinemind.ai.intent import best_match, read_request, selection_text, understand_selection
from cinemind.ai.llm import LLM, SETUP_HINT, LLMError
from cinemind.cli.home import run_home
from cinemind.cli.shortcut import create_shortcuts
from cinemind.cli.ui import console, plan_table, search_table, selection_line, status_table
from cinemind.cli.wizard import (
    Cancelled,
    ask_selection,
    confirm_plan,
    download,
    interactive,
    make_plan,
    open_session,
    pick_title,
    resume,
    show_overview,
)
from cinemind.core.config import (
    Config,
    config_path,
    data_dir,
    load_config,
    log_dir,
    migrate_legacy_files,
    save_config,
    set_value,
)
from cinemind.core.models import MediaType, Title
from cinemind.core.secrets import KNOWN, SecretsUnavailable, delete_secret, get_secret, set_secret
from cinemind.core.store import Store
from cinemind.ingest.naming import Kind
from cinemind.ingest.runner import Ingester, Media
from cinemind.pipeline.postprocess import hand_off_job, log_folder, new_batch
from cinemind.planning.planner import Plan
from cinemind.planning.quality import parse_quality
from cinemind.planning.selection import Selection, SelectionError, parse_selection, split_spec
from cinemind.service import Session
from cinemind.sources.registry import available
from cinemind.sources.stremio.catalog import search
from cinemind.sources.stremio.client import AddonError

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Find, fetch and file movies and TV shows into a Plex-ready library. Run without a "
    "command for the home menu.",
)
config_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
app.add_typer(config_app, name="config")
secrets_app = typer.Typer(help="Store tokens and passwords in the system keyring.",
                          no_args_is_help=True)  # fmt: skip
app.add_typer(secrets_app, name="secrets")


def _setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir() / "cinemind.log", maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")  # fmt: skip
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:
        pass
    if verbose:
        err = logging.StreamHandler(sys.stderr)
        err.setLevel(logging.DEBUG)
        root.addHandler(err)
    for noisy in ("httpx", "httpcore", "rebulk", "guessit"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(coro: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        console.print("\n[cyan]Paused.[/] Run [bold]cinemind resume[/] to continue any downloads.")
        raise typer.Exit(130) from None
    except Cancelled:
        console.print("Cancelled.")
        raise typer.Exit(1) from None
    except (AddonError, SelectionError, ValueError, LookupError, LLMError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from None


_MEDIA_TYPES: dict[str, MediaType] = {
    "series": "series",
    "tv": "series",
    "show": "series",
    "movie": "movie",
    "film": "movie",
}


def _media_type(value: str | None) -> MediaType | None:
    """The ``--type`` option: ``series`` or ``movie`` (``tv``/``show``/``film`` also work)."""
    if value is None:
        return None
    try:
        return _MEDIA_TYPES[value.strip().lower()]
    except KeyError:
        raise typer.BadParameter("use 'series' or 'movie'", param_hint="--type") from None


def _cfg() -> Config:
    try:
        return load_config()
    except Exception as exc:
        console.print(f"[red]Could not read {config_path()}: {escape(str(exc))}[/]")
        raise typer.Exit(1) from None


@app.callback()
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log debug output.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version.")] = False,
) -> None:
    if version:
        console.print(f"cinemind {__version__}")
        raise typer.Exit()
    try:
        carried = migrate_legacy_files()
    except (OSError, sqlite3.Error) as exc:
        console.print(f"[yellow]Could not copy your stremio-dl settings: {escape(str(exc))}[/]")
        carried = []
    if carried:
        console.print("[cyan]Copied your stremio-dl settings and download history to cinemind:[/] "
                      + ", ".join(str(p) for p in carried), markup=True)  # fmt: skip
    _setup_logging(verbose)
    if ctx.invoked_subcommand is None:
        run_home()


@app.command("search")
def search_cmd(
    query: Annotated[str, typer.Argument(help="Title to look for.")],
    type_: Annotated[str | None, typer.Option("--type", "-t", help="series or movie")] = None,
) -> None:
    """Search the metadata addons."""
    media = _media_type(type_)

    async def go() -> None:
        async with open_session(_cfg()) as sess:
            types: tuple[MediaType, ...] = (media,) if media else ("series", "movie")
            results = await search(sess.client, sess.meta_addons, query, types)
            if results:
                console.print(search_table(results))
            else:
                console.print("Nothing found.")

    _run(go())


@app.command("info")
def info_cmd(
    query: Annotated[str, typer.Argument(help="Title or IMDb id (tt…).")],
    type_: Annotated[str | None, typer.Option("--type", "-t", help="series or movie")] = None,
    sample: Annotated[bool, typer.Option(help="Sample available resolutions.")] = True,
) -> None:
    """Show seasons, episode counts and available resolutions."""
    media = _media_type(type_)

    async def go() -> None:
        async with open_session(_cfg()) as sess:
            title = await pick_title(sess, query, media, first=True, prompt=False)
            if title:
                await show_overview(sess, title, sample=sample)

    _run(go())


@app.command("get")
def get_cmd(
    query: Annotated[str, typer.Argument(help="Title or IMDb id (tt…).")],
    selection: Annotated[
        str | None,
        typer.Argument(help='Episodes and quality, e.g. "s1 eps10 - s2 eps 3 /full hd".'),
    ] = None,
    quality: Annotated[str | None, typer.Option("--quality", "-q", help="e.g. 4k, 1080p")] = None,
    type_: Annotated[str | None, typer.Option("--type", "-t", help="series or movie")] = None,
    first: Annotated[bool, typer.Option("--first", help="Take the top search result.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan only.")] = False,
    prefer_packs: Annotated[
        bool | None, typer.Option("--prefer-packs/--no-prefer-packs", help="Favor season packs.")
    ] = None,
) -> None:
    """Find, plan and download in one go."""
    cfg = _cfg()
    prompt = interactive() and not yes
    media = _media_type(type_)
    if media is None and selection and split_spec(selection)[0]:
        media = "series"  # "s1e5"-style input only makes sense for a show

    async def go() -> None:
        store = Store(data_dir() / "state.db")  # records LLM calls, if any are made
        try:
            async with open_session(cfg) as sess:
                title = await pick_title(sess, query, media, first=first, prompt=prompt)
                if title is None:
                    raise typer.Exit(1)
                text = selection
                if text is None:
                    if title.type == "series" and not prompt:
                        raise ValueError('Give an episode selection, e.g. "s1e1-5 /1080p".')
                    if title.type == "series":
                        await show_overview(sess, title)
                        picked = await ask_selection(sess, title)
                    else:
                        picked = parse_selection("", title, cfg.default_quality)
                else:
                    picked = await _read_selection(text, title, cfg, store)
                plan = await _plan_and_confirm(sess, title, picked, quality=quality,
                                               prompt=prompt, yes=yes, dry_run=dry_run,
                                               prefer_packs=prefer_packs)  # fmt: skip
        finally:
            store.close()
        if plan is not None:
            await download(cfg, plan)

    _run(go())


async def _read_selection(text: str, title: Title, cfg: Config, store: Store) -> Selection:
    """The parser's reading of a selection, or the model's when the parser can't read it."""
    llm = LLM.from_config(cfg, store)
    try:
        picked, read_as = await understand_selection(text, title, cfg.default_quality, llm)
    except SelectionError as exc:
        if llm is None:
            raise SelectionError(f"{exc} For plain-language selections: {SETUP_HINT}") from exc
        raise
    if read_as:
        console.print(f"[dim]Read “{text}” as[/] [cyan]{read_as}[/]")
    return picked


async def _plan_and_confirm(
    sess: Session,
    title: Title,
    picked: Selection,
    *,
    quality: str | None,
    prompt: bool,
    yes: bool,
    dry_run: bool,
    prefer_packs: bool | None,
) -> Plan | None:
    """Show the selection and the plan; return the plan to download, or None to stop."""
    cfg = sess.cfg
    if quality:
        picked.quality = parse_quality(quality)
    if title.type == "series":
        console.print(selection_line(picked.refs, picked.quality or cfg.default_quality))
    plan = await make_plan(sess, title, picked, prompt=prompt, prefer_packs=prefer_packs)
    if dry_run:
        console.print(plan_table(plan))
        return None
    if not await confirm_plan(sess, plan, prompt=prompt, assume_yes=yes):
        return None
    return plan


@app.command("ask")
def ask_cmd(
    request: Annotated[str, typer.Argument(help='e.g. "the last two seasons of Severance in 4K"')],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan only.")] = False,
) -> None:
    """Find, plan and download from a plain-language request (uses Claude)."""
    cfg = _cfg()
    prompt = interactive() and not yes

    async def go() -> None:
        store = Store(data_dir() / "state.db")
        try:
            llm = LLM.from_config(cfg, store)
            if llm is None:
                raise ValueError(f"`cinemind ask` reads requests with Claude. {SETUP_HINT}")
            with console.status("Reading the request…"):
                read = await read_request(llm, request)
            async with open_session(cfg) as sess:
                kind = None if read.media_type == "unknown" else read.media_type
                results = await service.search_titles(sess, read.title, kind)
                match = best_match(results, read)
                if match is None:
                    raise ValueError(f"Nothing found for “{read.title}”.")
                title = await service.load_title(sess, match.id, match.type)
                text = selection_text(read, title)
                picked = parse_selection(text, title, cfg.default_quality)
                console.print(f"[dim]Read as[/] [bold]{title.name}[/] ({match.release_info}) "
                              f"[cyan]{text or 'the movie'}[/]")  # fmt: skip
                plan = await _plan_and_confirm(sess, title, picked, quality=None,
                                               prompt=prompt, yes=yes, dry_run=dry_run,
                                               prefer_packs=None)  # fmt: skip
        finally:
            store.close()
        if plan is not None:
            await download(cfg, plan)

    _run(go())


@app.command("status")
def status_cmd(
    active: Annotated[bool, typer.Option("--active", help="Only unfinished jobs.")] = False,
) -> None:
    """List download jobs."""
    store = Store(data_dir() / "state.db")
    try:
        rows = store.jobs(include_finished=not active)
    finally:
        store.close()
    if rows:
        console.print(status_table(rows))
    else:
        console.print("No downloads yet.")


@app.command("resume")
def resume_cmd(
    job_id: Annotated[int | None, typer.Argument(help="Job number (default: all).")] = None,
) -> None:
    """Continue paused or interrupted downloads."""
    _run(resume(_cfg(), job_id))


@app.command("handoff")
def handoff_cmd(
    job_id: Annotated[int, typer.Argument(help="Job number (see `cinemind status`).")],
) -> None:
    """Run post_download_command for a job's finished files that are still in the library."""
    cfg = _cfg()
    if not cfg.post_download_command.strip():
        console.print(f"[red]post_download_command is not set[/] in {config_path()}.")
        raise typer.Exit(1)
    store = Store(data_dir() / "state.db")
    try:
        plan = store.load_plan(job_id)
        torrents = store.finished_torrents(job_id)
        started = hand_off_job(cfg, plan.title, torrents, new_batch("handoff", job_id),
                               store=store, job_id=job_id)  # fmt: skip
    except KeyError as exc:
        console.print(f"[red]{escape(str(exc.args[0]))}[/]")
        raise typer.Exit(1) from None
    finally:
        store.close()
    if not started:
        console.print(
            f"Nothing to hand off: job #{job_id} has no finished files left in the library."
        )
        return
    for path in started:
        console.print(f"  {path.name}")
    console.print(f"[cyan]{len(started)} file(s) handed to the post-download command[/] "
                  f"[dim](logs: {log_folder()})[/]")  # fmt: skip


@app.command("shortcut")
def shortcut_cmd(
    start_menu: Annotated[
        bool, typer.Option("--start-menu/--no-start-menu", help="Also add a Start menu entry.")
    ] = True,
) -> None:
    """Put a cinemind icon on the desktop (Windows) that opens the home menu."""
    try:
        paths = create_shortcuts(start_menu=start_menu)
    except RuntimeError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from None
    for path in paths:
        console.print(f"Created [bold]{path}[/]")


@config_app.command("show")
def config_show() -> None:
    """Print the current settings."""
    console.print(f"[dim]{config_path()}[/]")
    console.print(tomli_w.dumps(_cfg().model_dump()), markup=False)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file location."""
    console.print(str(config_path()), markup=False)


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Setting name, e.g. tv_root.")],
    value: Annotated[str, typer.Argument(help="New value. Lists are comma-separated.")],
) -> None:
    """Change one setting."""
    try:
        cfg = set_value(_cfg(), key, value)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc.args[0] if exc.args else exc))}[/]")
        raise typer.Exit(1) from None
    save_config(cfg)
    console.print(f"{key} = {getattr(cfg, key)!r}", markup=False)
    if key == "stream_addons" and cfg.stream_addons:
        console.print(STREAM_ADDON_NOTICE)


STREAM_ADDON_NOTICE = (
    "[yellow]Note:[/] Stremio stream addons such as Torrentio list torrents from public "
    "trackers, and much of what they list is copyrighted. Downloading it without permission is "
    "illegal in many countries, and torrent peers can see your IP address. Only download "
    "content you have the right to."
)


@app.command("mcp")
def mcp_cmd(
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Only search and plan; never start downloads.")
    ] = False,
    print_config: Annotated[
        bool, typer.Option("--print-config", help="Print a Claude Desktop config entry and exit.")
    ] = False,
) -> None:
    """Run the MCP server on stdio, so an AI assistant can find, plan and download."""
    if print_config:
        args = ["-m", "cinemind", "mcp", *(["--read-only"] if read_only else [])]
        entry = {"mcpServers": {"cinemind": {"command": sys.executable, "args": args}}}
        console.print_json(data=entry)
        return
    try:
        from cinemind.mcp.server import create_server
    except ImportError:
        console.print("[red]The MCP server needs the mcp extra:[/] "
                      + escape("pip install 'cinemind[mcp]'"))  # fmt: skip
        raise typer.Exit(1) from None
    console.file = sys.stderr  # stdout carries the protocol; nothing else may write there
    create_server(cfg=_cfg(), read_only=read_only).run("stdio")


@app.command("sources")
def sources_cmd() -> None:
    """List the stream sources and which ones are on."""
    cfg = _cfg()
    for name in available():
        on = name in cfg.stream_sources
        mark = "[green]on[/] " if on else "[dim]off[/]"
        extra = ""
        if name == "stremio-addons":
            extra = f" ({len(cfg.stream_addons)} addon(s) in stream_addons)"
        console.print(f"  {mark}  {name}{extra}")
    console.print("[dim]Change with: cinemind config set stream_sources internet-archive,...[/]")


@config_app.command("reset")
def config_reset() -> None:
    """Restore the default settings."""
    save_config(Config())
    console.print(f"Defaults written to {config_path()}", markup=False)


@app.command("ingest")
def ingest_cmd(
    path: Annotated[str, typer.Argument(help="A name in the incoming folder, or a path.")],
    kind: Annotated[str, typer.Option("--kind", help="movie or episode")],
    title: Annotated[str, typer.Option("--title", help="The movie or show name.")],
    year: Annotated[int | None, typer.Option("--year")] = None,
    imdb: Annotated[str | None, typer.Option("--imdb", help="tt…")] = None,
    tmdb: Annotated[str | None, typer.Option("--tmdb")] = None,
    tvdb: Annotated[str | None, typer.Option("--tvdb")] = None,
    season: Annotated[int | None, typer.Option("--season")] = None,
    episode: Annotated[int | None, typer.Option("--episode")] = None,
    end_episode: Annotated[int | None, typer.Option("--end-episode")] = None,
    episode_title: Annotated[str | None, typer.Option("--episode-title")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Change nothing.")] = False,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace a file.")] = False,
    keep_source: Annotated[bool, typer.Option("--keep-source", help="Keep it if fixed.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:  # fmt: skip
    """Check a file against the TV profile, fix it if needed (video is never re-encoded) and
    file it for Plex. Exit code: 0 placed, 10 fixed, 20 quarantined, 1 error."""
    if kind not in ("movie", "episode"):
        raise typer.BadParameter("use 'movie' or 'episode'", param_hint="--kind")
    cfg = _cfg()
    try:
        ingester = Ingester.from_config(cfg)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from None
    media_kind: Kind = "movie" if kind == "movie" else "episode"
    media = Media(media_kind, title, year=year, imdb=imdb, tmdb=tmdb, tvdb=tvdb,
                  season=season, episode=episode, end_episode=end_episode,
                  episode_title=episode_title)  # fmt: skip
    result = asyncio.run(ingester.ingest(path, media, dry_run=dry_run, overwrite=overwrite,
                                         keep_source=keep_source))  # fmt: skip
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        colour = {"placed": "green", "fixed": "green", "quarantined": "yellow",
                  "dry-run": "cyan"}.get(result.status, "red")  # fmt: skip
        verdict = result.verdict.verdict if result.verdict else "-"
        console.print(f"[{colour}]{result.status}[/] ({verdict}) → {escape(result.target or '')}")
        if result.verdict:
            for line in result.verdict.reasons + result.verdict.actions + result.verdict.warnings:
                console.print(f"  · {escape(line)}")
        if result.error:
            console.print(f"[red]{escape(result.error)}[/]")
        if result.plex:
            console.print(f"[dim]Plex: {result.plex.status} {escape(result.plex.detail or '')}[/]")
    raise typer.Exit(result.exit_code)


@app.command("subtitles")
def subtitles_cmd(
    video: Annotated[Path, typer.Argument(help="The video file.")],
    imdb: Annotated[str, typer.Option("--imdb", help="The title's IMDb id, tt…")],
    season: Annotated[int | None, typer.Option("--season")] = None,
    episode: Annotated[int | None, typer.Option("--episode")] = None,
    lang: Annotated[str | None, typer.Option("--lang", help="e.g. he, en")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Where to save it.")] = None,
    no_sync_check: Annotated[bool, typer.Option("--no-sync-check")] = False,
) -> None:
    """Find a subtitle whose timing matches the video's speech, and save it next to the video.
    Exit code 0 when one was saved, 1 when none fitted."""
    from cinemind.subtitles.finder import find_for_video

    if not video.is_file():
        raise typer.BadParameter(f"{video} doesn't exist", param_hint="VIDEO")
    result = asyncio.run(find_for_video(_cfg(), video, imdb, season, episode, lang, out,
                                        sync_check=not no_sync_check))  # fmt: skip
    for line in result.log:
        console.print(f"[dim]{escape(line)}[/]")
    if result.status == "found":
        sync = result.sync.status if result.sync else "not checked"
        console.print(f"[green]Saved[/] {escape(str(result.path))} [dim]({result.source}: "
                      f"{escape(result.release or '')}; sync {sync})[/]")  # fmt: skip
        return
    console.print(f"[yellow]No subtitle saved:[/] {escape(result.reason or '')}")
    raise typer.Exit(1)


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help=f"One of: {', '.join(KNOWN)}")],
    allow_hebrew: Annotated[
        bool, typer.Option("--allow-hebrew", help="The value really contains Hebrew letters.")
    ] = False,
) -> None:
    """Store a secret in the system keyring (the input is hidden)."""
    if name not in KNOWN:
        raise typer.BadParameter(f"use one of: {', '.join(KNOWN)}", param_hint="NAME")
    value = typer.prompt(KNOWN[name], hide_input=True).strip()
    # A hidden prompt can't show that the keyboard is on the Hebrew layout; it happened.
    if not allow_hebrew and any("֐" <= ch <= "׿" for ch in value):
        console.print("[red]That contains Hebrew letters: is the keyboard on the Hebrew layout?[/]"
                      " Switch it and try again, or pass --allow-hebrew.")  # fmt: skip
        raise typer.Exit(1)
    try:
        set_secret(name, value)
    except SecretsUnavailable as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from None
    console.print(f"Stored {name} in the system keyring.")


@secrets_app.command("list")
def secrets_list() -> None:
    """Which secrets are set (never their values)."""
    for name, what in KNOWN.items():
        state = "[green]set[/]" if get_secret(name) else "[dim]not set[/]"
        console.print(f"  {state}  {name}  [dim]{what}[/]")


@secrets_app.command("delete")
def secrets_delete(name: Annotated[str, typer.Argument()]) -> None:
    """Remove a secret from the system keyring."""
    console.print("Deleted." if delete_secret(name) else f"{name} wasn't stored.")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):  # not when replaced by a test harness or IDE
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # emoji, Cyrillic titles
            except ValueError:
                pass
    app()
