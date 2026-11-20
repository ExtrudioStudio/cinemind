"""``cinemind doctor``: is this machine ready, and if not, what to do about it.

Each check is **ok**, **warn** (a feature that won't work, or will work worse), **fail**
(something the settings ask for that can't work now) or **skip** (not set up, so not needed),
with the fix when there is one. Checks that need the network (the metadata addon, Plex, Ollama)
are left out with ``offline``. Nothing here changes a setting or a file, and no secret's value
is ever shown.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from cinemind.core.config import Config, config_path, data_dir, log_dir
from cinemind.core.network import Interface, interfaces, vpn_down
from cinemind.core.secrets import env_name, get_secret
from cinemind.core.store import Store

Status = Literal["ok", "warn", "fail", "skip"]
TIMEOUT = 6.0  # seconds, per network check


@dataclass(frozen=True)
class Check:
    area: str
    name: str
    status: Status
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _writable(path: Path) -> tuple[bool, str]:
    """Can files be made in ``path``, or, when it doesn't exist yet, in the nearest parent?"""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False, "no part of the path exists"
        probe = probe.parent
    if not probe.is_dir():
        return False, f"{probe} is a file"
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".cinemind-doctor-"):
            pass
    except OSError as exc:
        return False, f"can't write there ({exc.strerror or exc})"
    return True, "" if probe == path else f"will be created inside {probe}"


# -- setup -----------------------------------------------------------------------------------------


def check_setup() -> list[Check]:
    from cinemind import __version__

    out = [
        Check("Setup", "cinemind", "ok", f"{__version__} on Python {sys.version.split()[0]}"),
        Check("Setup", "Settings", "ok", str(config_path())),
    ]
    try:
        store = Store(data_dir() / "state.db")
        try:
            version = store.schema_version
        finally:
            store.close()
        out.append(
            Check(
                "Setup", "Download history", "ok", f"{data_dir() / 'state.db'} (schema v{version})"
            )
        )
    except Exception as exc:
        out.append(
            Check(
                "Setup",
                "Download history",
                "fail",
                f"state.db won't open: {exc}",
                "Check the folder's permissions and free space.",
            )
        )
    ok, why = _writable(log_dir())
    out.append(
        Check(
            "Setup",
            "Logs",
            "ok" if ok else "warn",
            str(log_dir()) + (f" ({why})" if why else ""),
            "" if ok else "Without it, nothing is logged.",
        )
    )
    return out


# -- downloads -------------------------------------------------------------------------------------


def check_libraries(cfg: Config) -> list[Check]:
    out = []
    for name, root in (("TV library", cfg.tv_root), ("Movie library", cfg.movie_root)):
        ok, why = _writable(Path(root))
        detail = root + (f" ({why})" if why else "")
        key = "tv_root" if name.startswith("TV") else "movie_root"
        out.append(
            Check(
                "Downloads",
                name,
                "ok" if ok else "fail",
                detail,
                "" if ok else f"cinemind config set {key} <a folder you can write>",
            )
        )
    for root in [*cfg.extra_tv_roots, *cfg.extra_movie_roots]:
        there = Path(root).is_dir()
        out.append(
            Check(
                "Downloads",
                "Other library",
                "ok" if there else "warn",
                root if there else f"{root} can't be reached",
                "" if there else "Episodes there aren't seen until it's back.",
            )
        )
    return out


def _torrents_wanted(cfg: Config) -> bool:
    return "stremio-addons" in cfg.stream_sources and bool(cfg.stream_addons)


def check_torrent(cfg: Config) -> Check:
    if importlib.util.find_spec("libtorrent") is None:
        status: Status = "fail" if _torrents_wanted(cfg) else "warn"
        return Check(
            "Downloads",
            "BitTorrent",
            status,
            "libtorrent isn't installed: direct downloads only",
            "pip install 'cinemind[torrent]'",
        )
    try:
        import libtorrent

        return Check("Downloads", "BitTorrent", "ok", f"libtorrent {libtorrent.__version__}")
    except ImportError as exc:  # installed, but its native library won't load
        return Check(
            "Downloads",
            "BitTorrent",
            "fail",
            f"libtorrent won't load: {exc}",
            "Reinstall it: pip install --force-reinstall libtorrent",
        )


