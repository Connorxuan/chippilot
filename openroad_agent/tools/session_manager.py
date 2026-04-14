"""Session management — group all files from a conversation into one folder.

Each conversation (chat session) gets a timestamped directory under
``<work_dir>/sessions/<YYYYMMDD_HHMMSS>_<name>/``.  All design files,
Yosys synthesis outputs, OpenROAD P&R results, DSE runs, logs, and
reports are organised within that single directory.

Directory layout
----------------
::

    openroad_work/sessions/20260225_143022_counter4_nangate45/
        session.json          ← metadata (creation time, runs list, …)
        designs/              ← user-uploaded RTL, SDC, etc.
            counter4.v
            counter4.sdc
        synth_counter4/       ← a Yosys synthesis run
            synth_counter4.ys
            synth_counter4.log
            results/
            reports/
        pr_counter4/          ← an OpenROAD P&R run
            pr_counter4.tcl
            pr_counter4.log
            results/
        dse_density_0.3/      ← a DSE exploration run
            …

The active session is stored as a module-level singleton so that all
tools (file_manager, openroad_runner, yosys_runner, ray_executor) can
discover it without passing extra arguments through every call chain.
"""

from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from langchain_core.tools import tool

from openroad_agent.config import OpenROADConfig
from openroad_agent.tools.artifacts import list_session_artifacts


_NON_RUN_DIRS = {"designs", "attachments", "__pycache__"}
_SESSION_TZ = timezone(timedelta(hours=8))
_current_session: ContextVar["SessionManager | None"] = ContextVar(
    "openroad_agent_current_session",
    default=None,
)


def _now() -> datetime:
    return datetime.now(_SESSION_TZ)


def _session_timestamp() -> str:
    return _now().strftime("%Y%m%d_%H%M%S")


def _iso_timestamp() -> str:
    return _now().isoformat()


# ═══════════════════════════════════════════════════════════════════════
# SessionManager class
# ═══════════════════════════════════════════════════════════════════════

