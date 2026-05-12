"""Persistent LangGraph checkpointer backed by SQLite."""

from __future__ import annotations

import random
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


class SQLiteCheckpointSaver(BaseCheckpointSaver[str]):
    """SQLite checkpointer compatible with LangGraph's saver protocol."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._setup()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _setup(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    checkpoint_id TEXT NOT NULL,
                    checkpoint_type TEXT NOT NULL,
                    checkpoint_blob BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_blob BLOB NOT NULL,
                    parent_checkpoint_id TEXT,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS langgraph_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    write_idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    task_path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx
                    )
                );
                CREATE TABLE IF NOT EXISTS langgraph_blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_blob BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                );
                CREATE INDEX IF NOT EXISTS idx_langgraph_checkpoints_thread
                    ON langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC);
                """
            )

    def _dump_typed(self, value: Any) -> tuple[str, bytes]:
        kind, payload = self.serde.dumps_typed(value)
        return str(kind), payload

    def _load_typed(self, kind: str, payload: bytes) -> Any:
        return self.serde.loads_typed((kind, payload))

    def _load_blobs(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        checkpoint_ns: str,
        versions: ChannelVersions,
    ) -> dict[str, Any]:
        channel_values: dict[str, Any] = {}
        for channel, version in versions.items():
            row = conn.execute(
                """
                SELECT value_type, value_blob
                FROM langgraph_blobs
                WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version = ?
                """,
                (thread_id, checkpoint_ns, channel, str(version)),
            ).fetchone()
            if row and row["value_type"] != "empty":
                channel_values[channel] = self._load_typed(row["value_type"], row["value_blob"])
        return channel_values

    def _pending_writes(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[tuple[str, str, Any]]:
        rows = conn.execute(
            """
            SELECT task_id, channel, value_type, value_blob
            FROM langgraph_writes
            WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
            ORDER BY task_id, write_idx
            """,
            (thread_id, checkpoint_ns, checkpoint_id),
        ).fetchall()
        return [
            (
                row["task_id"],
                row["channel"],
                self._load_typed(row["value_type"], row["value_blob"]),
            )
            for row in rows
        ]

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        with self._lock, self._connect() as conn:
            if checkpoint_id:
                row = conn.execute(
                    """
                    SELECT *
                    FROM langgraph_checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT *
                    FROM langgraph_checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY checkpoint_id DESC
                    LIMIT 1
                    """,
                    (thread_id, checkpoint_ns),
                ).fetchone()
            if not row:
                return None

            checkpoint = self._load_typed(row["checkpoint_type"], row["checkpoint_blob"])
            checkpoint_id = row["checkpoint_id"]
            checkpoint["channel_values"] = self._load_blobs(
                conn,
                thread_id,
                checkpoint_ns,
                checkpoint["channel_versions"],
            )
            parent_checkpoint_id = row["parent_checkpoint_id"]
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_id,
                    }
                },
                checkpoint=checkpoint,
                metadata=self._load_typed(row["metadata_type"], row["metadata_blob"]),
                pending_writes=self._pending_writes(
                    conn,
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                ),
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": parent_checkpoint_id,
                        }
                    }
                    if parent_checkpoint_id
                    else None
                ),
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        query = "SELECT * FROM langgraph_checkpoints"
        clauses: list[str] = []
        values: list[Any] = []
        if config:
            clauses.append("thread_id = ?")
            values.append(config["configurable"]["thread_id"])
            if checkpoint_ns := config["configurable"].get("checkpoint_ns"):
                clauses.append("checkpoint_ns = ?")
                values.append(checkpoint_ns)
            if checkpoint_id := get_checkpoint_id(config):
                clauses.append("checkpoint_id = ?")
                values.append(checkpoint_id)
        if before and (before_id := get_checkpoint_id(before)):
            clauses.append("checkpoint_id < ?")
            values.append(before_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY checkpoint_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, values).fetchall()
            for row in rows:
                metadata = self._load_typed(row["metadata_type"], row["metadata_blob"])
                if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                    continue
                item = self.get_tuple(
                    {
                        "configurable": {
                            "thread_id": row["thread_id"],
                            "checkpoint_ns": row["checkpoint_ns"],
                            "checkpoint_id": row["checkpoint_id"],
                        }
                    }
                )
                if item:
                    yield item

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        c = checkpoint.copy()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
        checkpoint_type, checkpoint_blob = self._dump_typed(c)
        metadata_type, metadata_blob = self._dump_typed(
            get_checkpoint_metadata(config, metadata)
        )
        with self._lock, self._connect() as conn:
            for channel, version in new_versions.items():
                if channel in values:
                    value_type, value_blob = self._dump_typed(values[channel])
                else:
                    value_type, value_blob = "empty", b""
                conn.execute(
                    """
                    INSERT OR REPLACE INTO langgraph_blobs (
                        thread_id, checkpoint_ns, channel, version, value_type, value_blob
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, checkpoint_ns, channel, str(version), value_type, value_blob),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO langgraph_checkpoints (
                    thread_id, checkpoint_ns, checkpoint_id,
                    checkpoint_type, checkpoint_blob, metadata_type, metadata_blob,
                    parent_checkpoint_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint["id"],
                    checkpoint_type,
                    checkpoint_blob,
                    metadata_type,
                    metadata_blob,
                    config["configurable"].get("checkpoint_id"),
                ),
            )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with self._lock, self._connect() as conn:
            for idx, (channel, value) in enumerate(writes):
                write_idx = WRITES_IDX_MAP.get(channel, idx)
                if write_idx >= 0:
                    existing = conn.execute(
                        """
                        SELECT 1
                        FROM langgraph_writes
                        WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                          AND task_id = ? AND write_idx = ?
                        """,
                        (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx),
                    ).fetchone()
                    if existing:
                        continue
                value_type, value_blob = self._dump_typed(value)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO langgraph_writes (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx,
                        channel, value_type, value_blob, task_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        task_id,
                        write_idx,
                        channel,
                        value_type,
                        value_blob,
                        task_path,
                    ),
                )

    def delete_thread(self, thread_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM langgraph_checkpoints WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM langgraph_writes WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM langgraph_blobs WHERE thread_id = ?", (thread_id,))

    def compact_thread(
        self,
        thread_id: str,
        new_messages: list[Any],
    ) -> None:
        """Rewrite a thread's history, replacing old messages with *new_messages*.

        This creates a fresh checkpoint for the thread so that subsequent
        LangGraph loads see only the compacted message list.

        Args:
            thread_id: The LangGraph thread to rewrite.
            new_messages: The replacement message list (usually a summary
                          SystemMessage + recent messages).
        """
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        tuple_ = self.get_tuple(config)
        if tuple_ is None:
            return

        checkpoint = dict(tuple_.checkpoint)
        metadata = dict(tuple_.metadata) if tuple_.metadata else {}

        # Replace messages channel
        channel_values = checkpoint.setdefault("channel_values", {})
        channel_values["messages"] = list(new_messages)

        # New checkpoint id so LangGraph treats this as the latest version
        old_id = checkpoint.get("id", "")
        new_id = f"{old_id}_compact_{int(time.time() * 1000)}"
        checkpoint["id"] = new_id

        # Bump every channel version so blobs are re-written
        current_versions = checkpoint.get("channel_versions", {})
        new_versions: dict[str, str] = {}
        for channel, version in current_versions.items():
            new_versions[channel] = self.get_next_version(version, None)
        if "messages" not in current_versions:
            new_versions["messages"] = self.get_next_version(None, None)
            checkpoint["channel_versions"] = current_versions

        # Persist the new checkpoint
        self.put(config, checkpoint, metadata, new_versions)

        # Drop old checkpoints and writes for this thread, keeping only the new one
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM langgraph_checkpoints WHERE thread_id = ? AND checkpoint_id != ?",
                (thread_id, new_id),
            )
            conn.execute(
                "DELETE FROM langgraph_writes WHERE thread_id = ?",
                (thread_id,),
            )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        return self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"


_CHECKPOINTERS: dict[Path, SQLiteCheckpointSaver] = {}
_CHECKPOINTERS_LOCK = threading.Lock()


def get_sqlite_checkpointer(work_dir: str | Path) -> SQLiteCheckpointSaver:
    db_path = Path(work_dir) / "chippilot.sqlite3"
    with _CHECKPOINTERS_LOCK:
        saver = _CHECKPOINTERS.get(db_path)
        if saver is None:
            saver = SQLiteCheckpointSaver(db_path)
            _CHECKPOINTERS[db_path] = saver
        return saver
