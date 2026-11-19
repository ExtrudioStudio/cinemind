"""SQLite state: download jobs, per-episode items, per-torrent resume data and hand-offs.

The database is opened in WAL mode so ``cinemind status`` (or a post-download hand-off recording
its result) can read and write while a download is running. Writes go through :meth:`Store._tx`,
which groups nested calls into one transaction, and the schema is versioned with
``PRAGMA user_version`` (see :data:`MIGRATIONS`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cinemind.core.models import Key, TorrentJob
from cinemind.planning.planner import Plan

# Version 1 is the original schema. Databases created before versioning have user_version 0
# and these tables already; "IF NOT EXISTS" makes the first migration a no-op for them.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id TEXT NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    quality TEXT NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    plan_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    job_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,
    info_hash TEXT,
    final_path TEXT,
    error TEXT,
    updated TEXT NOT NULL,
    PRIMARY KEY (job_id, key)
);
CREATE TABLE IF NOT EXISTS torrents (
    job_id INTEGER NOT NULL,
    info_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    job_json TEXT NOT NULL,
    resume BLOB,
    error TEXT,
    updated TEXT NOT NULL,
    PRIMARY KEY (job_id, info_hash)
);
"""

# Version 2: one row per post-download command run, so its exit code is known.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    path TEXT NOT NULL,
    batch TEXT NOT NULL,
    log_path TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    exit_code INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS handoffs_job ON handoffs (job_id);