class SessionManager:
    """Manages the working directory for a single conversation session."""

    # Backward-compatible fallback for CLI flows outside an async request context.
    _current: "SessionManager | None" = None

    def __init__(self, work_dir: str, name_hint: str = "") -> None:
        self.timestamp = _session_timestamp()
        self.name_hint = name_hint

        # Build session directory name
        if name_hint:
            safe = re.sub(r"[^\w\-]", "_", name_hint)[:40].strip("_")
            self.session_name = f"{self.timestamp}_{safe}"
        else:
            self.session_name = self.timestamp

        self.work_dir = work_dir
        self.base_dir = os.path.join(work_dir, "sessions", self.session_name)
        os.makedirs(self.base_dir, exist_ok=True)

        # Standard sub-directories
        self._designs_dir = os.path.join(self.base_dir, "designs")
        os.makedirs(self._designs_dir, exist_ok=True)

        # Track runs for metadata
        self._runs: list[dict[str, Any]] = []

        # Chat log path
        self._chat_log_path = os.path.join(self.base_dir, "chat_log.jsonl")

        # Write initial metadata
        self._write_meta()

    # ── Directory helpers ──────────────────────────────────────────────

    @property
    def designs_dir(self) -> str:
        """Directory for user-uploaded design files."""
        return self._designs_dir

    @property
    def chat_log_path(self) -> str:
        """Path to the session chat log."""
        return self._chat_log_path

    def run_dir(self, run_label: str) -> str:
        """Return (and create) a run directory inside the session.

        Args:
            run_label: Short label like "synth_counter4", "pr_counter4".

        Returns:
            Absolute path to ``<session_base>/<run_label>/``.
        """
        d = os.path.join(self.base_dir, run_label)
        os.makedirs(d, exist_ok=True)
        return d

    def register_run(
        self,
        run_label: str,
        tool: str,
        success: bool | None = None,
        metrics: dict | None = None,
    ) -> None:
        """Record a run in the session metadata."""
        entry: dict[str, Any] = {
            "run_label": run_label,
            "tool": tool,
            "timestamp": _iso_timestamp(),
        }
        if success is not None:
            entry["success"] = success
        if metrics:
            entry["metrics"] = metrics
        self._runs.append(entry)
        self._write_meta()

    # ── Chat log persistence ──────────────────────────────────────────

    def log_message(self, role: str, content: str, **extra: Any) -> None:
        """Append a chat message to *chat_log.jsonl* in the session dir.

        Args:
            role: ``"user"``, ``"assistant"``, ``"tool_call"``,
                  ``"tool_result"``, ``"sub_agent"``, etc.
            content: The text content.
            **extra: Additional fields (e.g. ``tool_name``, ``agent_name``).
        """
        entry: dict[str, Any] = {
            "ts": _iso_timestamp(),
            "tz": "UTC+08:00",
            "role": role,
            "content": content,
        }
        entry.update(extra)
        with open(self._chat_log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def log_tool_call(
        self,
        tool_name: str,
        args: dict | None = None,
        result: str | None = None,
    ) -> None:
        """Append a tool-call entry to the chat log."""
        entry: dict[str, Any] = {
            "ts": _iso_timestamp(),
            "tz": "UTC+08:00",
            "role": "tool_call",
            "tool_name": tool_name,
        }
        if args is not None:
            # Truncate large arg values for readability
            brief: dict[str, Any] = {}
            for k, v in args.items():
                s = str(v)
                brief[k] = s if len(s) <= 500 else s[:497] + "…"
            entry["args"] = brief
        if result is not None:
            entry["result"] = result if len(result) <= 2000 else result[:1997] + "…"
        with open(self._chat_log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Metadata persistence ───────────────────────────────────────────

    def _write_meta(self) -> None:
        meta = {
            "session_name": self.session_name,
            "created": self.timestamp,
            "name_hint": self.name_hint,
            "base_dir": self.base_dir,
            "chat_log": self._chat_log_path,
            "runs": self._runs,
        }
        meta_path = os.path.join(self.base_dir, "session.json")
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def to_dict(self) -> dict[str, Any]:
        """Return session info as a dict."""
        # List files in designs/ (recurse into design_name subdirs)
        design_files: list[str] = []
        if os.path.isdir(self._designs_dir):
            for root, _dirs, files in os.walk(self._designs_dir):
                for fn in sorted(files):
                    rel = os.path.relpath(os.path.join(root, fn), self._designs_dir)
                    design_files.append(rel)

        return {
            "session_name": self.session_name,
            "base_dir": self.base_dir,
            "designs_dir": self._designs_dir,
            "design_files": design_files,
            "run_dirs": list_run_dirs(self.base_dir),
            "runs": self._runs,
        }

    # ── Rename / update session name ───────────────────────────────────

    def rename(self, new_hint: str) -> None:
        """Rename the session directory with a new hint.

        The timestamp prefix is preserved.
        """
        if not new_hint:
            return
        safe = re.sub(r"[^\w\-]", "_", new_hint)[:40].strip("_")
        new_name = f"{self.timestamp}_{safe}"
        if new_name == self.session_name:
            return
        new_base = os.path.join(self.work_dir, "sessions", new_name)
        if os.path.exists(new_base):
            # Avoid collision
            return
        os.rename(self.base_dir, new_base)
        self.session_name = new_name
        self.name_hint = new_hint
        self.base_dir = new_base
        self._designs_dir = os.path.join(new_base, "designs")
        self._chat_log_path = os.path.join(new_base, "chat_log.jsonl")
        self._write_meta()

    # ── Class-level singleton API ──────────────────────────────────────

    @classmethod
    def start_new(cls, work_dir: str, name_hint: str = "") -> "SessionManager":
        """Create a new session and set it as active for the current context."""
        cls._current = cls(work_dir, name_hint)
        _current_session.set(cls._current)
        return cls._current

    @classmethod
    def load_existing(cls, work_dir: str, session_name: str) -> "SessionManager":
        """Load an existing session directory and set it as active."""
        base_dir = os.path.join(work_dir, "sessions", session_name)
        if not os.path.isdir(base_dir):
            raise FileNotFoundError(f"Session not found: {session_name}")

        meta_path = os.path.join(base_dir, "session.json")
        meta: dict[str, Any] = {}
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)

        session = cls.__new__(cls)
        session.timestamp = meta.get("created", session_name.split("_", 1)[0])
        session.name_hint = meta.get("name_hint", "")
        session.session_name = meta.get("session_name", session_name)
        session.work_dir = work_dir
        session.base_dir = base_dir
        session._designs_dir = os.path.join(base_dir, "designs")
        os.makedirs(session._designs_dir, exist_ok=True)
        session._runs = meta.get("runs", [])
        session._chat_log_path = os.path.join(base_dir, "chat_log.jsonl")

        cls._current = session
        _current_session.set(session)
        return session

    @classmethod
    def get_current(cls) -> "SessionManager | None":
        """Return the active session, or None if not started."""
        return _current_session.get() or cls._current

    @classmethod
    def clear(cls) -> None:
        """Clear the active session (for testing)."""
        cls._current = None
        _current_session.set(None)


# ═══════════════════════════════════════════════════════════════════════
# Helper for run_dir resolution (used by all runners)
# ═══════════════════════════════════════════════════════════════════════

def resolve_run_dir(work_dir: str, run_label: str) -> str:
    """Return the run directory, inside the session if one is active.

    Falls back to ``<work_dir>/<run_label>`` if no session is active
    (backward-compatible behaviour).
    """
    session = SessionManager.get_current()
    if session:
        return session.run_dir(run_label)
    d = os.path.join(work_dir, run_label)
    os.makedirs(d, exist_ok=True)
    return d


def get_session_name() -> str | None:
    """Return the current session name, or None."""
    session = SessionManager.get_current()
    return session.session_name if session else None


def list_run_dirs(base_dir: str | Path) -> list[str]:
    """List top-level directories in a session that look like tool runs."""
    root = Path(base_dir)
    if not root.is_dir():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and path.name not in _NON_RUN_DIRS
        and not path.name.startswith(".")
    )