def check_vpn(cfg: Config, found: list[Interface] | None = None) -> Check:
    if not cfg.outgoing_interfaces.strip():
        return Check(
            "Downloads",
            "VPN",
            "skip",
            "outgoing_interfaces isn't set: BitTorrent uses any connection",
            "To keep it on a VPN: cinemind config set outgoing_interfaces <adapter>",
        )
    found = interfaces() if found is None else found
    reason = vpn_down(cfg.outgoing_interfaces, found)
    if reason:
        up = ", ".join(i.name for i in found if i.up and i.addresses) or "none"
        if cfg.vpn_kill_switch:
            return Check(
                "Downloads",
                "VPN",
                "fail",
                f"{reason}: BitTorrent won't start until it's up",
                f"Connect the VPN, or check the name. Up now: {up}",
            )
        return Check(
            "Downloads",
            "VPN",
            "fail",
            f"{reason}, and vpn_kill_switch is off: torrents would bypass it",
            "Connect the VPN, and: cinemind config set vpn_kill_switch true",
        )
    if not cfg.vpn_kill_switch:
        return Check(
            "Downloads",
            "VPN",
            "warn",
            f"{cfg.outgoing_interfaces} is up, but vpn_kill_switch is off: if it "
            "drops, torrents carry on without it",
            "cinemind config set vpn_kill_switch true",
        )
    return Check(
        "Downloads", "VPN", "ok", f"{cfg.outgoing_interfaces} is up; BitTorrent pauses if it drops"
    )


async def check_sources(cfg: Config, http: httpx.AsyncClient | None) -> list[Check]:
    from cinemind.sources.registry import available

    known = available()
    unknown = [s for s in cfg.stream_sources if s not in known]
    out = []
    if unknown:
        out.append(
            Check(
                "Downloads",
                "Sources",
                "fail",
                f"unknown: {', '.join(unknown)}",
                f"Known sources: {', '.join(known)} (cinemind sources)",
            )
        )
    else:
        listed = ", ".join(cfg.stream_sources) or "none"
        if "stremio-addons" in cfg.stream_sources:
            listed += f" ({len(cfg.stream_addons)} addon(s) in stream_addons)"
        out.append(
            Check(
                "Downloads",
                "Sources",
                "ok" if cfg.stream_sources else "warn",
                listed,
                "" if cfg.stream_sources else "cinemind sources",
            )
        )
    if http is None:
        return out
    url = cfg.meta_addons[0] if cfg.meta_addons else ""
    if not url:
        out.append(
            Check(
                "Downloads",
                "Metadata",
                "fail",
                "meta_addons is empty",
                "cinemind config reset, or set meta_addons",
            )
        )
        return out
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        name = (resp.json() or {}).get("name") or url
        out.append(Check("Downloads", "Metadata", "ok", f"{name} answers"))
    except (httpx.HTTPError, ValueError) as exc:
        out.append(
            Check(
                "Downloads",
                "Metadata",
                "fail",
                f"{url} doesn't answer ({exc})",
                "Check the connection; searches and plans need it.",
            )
        )
    return out


# -- ingest and subtitles --------------------------------------------------------------------------


def _tool_version(path: str) -> str:
    try:
        run = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    words = (run.stdout.splitlines() or [""])[0].split()
    return words[2] if len(words) > 2 and words[1] == "version" else ""


def check_ffmpeg(cfg: Config) -> list[Check]:
    from cinemind.ingest.probe import INSTALL_HINT, ToolMissing, find_tool

    needed = cfg.auto_ingest or cfg.auto_subtitles
    out = []
    for tool, override in (("ffmpeg", cfg.ffmpeg_path), ("ffprobe", cfg.ffprobe_path)):
        try:
            path = find_tool(tool, override)
        except ToolMissing as exc:
            out.append(
                Check(
                    "Ingest",
                    tool,
                    "fail" if needed else "warn",
                    str(exc).split(". ")[0],
                    INSTALL_HINT + ". Ingest and the subtitle audio check need it.",
                )
            )
            continue
        version = _tool_version(path)
        out.append(Check("Ingest", tool, "ok", f"{version} at {path}" if version else path))
    return out


