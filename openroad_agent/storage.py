"""SQLite-backed metadata store for the Chippilot web UI."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


class ChippilotStore:
    """Small SQLite store for users, sessions, jobs, and agent threads."""

    def __init__(self, work_dir: str | Path) -> None:
        self.work_dir = Path(work_dir)
        self.db_path = self.work_dir / "chippilot.sqlite3"

    def initialize(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL DEFAULT '',
                    salt TEXT NOT NULL DEFAULT '',
                    disabled_password INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    name_hint TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_message_at TEXT NOT NULL DEFAULT '',
                    deleted_at TEXT,
                    PRIMARY KEY (user_id, session_name),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_active
                    ON sessions(user_id, deleted_at, created_at DESC);
                CREATE TABLE IF NOT EXISTS agent_threads (
                    user_id TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_name)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    job_type TEXT NOT NULL DEFAULT 'chat',
                    timeout_seconds INTEGER NOT NULL DEFAULT 600,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_user_session
                    ON jobs(user_id, session_name, created_at DESC);
                """
            )
            self._ensure_column(conn, "jobs", "job_type", "TEXT NOT NULL DEFAULT 'chat'")
            self._ensure_column(conn, "jobs", "timeout_seconds", "INTEGER NOT NULL DEFAULT 600")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def user_exists(self, user_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_user(self, user: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, password_hash, salt, disabled_password, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    password_hash = excluded.password_hash,
                    salt = excluded.salt,
                    disabled_password = excluded.disabled_password,
                    created_at = excluded.created_at
                """,
                (
                    user["user_id"],
                    user["username"],
                    user.get("password_hash", ""),
                    user.get("salt", ""),
                    1 if user.get("disabled_password") else 0,
                    user.get("created_at") or utc_now(),
                ),
            )

    def create_user(self, user: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, password_hash, salt, disabled_password, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user["user_id"],
                    user["username"],
                    user.get("password_hash", ""),
                    user.get("salt", ""),
                    1 if user.get("disabled_password") else 0,
                    user.get("created_at") or utc_now(),
                ),
            )

    def list_sessions(self, user_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_name
                FROM sessions
                WHERE user_id = ? AND deleted_at IS NULL
                ORDER BY COALESCE(NULLIF(last_message_at, ''), created_at) DESC
                """,
                (user_id,),
            ).fetchall()
        return [row["session_name"] for row in rows]

    def get_session(self, user_id: str, session_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM sessions
                WHERE user_id = ? AND session_name = ? AND deleted_at IS NULL
                """,
                (user_id, session_name),
            ).fetchone()
        return dict(row) if row else None

    def upsert_session(
        self,
        user_id: str,
        session_name: str,
        path: str | Path,
        name_hint: str = "",
        created_at: str = "",
        last_message_at: str = "",
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    user_id, session_name, path, name_hint, created_at, last_message_at, deleted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(user_id, session_name) DO UPDATE SET
                    path = excluded.path,
                    name_hint = CASE
                        WHEN excluded.name_hint != '' THEN excluded.name_hint
                        ELSE sessions.name_hint
                    END,
                    created_at = CASE
                        WHEN excluded.created_at != '' THEN excluded.created_at
                        ELSE sessions.created_at
                    END,
                    last_message_at = CASE
                        WHEN excluded.last_message_at != '' THEN excluded.last_message_at
                        ELSE sessions.last_message_at
                    END,
                    deleted_at = NULL
                """,
                (
                    user_id,
                    session_name,
                    str(path),
                    name_hint,
                    created_at or now,
                    last_message_at,
                ),
            )

    def rename_session(self, user_id: str, old_name: str, new_name: str, new_path: str | Path) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET session_name = ?, path = ?
                WHERE user_id = ? AND session_name = ? AND deleted_at IS NULL
                """,
                (new_name, str(new_path), user_id, old_name),
            )
            conn.execute(
                """
                UPDATE agent_threads
                SET session_name = ?, updated_at = ?
                WHERE user_id = ? AND session_name = ?
                """,
                (new_name, utc_now(), user_id, old_name),
            )
            conn.execute(
                """
                UPDATE jobs
                SET session_name = ?, updated_at = ?
                WHERE user_id = ? AND session_name = ?
                """,
                (new_name, utc_now(), user_id, old_name),
            )

    def touch_session(self, user_id: str, session_name: str, last_message_at: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET last_message_at = ?
                WHERE user_id = ? AND session_name = ? AND deleted_at IS NULL
                """,
                (last_message_at or utc_now(), user_id, session_name),
            )

    def mark_session_deleted(self, user_id: str, session_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET deleted_at = ?
                WHERE user_id = ? AND session_name = ?
                """,
                (utc_now(), user_id, session_name),
            )

    def get_or_create_thread_id(self, user_id: str, session_name: str, thread_id: str) -> str:
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT thread_id
                FROM agent_threads
                WHERE user_id = ? AND session_name = ?
                """,
                (user_id, session_name),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE agent_threads
                    SET updated_at = ?
                    WHERE user_id = ? AND session_name = ?
                    """,
                    (now, user_id, session_name),
                )
                return str(row["thread_id"])
            conn.execute(
                """
                INSERT INTO agent_threads (
                    user_id, session_name, thread_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, session_name, thread_id, now, now),
            )
        return thread_id

    def get_thread_id(self, user_id: str, session_name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT thread_id
                FROM agent_threads
                WHERE user_id = ? AND session_name = ?
                """,
                (user_id, session_name),
            ).fetchone()
        return str(row["thread_id"]) if row else None

    def move_thread(self, user_id: str, old_name: str, new_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_threads
                SET session_name = ?, updated_at = ?
                WHERE user_id = ? AND session_name = ?
                """,
                (new_name, utc_now(), user_id, old_name),
            )

    def create_job(
        self,
        job_id: str,
        user_id: str,
        session_name: str,
        message: str = "",
        job_type: str = "chat",
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        now = utc_now()
        row = {
            "job_id": job_id,
            "user_id": user_id,
            "session_name": session_name,
            "job_type": job_type,
            "timeout_seconds": timeout_seconds,
            "status": "queued",
            "message": message,
            "error": "",
            "result": {},
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "finished_at": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, user_id, session_name, job_type, timeout_seconds,
                    status, message, error, result_json, created_at, started_at,
                    updated_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    session_name,
                    job_type,
                    timeout_seconds,
                    row["status"],
                    message,
                    "",
                    "{}",
                    now,
                    None,
                    now,
                    None,
                ),
            )
        return row

    def get_job(self, user_id: str, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND job_id = ?",
                (user_id, job_id),
            ).fetchone()
        return self._job_row(row) if row else None

    def fail_interrupted_jobs(self, reason: str) -> int:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    message = 'Agent job interrupted.',
                    error = ?,
                    updated_at = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('queued', 'running')
                """,
                (reason, now, now),
            )
            return cursor.rowcount

    def update_job(
        self,
        job_id: str,
        status: str,
        *,
        message: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        sets = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now]
        if status == "running":
            sets.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in {"completed", "failed", "cancelled"}:
            sets.append("finished_at = COALESCE(finished_at, ?)")
            values.append(now)
        if message is not None:
            sets.append("message = ?")
            values.append(message)
        if error is not None:
            sets.append("error = ?")
            values.append(error)
        if result is not None:
            sets.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        values.append(job_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", values)

    def _job_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["result"] = json.loads(data.pop("result_json") or "{}")
        except json.JSONDecodeError:
            data["result"] = {}
        return data