def run_count_from_meta(meta: dict[str, Any], base_dir: str | Path) -> int:
    """Return a run count that works for both new and legacy sessions."""
    recorded_runs = meta.get("runs", [])
    if recorded_runs:
        return len(recorded_runs)
    return len(list_run_dirs(base_dir))


def register_current_run(
    run_label: str,
    tool_name: str,
    result: dict[str, Any],
) -> None:
    """Record a completed tool run in the active session metadata."""
    session = SessionManager.get_current()
    if not session:
        return

    success = result.get("success")
    metrics = result.get("metrics")
    session.register_run(
        run_label=run_label,
        tool=tool_name,
        success=success if isinstance(success, bool) else None,
        metrics=metrics if isinstance(metrics, dict) else None,
    )


# ═══════════════════════════════════════════════════════════════════════
# LangChain tools — let the LLM manage sessions
# ═══════════════════════════════════════════════════════════════════════

def _get_cfg() -> OpenROADConfig:
    global _cfg_instance
    if _cfg_instance is None:
        _cfg_instance = OpenROADConfig()
    return _cfg_instance


_cfg_instance: OpenROADConfig | None = None


@tool
def start_session(session_name: str = "") -> str:
    """Start a new work session or rename the current one.

    A session groups all design files, synthesis outputs, P&R results,
    logs, and reports under a single timestamped directory.

    If a session is already active, calling this with a *session_name*
    renames it (the timestamp prefix is preserved).

    Args:
        session_name: Descriptive name for the session, e.g.
                      "counter4_nangate45" or "aes_dse_timing".
                      Used as a suffix after the timestamp.

    Returns:
        JSON with session info (base_dir, designs_dir, etc.).
    """
    session = SessionManager.get_current()
    if session:
        # Rename existing session
        if session_name and session_name != session.name_hint:
            session.rename(session_name)
    else:
        cfg = _get_cfg()
        session = SessionManager.start_new(cfg.work_dir, session_name)
    return json.dumps(session.to_dict())


@tool
def get_session_info() -> str:
    """Get information about the current work session.

    Returns the session directory, list of design files, list of runs,
    and metadata.  If no session is active, returns an error.

    Returns:
        JSON with session info.
    """
    session = SessionManager.get_current()
    if not session:
        return json.dumps({"error": "No active session. Use start_session first."})
    return json.dumps(session.to_dict())


@tool
def list_downloadable_artifacts(include_inputs: bool = False) -> str:
    """List files from the current session that can be downloaded by the user.

    Use this when the user asks to download generated results, GDS files,
    reports, logs, or an archive of the current session outputs. The returned
    JSON includes browser-relative URLs that can be shown directly to the user.

    Args:
        include_inputs: Include user input files from designs/ and attachments/.

    Returns:
        JSON with downloadable artifacts and an all-results zip URL.
    """
    session = SessionManager.get_current()
    if not session:
        return json.dumps({"error": "No active session. Use start_session first."})

    artifacts = list_session_artifacts(Path(session.base_dir), include_inputs=include_inputs)
    for artifact in artifacts:
        artifact["download_url"] = (
            f"/api/sessions/{quote(session.session_name)}/download?"
            f"file_path={quote(artifact['path'])}"
        )

    return json.dumps(
        {
            "session_name": session.session_name,
            "artifact_count": len(artifacts),
            "zip_download_url": f"/api/sessions/{quote(session.session_name)}/download.zip",
            "artifacts": artifacts,
        },
        indent=2,
    )


@tool
def list_sessions() -> str:
    """List all past and current work sessions.

    Returns:
        JSON array of session summaries with name, date, and directory.
    """
    session = SessionManager.get_current()
    work_dir = session.work_dir if session else _get_cfg().work_dir
    sessions_root = os.path.join(work_dir, "sessions")
    if not os.path.isdir(sessions_root):
        return json.dumps([])

    result = []
    for name in sorted(os.listdir(sessions_root), reverse=True):
        sp = os.path.join(sessions_root, name)
        if not os.path.isdir(sp):
            continue
        meta_path = os.path.join(sp, "session.json")
        meta: dict[str, Any] = {}
        if os.path.isfile(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass
        # Count runs and design files
        run_count = run_count_from_meta(meta, sp)
        designs_dir = os.path.join(sp, "designs")
        design_count = (
            len(os.listdir(designs_dir))
            if os.path.isdir(designs_dir)
            else 0
        )
        result.append({
            "session_name": name,
            "base_dir": sp,
            "created": meta.get("created", ""),
            "name_hint": meta.get("name_hint", ""),
            "run_count": run_count,
            "design_file_count": design_count,
        })

    return json.dumps(result, indent=2)