def check_ingest(cfg: Config) -> list[Check]:
    if not cfg.ingest_root:
        status: Status = "fail" if cfg.auto_ingest else "skip"
        return [
            Check(
                "Ingest",
                "Library share",
                status,
                "ingest_root isn't set",
                "cinemind config set ingest_root \\\\nas\\Media",
            )
        ]
    from cinemind.ingest.profile import load_profile

    out = []
    root = Path(cfg.ingest_root)
    if not root.is_dir():
        out.append(
            Check(
                "Ingest",
                "Library share",
                "fail",
                f"{root} can't be reached",
                "Is the NAS on, and the share mounted or reachable from here?",
            )
        )
    else:
        ok, why = _writable(root)
        out.append(
            Check(
                "Ingest",
                "Library share",
                "ok" if ok else "fail",
                str(root) + ("" if ok else f" ({why})"),
                "" if ok else "Give this user write access to the share.",
            )
        )
    try:
        profile = load_profile(cfg.ingest_profile)
        out.append(
            Check("Ingest", "TV profile", "ok", getattr(profile, "name", "") or cfg.ingest_profile)
        )
    except Exception as exc:
        out.append(
            Check(
                "Ingest",
                "TV profile",
                "fail",
                f"{cfg.ingest_profile}: {exc}",
                "cinemind config set ingest_profile samsung-q60b, or fix the file",
            )
        )
    return out


async def check_plex(cfg: Config, http: httpx.AsyncClient | None) -> Check:
    if not cfg.plex_url:
        return Check("Ingest", "Plex", "skip", "plex_url isn't set: no library refreshes")
    token = get_secret("plex")
    if not token:
        return Check(
            "Ingest",
            "Plex",
            "fail",
            "no Plex token",
            f"cinemind secrets set plex (or set {env_name('plex')})",
        )
    if http is None:
        return Check("Ingest", "Plex", "skip", "token set; not tried (--offline)")
    from cinemind.ingest.plex import Plex

    try:
        sections = await Plex(cfg.plex_url, token, http).sections()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return Check(
                "Ingest",
                "Plex",
                "fail",
                "Plex rejected the token",
                "cinemind secrets set plex, with a current X-Plex-Token",
            )
        return Check("Ingest", "Plex", "fail", f"Plex returned HTTP {exc.response.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        return Check(
            "Ingest",
            "Plex",
            "fail",
            f"{cfg.plex_url} doesn't answer ({exc})",
            "Is Plex running, and plex_url right?",
        )
    return Check("Ingest", "Plex", "ok", f"{cfg.plex_url}: {len(sections)} libraries")


def check_subtitles(cfg: Config) -> Check:
    if not cfg.auto_subtitles and not cfg.opensubtitles_username:
        return Check(
            "Subtitles",
            "Providers",
            "skip",
            "auto_subtitles is off (Wizdom needs no account, for Hebrew)",
        )
    if not cfg.opensubtitles_username:
        status: Status = "ok" if cfg.subtitle_language == "he" else "warn"
        return Check(
            "Subtitles",
            "Providers",
            status,
            "Wizdom only (Hebrew): no OpenSubtitles account set",
            ""
            if status == "ok"
            else "cinemind config set opensubtitles_username <you>, then cinemind secrets "
            "set opensubtitles_api_key and opensubtitles_password",
        )
    missing = [n for n in ("opensubtitles_api_key", "opensubtitles_password") if not get_secret(n)]
    if missing:
        return Check(
            "Subtitles",
            "Providers",
            "fail",
            f"OpenSubtitles user {cfg.opensubtitles_username}, but no {' or '.join(missing)}",
            " · ".join(f"cinemind secrets set {n}" for n in missing),
        )
    return Check(
        "Subtitles", "Providers", "ok", f"Wizdom, and OpenSubtitles as {cfg.opensubtitles_username}"
    )


def check_keyring() -> Check:
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception as exc:
        return Check(
            "Setup",
            "Keyring",
            "warn",
            f"none ({exc})",
            "Set secrets as CINEMIND_<NAME> environment variables instead",
        )
    module, name = type(backend).__module__, type(backend).__name__
    if module.endswith((".fail", ".null")):  # what keyring falls back to without a backend
        return Check(
            "Setup",
            "Keyring",
            "warn",
            "no system keyring (as in Docker)",
            "Set secrets as CINEMIND_<NAME> environment variables instead",
        )
    return Check("Setup", "Keyring", "ok", f"{module.removeprefix('keyring.backends.')}.{name}")


