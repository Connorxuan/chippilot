"""SQLite-backed queue and run metadata."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_adapter.models import RunStatus, TERMINAL_STATUSES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RunStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    worker_pid INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_benchmark_runs_status
                    ON benchmark_runs(status, created_at);
                CREATE TABLE IF NOT EXISTS remote_jobs (
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    address TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, job_id)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(remote_jobs)")}
            if "address" not in columns:
                conn.execute("ALTER TABLE remote_jobs ADD COLUMN address TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        value["result"] = json.loads(value.pop("result_json") or "{}")
        return value

    def create(self, run_id: str, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM benchmark_runs WHERE request_id = ?", (request["request_id"],)
            ).fetchone()
            if existing:
                return self._row(existing), False  # type: ignore[return-value]
            conn.execute(
                """INSERT INTO benchmark_runs
                   (run_id, request_id, task_id, status, request_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    request["request_id"],
                    request["task"]["task_id"],
                    RunStatus.queued.value,
                    json.dumps(request, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(run_id), True  # type: ignore[return-value]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM benchmark_runs WHERE run_id = ?", (run_id,)).fetchone())

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM benchmark_runs WHERE status = ? ORDER BY created_at LIMIT 1",
                (RunStatus.queued.value,),
            ).fetchone()
            if not row:
                return None
            now = utc_now()
            changed = conn.execute(
                "UPDATE benchmark_runs SET status = ?, started_at = ?, updated_at = ? "
                "WHERE run_id = ? AND status = ?",
                (RunStatus.preparing.value, now, now, row["run_id"], RunStatus.queued.value),
            ).rowcount
            if not changed:
                return None
        return self.get(row["run_id"])

    def update(
        self,
        run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        worker_pid: int | None = None,
    ) -> None:
        now = utc_now()
        sets = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now]
        if result is not None:
            sets.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            sets.append("error = ?")
            values.append(error)
        if worker_pid is not None:
            sets.append("worker_pid = ?")
            values.append(worker_pid)
        if status in TERMINAL_STATUSES:
            sets.append("finished_at = COALESCE(finished_at, ?)")
            values.append(now)
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE benchmark_runs SET {', '.join(sets)} WHERE run_id = ?", values)

    def request_cancel(self, run_id: str) -> dict[str, Any] | None:
        current = self.get(run_id)
        if not current or current["status"] in TERMINAL_STATUSES:
            return current
        self.update(run_id, RunStatus.cancelling.value)
        return self.get(run_id)

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self._connect() as conn:
            return conn.execute(
                """UPDATE benchmark_runs SET status = ?, error = ?, updated_at = ?, finished_at = ?
                   WHERE status IN (?, ?, ?)""",
                (
                    RunStatus.failed.value,
                    "Adapter restarted while this run was active.",
                    now,
                    now,
                    RunStatus.preparing.value,
                    RunStatus.running.value,
                    RunStatus.cancelling.value,
                ),
            ).rowcount

    def register_remote_job(self, run_id: str, job_id: str, address: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO remote_jobs
                   (run_id, job_id, address, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (run_id, job_id, address, now, now),
            )

    def update_remote_job(self, run_id: str, job_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE remote_jobs SET status = ?, updated_at = ? WHERE run_id = ? AND job_id = ?",
                (status, utc_now(), run_id, job_id),
            )

    def active_remote_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_jobs WHERE run_id = ? AND status = 'active'", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]