"""

MIGRATIONS = (SCHEMA_V1, SCHEMA_V2)
SCHEMA_VERSION = len(MIGRATIONS)

OPEN_TORRENT = ("queued", "active", "paused")


def key_name(key: Key) -> str:
    return "movie" if key is None else key.code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class JobRow:
    id: int
    title: str
    type: str
    quality: str
    status: str
    created: str
    updated: str
    counts: dict[str, int]
    handoffs: dict[str, int] = field(default_factory=dict)  # HandoffRow.state -> count


@dataclass
class HandoffRow:
    id: int
    job_id: int
    key: str
    path: str
    batch: str
    log_path: str
    started: str
    finished: str | None
    exit_code: int | None
    error: str | None

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if self.exit_code is None:
            return "running"
        return "ok" if self.exit_code == 0 else "failed"


class Store:
    def __init__(self, path: Path, timeout: float = 5.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # isolation_level=None: no implicit transactions; _tx() opens them explicitly.
        self.db = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._depth = 0
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            self._migrate()
        except BaseException:
            self.db.close()  # the caller never gets a Store to close
            raise

    def close(self) -> None:
        self.db.close()

    @property
    def schema_version(self) -> int:
        return int(self.db.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"state.db is schema version {current}, newer than this version of cinemind "
                f"understands ({SCHEMA_VERSION}). Upgrade cinemind."
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            # executescript() commits first, so the transaction lives inside the script.
            script = MIGRATIONS[version - 1]
            try:
                self.db.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """One write transaction. Nested calls join the outermost one, so it is all or nothing."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")
        finally:
            self._depth = 0

    # -- jobs ------------------------------------------------------------------------------

    def create_job(self, plan: Plan) -> int:
        now = _now()
        with self._tx():
            cur = self.db.execute(
                "INSERT INTO jobs (title_id, title, type, quality, status, created, updated, "
                "plan_json) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    plan.title.id,
                    plan.title.name,
                    plan.title.type,
                    plan.quality,
                    now,
                    now,
                    json.dumps(plan.to_dict()),
                ),  # fmt: skip
            )
            assert cur.lastrowid is not None  # always set after a successful INSERT
            job_id = cur.lastrowid
            for key in plan.keys:
                if key in plan.existing:
                    self.set_item(job_id, key, "skipped", path=str(plan.existing[key]))
                elif key in plan.missing:
                    self.set_item(job_id, key, "failed", error=plan.missing[key])
            for job in plan.jobs:
                self.add_torrent(job_id, job)
        return job_id

    def save_plan(self, job_id: int, plan: Plan) -> None:
        with self._tx():
            self.db.execute(
                "UPDATE jobs SET plan_json = ?, updated = ? WHERE id = ?",
                (json.dumps(plan.to_dict()), _now(), job_id),
            )

    def load_plan(self, job_id: int) -> Plan:
        row = self.db.execute("SELECT plan_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"No download job #{job_id}")
        return Plan.from_dict(json.loads(row["plan_json"]))

    def set_job_status(self, job_id: int, status: str) -> None:
        with self._tx():
            self.db.execute(
                "UPDATE jobs SET status = ?, updated = ? WHERE id = ?", (status, _now(), job_id)
            )

    def jobs(self, include_finished: bool = True, limit: int = 30) -> list[JobRow]:
        sql = "SELECT * FROM jobs"
        if not include_finished:
            sql += " WHERE status IN ('running', 'paused')"
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            counts = dict(
                self.db.execute(
                    "SELECT status, COUNT(*) FROM items WHERE job_id = ? GROUP BY status",
                    (r["id"],),
                ).fetchall()
            )
            handoffs = dict(
                self.db.execute(
                    "SELECT CASE WHEN error IS NOT NULL THEN 'error' "
                    "WHEN exit_code IS NULL THEN 'running' WHEN exit_code = 0 THEN 'ok' "
                    "ELSE 'failed' END AS state, COUNT(*) FROM handoffs WHERE job_id = ? "
                    "GROUP BY state",
                    (r["id"],),
                ).fetchall()
            )
            out.append(JobRow(r["id"], r["title"], r["type"], r["quality"], r["status"],
                              r["created"], r["updated"], counts, handoffs))  # fmt: skip
        return out

    def finish_job(self, job_id: int) -> str:
        """Set the final status from the items' states and return it."""
        open_ = self.db.execute(
            "SELECT COUNT(*) FROM torrents WHERE job_id = ? AND status IN (?, ?, ?)",
            (job_id, *OPEN_TORRENT),
        ).fetchone()[0]
        counts = dict(
            self.db.execute(
                "SELECT status, COUNT(*) FROM items WHERE job_id = ? GROUP BY status", (job_id,)
            ).fetchall()
        )
        if open_:
            status = "paused"
        elif counts.get("failed"):
            status = "partial" if counts.get("done") else "failed"
        else:
            status = "done"
        self.set_job_status(job_id, status)
        return status

    # -- items -----------------------------------------------------------------------------

    def set_item(
        self,
        job_id: int,
        key: Key,
        status: str,
        info_hash: str | None = None,
        path: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._tx():
            self.db.execute(
                "INSERT INTO items (job_id, key, status, info_hash, final_path, error, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (job_id, key) DO UPDATE SET "
                "status = excluded.status, info_hash = COALESCE(excluded.info_hash, info_hash), "
                "final_path = COALESCE(excluded.final_path, final_path), error = excluded.error, "
                "updated = excluded.updated",
                (job_id, key_name(key), status, info_hash, path, error, _now()),
            )

    # -- torrents --------------------------------------------------------------------------

    def add_torrent(self, job_id: int, job: TorrentJob) -> None:
        """Insert a torrent, or add new targets to one that's already recorded."""
        with self._tx():  # read and write in one transaction
            row = self.db.execute(
                "SELECT job_json FROM torrents WHERE job_id = ? AND info_hash = ?",
                (job_id, job.info_hash),
            ).fetchone()
            if row is not None:
                stored = TorrentJob.from_dict(json.loads(row["job_json"]))
                known = {str(t.final_path) for t in stored.targets}
                stored.targets += [t for t in job.targets if str(t.final_path) not in known]
                job = stored
            self.db.execute(
                "INSERT INTO torrents (job_id, info_hash, status, job_json, updated) "
                "VALUES (?, ?, 'queued', ?, ?) ON CONFLICT (job_id, info_hash) DO UPDATE SET "
                "status = 'queued', job_json = excluded.job_json, updated = excluded.updated",
                (job_id, job.info_hash, json.dumps(job.to_dict()), _now()),
            )
            for target in job.targets:
                for key in target.refs or (None,):
                    self.set_item(job_id, key, "queued", info_hash=job.info_hash,
                                  path=str(target.final_path))  # fmt: skip

    def update_torrent(
        self,
        job_id: int,
        info_hash: str,
        status: str | None = None,
        job: TorrentJob | None = None,
        resume: bytes | None = None,
        error: str | None = None,
    ) -> None:
        sets: list[str] = ["updated = ?"]
        args: list[str | bytes] = [_now()]
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if job is not None:
            sets.append("job_json = ?")
            args.append(json.dumps(job.to_dict()))
        if resume is not None:
            sets.append("resume = ?")
            args.append(resume)
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        with self._tx():
            self.db.execute(
                f"UPDATE torrents SET {', '.join(sets)} WHERE job_id = ? AND info_hash = ?",
                (*args, job_id, info_hash),
            )

    def open_torrents(self, job_id: int) -> list[tuple[TorrentJob, bytes | None]]:
        rows = self.db.execute(
            "SELECT job_json, resume FROM torrents WHERE job_id = ? AND status IN (?, ?, ?)",
            (job_id, *OPEN_TORRENT),
        ).fetchall()
        return [(TorrentJob.from_dict(json.loads(r["job_json"])), r["resume"]) for r in rows]

    def finished_torrents(self, job_id: int) -> list[TorrentJob]:
        rows = self.db.execute(
            "SELECT job_json FROM torrents WHERE job_id = ? AND status = 'done'", (job_id,)
        ).fetchall()
        return [TorrentJob.from_dict(json.loads(r["job_json"])) for r in rows]

    def pause_open(self, job_id: int) -> None:
        with self._tx():
            self.db.execute(
                "UPDATE torrents SET status = 'paused', updated = ? "
                "WHERE job_id = ? AND status IN ('queued', 'active')",
                (_now(), job_id),
            )

    # -- hand-offs (post_download_command runs) --------------------------------------------

    def add_handoff(self, job_id: int, key: str, path: str, batch: str, log_path: str) -> int:
        with self._tx():
            cur = self.db.execute(
                "INSERT INTO handoffs (job_id, key, path, batch, log_path, started) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, key, path, batch, log_path, _now()),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_handoff(
        self, handoff_id: int, exit_code: int | None, error: str | None = None
    ) -> None:
        with self._tx():
            self.db.execute(
                "UPDATE handoffs SET finished = ?, exit_code = ?, error = ? WHERE id = ?",
                (_now(), exit_code, error, handoff_id),
            )

    def handoffs(self, job_id: int) -> list[HandoffRow]:
        rows = self.db.execute(
            "SELECT * FROM handoffs WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [HandoffRow(**dict(r)) for r in rows]