# -- AI --------------------------------------------------------------------------------------------


def _claude_credentials() -> str:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            return var
    return ""


def check_claude(cfg: Config) -> Check:
    if not cfg.llm_enabled:
        return Check("AI", "Claude", "skip", "llm_enabled is off: selections are parsed only")
    version = _version("anthropic")
    if version is None:
        return Check(
            "AI",
            "Claude",
            "warn",
            "the SDK isn't installed: `cinemind ask` and plain-language selections won't work",
            "pip install 'cinemind[ai]'",
        )
    var = _claude_credentials()
    if var:
        return Check(
            "AI",
            "Claude",
            "ok",
            f"{cfg.llm_model} with {var} (anthropic {version}; checked on first use)",
        )
    return Check(
        "AI",
        "Claude",
        "warn",
        "no ANTHROPIC_API_KEY in the environment (an `ant auth login` profile also "
        "works, but isn't checked here)",
        "Set ANTHROPIC_API_KEY, or run ant auth login",
    )


async def check_judge(cfg: Config, http: httpx.AsyncClient | None) -> Check:
    if cfg.judge_provider == "off":
        return Check("AI", "Pack-file judge", "skip", "judge_provider is off: file names only")
    if cfg.judge_provider == "anthropic":
        claude = check_claude(cfg)
        return Check(
            "AI", "Pack-file judge", claude.status, f"on Claude: {claude.detail}", claude.fix
        )
    model = cfg.judge_model
    if http is None:
        return Check("AI", "Pack-file judge", "skip", f"ollama/{model}; not tried (--offline)")
    url = cfg.ollama_url.rstrip("/")
    try:
        resp = await http.get(f"{url}/api/tags")
        resp.raise_for_status()
        names = {m.get("name", "") for m in (resp.json() or {}).get("models", [])}
    except (httpx.HTTPError, ValueError):
        return Check(
            "AI",
            "Pack-file judge",
            "warn",
            f"Ollama isn't running at {url}: unclear pack files are re-planned",
            f"Install Ollama and run `ollama pull {model}`, or cinemind config set "
            "judge_provider off",
        )
    if model in names or f"{model}:latest" in names:
        return Check("AI", "Pack-file judge", "ok", f"ollama/{model} at {url}")
    return Check(
        "AI",
        "Pack-file judge",
        "warn",
        f"Ollama is running, but has no {model}",
        f"ollama pull {model}",
    )


def check_mcp() -> Check:
    version = _version("mcp")
    if version is None:
        return Check(
            "AI",
            "MCP server",
            "skip",
            "the mcp extra isn't installed",
            "pip install 'cinemind[mcp]'",
        )
    if not Path(sys.executable).is_file():
        return Check(
            "AI",
            "MCP server",
            "fail",
            f"{sys.executable} doesn't exist",
            "Reinstall cinemind; Claude Desktop starts it with this Python",
        )
    return Check(
        "AI",
        "MCP server",
        "ok",
        f"mcp {version}; cinemind mcp --print-config gives the Claude Desktop entry",
    )


# -- all of it -------------------------------------------------------------------------------------


async def run_checks(
    cfg: Config, offline: bool = False, http: httpx.AsyncClient | None = None
) -> list[Check]:
    """Every check, in display order. ``http`` is for tests; ``offline`` skips the network."""
    own = None
    if http is None and not offline:
        from cinemind.core.config import PROJECT_URL

        own = http = httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": f"cinemind-doctor (+{PROJECT_URL})"},
        )
    client = None if offline else http
    try:
        sources, plex, judge = await asyncio.gather(
            check_sources(cfg, client), check_plex(cfg, client), check_judge(cfg, client)
        )
    finally:
        if own is not None:
            await own.aclose()
    return [
        *check_setup(),
        check_keyring(),
        *check_libraries(cfg),
        check_torrent(cfg),
        check_vpn(cfg),
        *sources,
        *check_ffmpeg(cfg),
        *check_ingest(cfg),
        plex,
        check_subtitles(cfg),
        check_claude(cfg),
        judge,
        check_mcp(),
    ]


def summary(checks: list[Check]) -> dict[str, Any]:
    counts = {s: sum(c.status == s for c in checks) for s in ("ok", "warn", "fail", "skip")}
    return {"healthy": counts["fail"] == 0, **counts}
