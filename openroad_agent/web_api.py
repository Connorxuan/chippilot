"""FastAPI app for the Chippilot web interface."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from openroad_agent.agents.orchestrator import _extract_subagent_result, set_stream_callback
from openroad_agent.auth import AuthStore, AuthUser, COOKIE_NAME, TOKEN_MAX_AGE_SECONDS
from openroad_agent.checkpoint import get_sqlite_checkpointer
from openroad_agent.config import OpenROADConfig, SUPPORTED_MODELS
from openroad_agent.context_manager import ContextManager
from openroad_agent.tools.artifacts import (
    build_artifact_zip,
    list_session_artifacts,
    resolve_session_file,
)
from openroad_agent.main import create_agent
from openroad_agent.tools.session_manager import SessionManager, list_run_dirs, run_count_from_meta

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
STATIC_VERSION = max(
    int(path.stat().st_mtime)
    for path in STATIC_DIR.glob("*")
    if path.is_file()
)
MAX_ATTACHMENT_BYTES = 240_000
MAX_ATTACHMENT_CONTEXT_CHARS = 360_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip().strip("\"'")
    try:
        return int(raw)
    except ValueError:
        print(f"WARNING: {name} must be an integer, got {raw!r}; using {default}.")
        return default


CHAT_JOB_TIMEOUT_SECONDS = _env_int("CHIPPILOT_CHAT_JOB_TIMEOUT_SECONDS", 600)
CHAT_LONG_JOB_TIMEOUT_SECONDS = _env_int("CHIPPILOT_LONG_JOB_TIMEOUT_SECONDS", 10_800)
CHAT_JOB_QUEUE_TIMEOUT_SECONDS = _env_int("CHIPPILOT_CHAT_QUEUE_TIMEOUT_SECONDS", 120)
CHIP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#0f766e"/>
  <g fill="#f8f6f1">
    <rect x="18" y="18" width="28" height="28" rx="4"/>
    <rect x="23" y="23" width="18" height="18" rx="2" fill="#0b5d57"/>
    <rect x="11" y="20" width="5" height="4" rx="1"/>
    <rect x="11" y="30" width="5" height="4" rx="1"/>
    <rect x="11" y="40" width="5" height="4" rx="1"/>
    <rect x="48" y="20" width="5" height="4" rx="1"/>
    <rect x="48" y="30" width="5" height="4" rx="1"/>
    <rect x="48" y="40" width="5" height="4" rx="1"/>
    <rect x="20" y="11" width="4" height="5" rx="1"/>
    <rect x="30" y="11" width="4" height="5" rx="1"/>
    <rect x="40" y="11" width="4" height="5" rx="1"/>
    <rect x="20" y="48" width="4" height="5" rx="1"/>
    <rect x="30" y="48" width="4" height="5" rx="1"/>
    <rect x="40" y="48" width="4" height="5" rx="1"/>
  </g>
</svg>"""


class NoCacheStaticFiles(StaticFiles):
    """Static files that always disable client caching."""

    async def get_response(self, path: str, scope):
        request_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_headers.pop("if-none-match", None)
        request_headers.pop("if-modified-since", None)
        scope = {
            **scope,
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in request_headers.items()
            ],
        }
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        if "etag" in response.headers:
            del response.headers["etag"]
        if "last-modified" in response.headers:
            del response.headers["last-modified"]
        return response

STARTER_PROMPTS = [
    {
        "title": "Run a Reference Flow",
        "prompt": "Run the full RTL-to-GDS flow for gcd on nangate45 and summarize timing, area, and utilization.",
    },
    {
        "title": "Explore Density",
        "prompt": "Explore placement density from 0.2 to 0.6 for gcd on nangate45 and identify the best trade-off.",
    },
    {
        "title": "Debug a Failure",
        "prompt": "Read the latest synthesis or placement logs in the current session and explain the root cause of the failure.",
    },
    {
        "title": "Plan an Optimization",
        "prompt": "Suggest the next three optimization moves for improving timing on aes for sky130hd.",
    },
]


class CreateSessionRequest(BaseModel):
    name_hint: str = ""


class ChatRequest(BaseModel):
    message: str = ""
    session_name: str | None = None
    attachments: list[str] = Field(default_factory=list)


class DeleteFileRequest(BaseModel):
    session_name: str
    file_path: str


class AuthRequest(BaseModel):
    username: str
    password: str


class AppState:
    def __init__(self) -> None:
        self.config = OpenROADConfig()
        self.auth_store = AuthStore(self.config.work_dir)
        self.chat_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.agent_cache: dict[tuple[str, str], Any] = {}
        self.thread_ids: dict[tuple[str, str], str] = {}
        self.job_queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self.context_manager = ContextManager(model_name=self.config.model_name)
        self.current_model: str = self.config.model_name
        self.model_lock = asyncio.Lock()


state = AppState()


def _auth_store() -> AuthStore:
    if state.auth_store.work_dir != Path(state.config.work_dir):
        state.auth_store = AuthStore(state.config.work_dir)
    return state.auth_store


def _store():
    return _auth_store().store


def _normalize_content(content: Any) -> str:
    if isinstance(content, list):
        parts = [
            part["text"] if isinstance(part, dict) and "text" in part else str(part)
            for part in content
        ]
        return "\n".join(parts)
    return str(content or "")


def _brief_args(args: dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    return joined[:200] + "..." if len(joined) > 200 else joined


def _tool_activity_key(raw: dict[str, Any]) -> tuple[str, str]:
    return (str(raw.get("agent_name") or ""), str(raw.get("tool_name") or ""))


def _parse_event_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _elapsed_from_result_content(content: str) -> float | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    elapsed = data.get("elapsed_s")
    if not isinstance(elapsed, int | float):
        return None
    elapsed = float(elapsed)
    return elapsed if elapsed >= 0 else None


def _tool_elapsed_seconds(call_ts: Any, result_ts: Any, result_content: str) -> float | None:
    elapsed = _elapsed_from_result_content(result_content)
    if elapsed is not None:
        return round(elapsed, 3)

    start = _parse_event_time(call_ts)
    end = _parse_event_time(result_ts)
    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        return None
    elapsed = (end - start).total_seconds()
    return round(elapsed, 3) if elapsed >= 0 else None


def _format_duration_seconds(value: float) -> str:
    if value < 1:
        return "<1s"
    if value < 10:
        return f"{value:.1f}s"
    if value < 60:
        return f"{round(value)}s"
    minutes, seconds = divmod(round(value), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _markdown_escape(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"


def _format_markdown_section(title: str, body: str = "") -> str:
    return f"## {title}\n\n{body.strip()}\n" if body.strip() else f"## {title}\n"


def _sanitize_hint(raw: str) -> str:
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if not collapsed:
        return f"chat_{uuid.uuid4().hex[:8]}"
    words = collapsed.split(" ")
    return " ".join(words[:6])[:48]


def _session_timestamp_prefix(session_name: str) -> str:
    match = re.match(r"^\d{8}_\d{6}", session_name)
    return match.group(0) if match else ""


def _user_work_dir(user: AuthUser) -> Path:
    return _auth_store().user_root(user)


def _sessions_root(user: AuthUser) -> Path:
    root = _user_work_dir(user) / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _require_user(request: Request) -> AuthUser:
    store = _auth_store()
    store.ensure_initialized()
    user = store.verify_user_token(request.cookies.get(COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    store.user_root(user)
    return user


def _chat_lock_for(user: AuthUser, session_name: str) -> asyncio.Lock:
    key = (user.user_id, session_name)
    lock = state.chat_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        state.chat_locks[key] = lock
    return lock


def _resolve_session_name(session_name: str, user: AuthUser) -> str:
    if _store().get_session(user.user_id, session_name):
        return session_name

    sessions_root = _sessions_root(user)
    prefix = _session_timestamp_prefix(session_name)
    current = SessionManager.get_current()
    if (
        prefix
        and current
        and Path(current.work_dir) == _user_work_dir(user)
        and _session_timestamp_prefix(current.session_name) == prefix
    ):
        current_path = sessions_root / current.session_name
        if current_path.is_dir():
            return current.session_name

    if prefix:
        matches = sorted(
            name
            for name in _store().list_sessions(user.user_id)
            if _session_timestamp_prefix(name) == prefix
        )
        if len(matches) == 1:
            return matches[0]

    return session_name


def _session_path(session_name: str, user: AuthUser) -> Path:
    return _sessions_root(user) / _resolve_session_name(session_name, user)


def _safe_filename(name: str) -> str:
    base = Path(name or "upload").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return cleaned or "upload"


def _session_meta(session_name: str, user: AuthUser) -> dict[str, Any]:
    meta_path = _session_path(session_name, user) / "session.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_name}")
    with meta_path.open() as f:
        return json.load(f)


def _count_design_files(designs_dir: Path) -> int:
    if not designs_dir.exists():
        return 0
    return sum(1 for path in designs_dir.rglob("*") if path.is_file())


def _attachments_dir(session: SessionManager) -> Path:
    path = Path(session.base_dir) / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_title(session_name: str, meta: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    if meta.get("name_hint"):
        return meta["name_hint"]
    for message in messages:
        if message["role"] == "user" and message["content"]:
            return _sanitize_hint(message["content"])
    suffix = re.sub(r"^\d{8}_\d{6}_?", "", session_name)
    return suffix or session_name


def _read_chat_messages(session_name: str, user: AuthUser) -> list[dict[str, Any]]:
    chat_log_path = _session_path(session_name, user) / "chat_log.jsonl"
    if not chat_log_path.exists():
        return []

    messages: list[dict[str, Any]] = []
    pending_tool_messages: dict[tuple[str, str], dict[str, Any]] = {}
    with chat_log_path.open() as f:
        for index, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            if raw.get("tool_name") == "upload_files":
                continue

            role = raw.get("role", "unknown")
            content = str(raw.get("content", ""))
            label = role.replace("_", " ").title()
            kind = "message"

            if role == "tool_call":
                tool_name = raw.get("tool_name", "tool")
                content = f"{tool_name}({_brief_args(raw.get('args', {}))})"
                label = f"{raw.get('agent_name')}: {tool_name}" if raw.get("agent_name") else tool_name
                kind = "activity"
                message = {
                    "id": f"{session_name}-{index}",
                    "ts": raw.get("ts", ""),
                    "role": role,
                    "kind": kind,
                    "label": label,
                    "content": content,
                    "tool_name": raw.get("tool_name"),
                    "agent_name": raw.get("agent_name"),
                    "attachments": raw.get("attachments", []) or [],
                    "agent_context": raw.get("agent_context"),
                    "result_content": "",
                    "result_ts": "",
                    "result_elapsed_s": None,
                }
                messages.append(message)
                pending_tool_messages[_tool_activity_key(raw)] = message
                continue
            elif role in {"tool_result", "sub_agent_tool_result"}:
                key = _tool_activity_key(raw)
                pending = pending_tool_messages.get(key)
                if pending:
                    pending["result_content"] = content
                    pending["result_ts"] = raw.get("ts", "")
                    pending["result_elapsed_s"] = _tool_elapsed_seconds(
                        pending.get("ts"),
                        raw.get("ts", ""),
                        content,
                    )
                    pending_tool_messages.pop(key, None)
                    continue
                label = raw.get("tool_name", "Tool Result")
                if raw.get("agent_name"):
                    label = f"{raw.get('agent_name')}: {label}"
                kind = "activity"
            elif role == "sub_agent":
                label = raw.get("agent_name", "Sub-agent")
                kind = "activity"
            elif role == "error":
                label = "Error"
                kind = "error"
            elif role not in {"user", "assistant"}:
                kind = "activity"

            messages.append(
                {
                    "id": f"{session_name}-{index}",
                    "ts": raw.get("ts", ""),
                    "role": role,
                    "kind": kind,
                    "label": label,
                    "content": content,
                    "tool_name": raw.get("tool_name"),
                    "agent_name": raw.get("agent_name"),
                    "attachments": raw.get("attachments", []) or [],
                    "agent_context": raw.get("agent_context"),
                }
            )

    return messages


def _read_raw_chat_log(session_name: str, user: AuthUser) -> list[dict[str, Any]]:
    chat_log_path = _session_path(session_name, user) / "chat_log.jsonl"
    if not chat_log_path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with chat_log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _export_chat_markdown(session_name: str, user: AuthUser) -> str:
    resolved_name = _resolve_session_name(session_name, user)
    meta = _session_meta(resolved_name, user)
    messages = _read_chat_messages(resolved_name, user)
    raw_rows = _read_raw_chat_log(resolved_name, user)
    title = _session_title(resolved_name, meta, messages)

    lines = [
        "# Chippilot Conversation Export",
        "",
        f"- Session: `{resolved_name}`",
        f"- Title: {title}",
        f"- User: {user.username}",
        f"- Exported: {datetime.utcnow().isoformat()}Z",
        "",
    ]

    for message in messages:
        ts = message.get("ts") or "unknown time"
        role = message.get("role", "message")
        if message.get("kind") == "activity":
            label = message.get("label") or message.get("tool_name") or "Agent activity"
            body_parts = []
            if message.get("content"):
                body_parts.append("**Tool call**\n\n```text\n" + _markdown_escape(message["content"]) + "\n```")
            if message.get("result_content"):
                result_label = "**Tool result**"
                elapsed = message.get("result_elapsed_s")
                if isinstance(elapsed, int | float):
                    result_label = f"**Tool result · {_format_duration_seconds(float(elapsed))}**"
                body_parts.append(result_label + "\n\n```text\n" + _markdown_escape(message["result_content"]) + "\n```")
            lines.append(_format_markdown_section(f"Activity · {label} · {ts}", "\n\n".join(body_parts)))
            continue

        heading = {
            "user": "User",
            "assistant": "Assistant",
            "error": "Error",
        }.get(role, role.replace("_", " ").title())
        body = _markdown_escape(message.get("content", ""))
        attachments = message.get("attachments") or []
        if attachments:
            body = f"{body}\n\n**Attachments**\n" + "\n".join(f"- `{item}`" for item in attachments)
        lines.append(_format_markdown_section(f"{heading} · {ts}", body))

    lines.append("## Raw Metadata")
    lines.append("")
    lines.append(_json_block({"session": meta, "raw_event_count": len(raw_rows)}))
    lines.append("")
    return "\n".join(lines)


def _build_session_summary(session_name: str, user: AuthUser) -> dict[str, Any]:
    session_name = _resolve_session_name(session_name, user)
    meta = _session_meta(session_name, user)
    messages = _read_chat_messages(session_name, user)
    base_dir = _session_path(session_name, user)
    designs_dir = base_dir / "designs"
    artifacts = list_session_artifacts(base_dir)
    preview = ""
    last_message_at = meta.get("created", "")
    for message in reversed(messages):
        if message["role"] in {"assistant", "user"} and message["content"].strip():
            preview = message["content"].strip().replace("\n", " ")
            last_message_at = message["ts"] or last_message_at
            break

    return {
        "session_name": session_name,
        "title": _session_title(session_name, meta, messages),
        "name_hint": meta.get("name_hint", ""),
        "created": meta.get("created", ""),
        "last_message_at": last_message_at,
        "preview": preview[:140],
        "run_count": run_count_from_meta(meta, base_dir),
        "design_file_count": _count_design_files(designs_dir),
        "artifact_count": len(artifacts),
    }


def _build_session_detail(session_name: str, user: AuthUser) -> dict[str, Any]:
    session_name = _resolve_session_name(session_name, user)
    meta = _session_meta(session_name, user)
    base_dir = _session_path(session_name, user)
    messages = _read_chat_messages(session_name, user)
    designs_dir = base_dir / "designs"

    design_files = []
    if designs_dir.exists():
        for path in sorted(designs_dir.rglob("*")):
            if path.is_file():
                design_files.append(str(path.relative_to(designs_dir)))

    run_dirs = list_run_dirs(base_dir)

    summary = _build_session_summary(session_name, user)
    summary.update(
        {
            "base_dir": str(base_dir),
            "messages": messages,
            "design_files": design_files,
            "run_dirs": run_dirs,
            "runs": meta.get("runs", []),
            "artifacts": list_session_artifacts(base_dir),
        }
    )
    return summary


def _write_uploaded_file(target_dir: Path, upload: UploadFile) -> str:
    filename = _safe_filename(upload.filename or "upload")
    target = target_dir / filename

    stem = target.stem
    suffix = target.suffix
    counter = 1
    while target.exists():
        target = target_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    upload.file.seek(0)
    with target.open("wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    return target.name


def _resolve_file_in_root(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {relative_path}")
    return candidate


def _resolve_design_file(session: SessionManager, relative_path: str) -> Path:
    return _resolve_file_in_root(Path(session.designs_dir), relative_path)


def _resolve_attachment_file(session: SessionManager, relative_path: str) -> Path:
    return _resolve_file_in_root(_attachments_dir(session), relative_path)


def _read_attachment_context(session: SessionManager, attachments: list[str]) -> str:
    if not attachments:
        return ""

    sections = []
    total_chars = 0
    for relative_path in attachments:
        try:
            path = _resolve_attachment_file(session, relative_path)
        except HTTPException:
            # Backward compatibility for files uploaded before attachments were staged.
            path = _resolve_design_file(session, relative_path)

        data = path.read_bytes()
        truncated = len(data) > MAX_ATTACHMENT_BYTES
        if truncated:
            data = data[:MAX_ATTACHMENT_BYTES]

        text = data.decode("utf-8", errors="replace")
        if total_chars + len(text) > MAX_ATTACHMENT_CONTEXT_CHARS:
            remaining = MAX_ATTACHMENT_CONTEXT_CHARS - total_chars
            if remaining <= 0:
                sections.append(
                    f"<attached_file name={json.dumps(relative_path)} omitted=\"context_limit\" />"
                )
                continue
            text = text[:remaining]
            truncated = True

        total_chars += len(text)
        truncated_note = " truncated=\"true\"" if truncated else ""
        sections.append(
            f"<attached_file name={json.dumps(relative_path)}{truncated_note}>\n"
            f"{text}\n"
            f"</attached_file>"
        )

    return "\n\n".join(sections)


def _message_with_attachments(message: str, attachments: list[str], session: SessionManager) -> str:
    attachment_context = _read_attachment_context(session, attachments)
    if not attachment_context:
        return message

    user_text = message or "Please process the attached file(s)."
    return (
        f"{user_text}\n\n"
        "The user attached the following file content. Treat these attachments as part of "
        "the user's message. If they are design inputs needed for the task, first save them "
        "to the current session workspace with the save_design_files tool before running "
        "synthesis, OpenROAD, or other flow tools.\n\n"
        f"{attachment_context}"
    )


def _conversation_history(session_name: str, user: AuthUser) -> list[HumanMessage | AIMessage]:
    history: list[HumanMessage | AIMessage] = []
    for message in _read_chat_messages(session_name, user):
        if message["role"] == "user":
            content = message.get("agent_context") or message["content"]
            if content:
                history.append(HumanMessage(content=content))
        elif message["role"] == "assistant" and message["content"]:
            history.append(AIMessage(content=message["content"]))
    return history[-16:]


def _list_session_names(user: AuthUser) -> list[str]:
    return _store().list_sessions(user.user_id)


def _delete_session_dir(session_name: str, user: AuthUser) -> None:
    resolved_name = _resolve_session_name(session_name, user)
    sessions_root = _sessions_root(user).resolve()
    target = (sessions_root / resolved_name).resolve()

    if sessions_root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid session path.")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_name}")

    current = SessionManager.get_current()
    if current and current.session_name == resolved_name and Path(current.work_dir) == _user_work_dir(user):
        SessionManager.clear()

    cache_key = (user.user_id, resolved_name)
    thread_id = state.thread_ids.get(cache_key) or _store().get_thread_id(user.user_id, resolved_name)
    state.agent_cache.pop(cache_key, None)
    state.thread_ids.pop(cache_key, None)
    if thread_id:
        get_sqlite_checkpointer(state.config.work_dir).delete_thread(thread_id)
    _store().mark_session_deleted(user.user_id, resolved_name)
    shutil.rmtree(target)


def _activate_session(user: AuthUser, session_name: str | None, name_hint: str = "") -> SessionManager:
    work_dir = str(_user_work_dir(user))
    if session_name:
        session_name = _resolve_session_name(session_name, user)
        current = SessionManager.get_current()
        if current and current.session_name == session_name and Path(current.work_dir) == Path(work_dir):
            return current
        session = SessionManager.load_existing(work_dir, session_name)
        _store().upsert_session(
            user.user_id,
            session.session_name,
            session.base_dir,
            name_hint=session.name_hint,
            created_at=session.timestamp,
        )
        return session
    session = SessionManager.start_new(work_dir, name_hint=name_hint)
    _store().upsert_session(
        user.user_id,
        session.session_name,
        session.base_dir,
        name_hint=session.name_hint,
        created_at=session.timestamp,
    )
    return session


def _agent_for_session(user: AuthUser, session_name: str) -> tuple[Any, str, bool]:
    cache_key = (user.user_id, session_name)
    agent = state.agent_cache.get(cache_key)
    created = False
    if agent is None:
        agent = create_agent(model=state.current_model, config=state.config)
        state.agent_cache[cache_key] = agent
        state.thread_ids[cache_key] = _store().get_or_create_thread_id(
            user.user_id,
            session_name,
            str(uuid.uuid4()),
        )
        created = True
    elif cache_key not in state.thread_ids:
        state.thread_ids[cache_key] = _store().get_or_create_thread_id(
            user.user_id,
            session_name,
            str(uuid.uuid4()),
        )
    return agent, state.thread_ids[cache_key], created


def _sync_agent_session_key(user: AuthUser, previous_name: str, current_name: str) -> None:
    if previous_name == current_name:
        return
    previous_key = (user.user_id, previous_name)
    current_key = (user.user_id, current_name)
    if previous_key in state.agent_cache and current_key not in state.agent_cache:
        state.agent_cache[current_key] = state.agent_cache.pop(previous_key)
    if previous_key in state.thread_ids and current_key not in state.thread_ids:
        state.thread_ids[current_key] = state.thread_ids.pop(previous_key)
    _store().move_thread(user.user_id, previous_name, current_name)
    _store().rename_session(user.user_id, previous_name, current_name, _session_path(current_name, user))


def _log_stream_event(session: SessionManager, msg: Any) -> None:
    content = _normalize_content(msg.content)

    if msg.type == "ai":
        for tool_call in getattr(msg, "tool_calls", []):
            session.log_tool_call(tool_call["name"], tool_call.get("args", {}))
    elif msg.type == "tool":
        trimmed = content if len(content) <= 2000 else content[:1997] + "..."
        session.log_message(
            "tool_result",
            trimmed,
            tool_name=getattr(msg, "name", ""),
        )


def _log_subagent_stream_event(session: SessionManager, event_type: str, agent_name: str, msg: Any) -> None:
    if event_type == "start":
        session.log_message(
            "sub_agent",
            f"Sub-agent {agent_name} started.",
            agent_name=agent_name,
            event_type="start",
        )
        return

    if event_type == "end":
        session.log_message(
            "sub_agent",
            f"Sub-agent {agent_name} finished.",
            agent_name=agent_name,
            event_type="end",
        )
        return

    if event_type != "msg" or msg is None:
        return

    content = _normalize_content(msg.content)
    if msg.type == "ai":
        if content.strip():
            trimmed = content if len(content) <= 2000 else content[:1997] + "..."
            session.log_message("sub_agent", trimmed, agent_name=agent_name)
        for tool_call in getattr(msg, "tool_calls", []):
            session.log_message(
                "tool_call",
                "",
                tool_name=tool_call["name"],
                args=tool_call.get("args", {}),
                agent_name=agent_name,
            )
    elif msg.type == "tool":
        trimmed = content if len(content) <= 2000 else content[:1997] + "..."
        session.log_message(
            "sub_agent_tool_result",
            trimmed,
            agent_name=agent_name,
            tool_name=getattr(msg, "name", ""),
        )


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "session_name": job["session_name"],
        "job_type": job.get("job_type", "chat"),
        "timeout_seconds": job.get("timeout_seconds", CHAT_JOB_TIMEOUT_SECONDS),
        "status": job["status"],
        "message": job.get("message", ""),
        "error": job.get("error", ""),
        "result": job.get("result", {}),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
    }


def _classify_chat_job(message: str, attachments: list[str]) -> tuple[str, int]:
    text = message.lower()
    long_patterns = [
        "dse",
        "explore",
        "exploration",
        "sweep",
        "parameter",
        "full flow",
        "rtl-to-gds",
        "openroad",
        "yosys",
        "klayout",
        "run ",
        "execute",
        "synthesis",
        "synthesize",
        "placement",
        "routing",
        "floorplan",
        "cts",
        "gds",
        "参数",
        "探索",
        "扫描",
        "扫一遍",
        "多组",
        "完整流程",
        "全流程",
        "运行",
        "执行",
        "综合",
        "布局",
        "布线",
        "物理设计",
        "生成gds",
    ]
    if attachments or any(pattern in text for pattern in long_patterns):
        return "long_running", CHAT_LONG_JOB_TIMEOUT_SECONDS
    return "chat", CHAT_JOB_TIMEOUT_SECONDS


def _emit_job_event(job_id: str, event: dict[str, Any]) -> None:
    queues = state.job_queues.get(job_id, [])
    for queue in list(queues):
        queue.put_nowait(event)


def _job_event_payload(event_type: str, job: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "job": _public_job(job),
        "session_name": job["session_name"],
        **extra,
    }


def _fail_chat_job(
    job_id: str,
    user: AuthUser,
    session_name: str,
    message: str,
    error: str,
) -> None:
    _store().update_job(
        job_id,
        "failed",
        message=message,
        error=error,
        result={"session_name": session_name},
    )
    failed_job = _store().get_job(user.user_id, job_id)
    if failed_job:
        _emit_job_event(job_id, _job_event_payload("failed", failed_job))


async def _run_chat_job_locked(
    job_id: str,
    user: AuthUser,
    session_name: str,
    initial_session_name: str,
    input_messages: list[HumanMessage | AIMessage],
    agent: Any,
    thread_id: str,
) -> None:
    job = _store().get_job(user.user_id, job_id)
    if not job:
        return

    collected_messages = list(input_messages)
    session = _activate_session(user, session_name)

    def emit_session_update(event_type: str = "session") -> None:
        current_job = _store().get_job(user.user_id, job_id) or job
        _emit_job_event(
            job_id,
            _job_event_payload(
                event_type,
                current_job,
                session_name=session.session_name,
            ),
        )

    def on_subagent_event(event_type: str, agent_name: str, msg: Any) -> None:
        _log_subagent_stream_event(session, event_type, agent_name, msg)
        _sync_agent_session_key(user, initial_session_name, session.session_name)
        emit_session_update("session")

    # ── Context compaction: prevent unbounded growth in long sessions ──
    checkpointer = get_sqlite_checkpointer(state.config.work_dir)
    cfg_tuple = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    if cfg_tuple:
        hist = cfg_tuple.checkpoint.get("channel_values", {}).get("messages", [])
        if state.context_manager.should_compact(hist):
            compacted = state.context_manager.compact(hist)
            checkpointer.compact_thread(thread_id, compacted)

    set_stream_callback(on_subagent_event)
    try:
        async for event in agent.astream(
            {"messages": input_messages},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode="updates",
        ):
            for node_output in event.values():
                for msg in node_output.get("messages", []):
                    collected_messages.append(msg)
                    _log_stream_event(session, msg)
                    _sync_agent_session_key(user, initial_session_name, session.session_name)
                    _store().touch_session(user.user_id, session.session_name)
                    emit_session_update("session")
    finally:
        set_stream_callback(None)

    assistant_reply = _extract_subagent_result({"messages": collected_messages})
    session.log_message("assistant", assistant_reply)
    _store().touch_session(user.user_id, session.session_name)
    _sync_agent_session_key(user, initial_session_name, session.session_name)
    _store().update_job(
        job_id,
        "completed",
        message="Agent completed.",
        result={
            "session_name": session.session_name,
            "assistant_reply": assistant_reply,
        },
    )
    completed_job = _store().get_job(user.user_id, job_id) or job
    _emit_job_event(job_id, _job_event_payload("completed", completed_job))


async def _run_chat_job(
    job_id: str,
    user: AuthUser,
    session_name: str,
    initial_session_name: str,
    input_messages: list[HumanMessage | AIMessage],
    agent: Any,
    thread_id: str,
) -> None:
    job = _store().get_job(user.user_id, job_id)
    if not job:
        return

    lock = _chat_lock_for(user, session_name)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=CHAT_JOB_QUEUE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _fail_chat_job(
            job_id,
            user,
            session_name,
            "Agent job timed out in queue.",
            (
                "Timed out waiting for this session to become available "
                f"after {CHAT_JOB_QUEUE_TIMEOUT_SECONDS}s."
            ),
        )
        return

    try:
        _store().update_job(job_id, "running", message="Agent is working.")
        job = _store().get_job(user.user_id, job_id) or job
        _emit_job_event(job_id, _job_event_payload("status", job))
        timeout_seconds = int(job.get("timeout_seconds") or CHAT_JOB_TIMEOUT_SECONDS)
        try:
            await asyncio.wait_for(
                _run_chat_job_locked(
                    job_id,
                    user,
                    session_name,
                    initial_session_name,
                    input_messages,
                    agent,
                    thread_id,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            current = SessionManager.get_current()
            current_name = (
                current.session_name
                if current and Path(current.work_dir) == _user_work_dir(user)
                else session_name
            )
            if current and Path(current.work_dir) == _user_work_dir(user):
                current.log_message(
                    "error",
                    f"Agent job timed out after {timeout_seconds}s.",
                )
                _store().touch_session(user.user_id, current.session_name)
            _fail_chat_job(
                job_id,
                user,
                current_name,
                "Agent job timed out.",
                f"Agent did not finish within {timeout_seconds}s.",
            )
        except Exception as exc:
            current = SessionManager.get_current()
            current_name = (
                current.session_name
                if current and Path(current.work_dir) == _user_work_dir(user)
                else session_name
            )
            if current and Path(current.work_dir) == _user_work_dir(user):
                current.log_message("error", str(exc))
                _store().touch_session(user.user_id, current.session_name)
                _sync_agent_session_key(user, initial_session_name, current.session_name)
            _fail_chat_job(
                job_id,
                user,
                current_name,
                "Agent failed.",
                f"{type(exc).__name__}: {exc}",
            )
    finally:
        lock.release()


def create_app() -> FastAPI:
    app = FastAPI(title="Chippilot Web UI")
    app.mount(
        "/static",
        NoCacheStaticFiles(directory=str(STATIC_DIR), html=False, follow_symlink=False),
        name="static",
    )

    @app.on_event("startup")
    async def startup_recover_jobs() -> None:
        _auth_store().ensure_initialized()

    @app.post("/api/auth/register")
    async def register(payload: AuthRequest, response: Response) -> dict[str, Any]:
        store = _auth_store()
        store.ensure_initialized()
        try:
            user = store.register(payload.username, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response.set_cookie(
            COOKIE_NAME,
            store.sign_user_token(user),
            max_age=TOKEN_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return {"user": user.__dict__}

    @app.post("/api/auth/login")
    async def login(payload: AuthRequest, response: Response) -> dict[str, Any]:
        store = _auth_store()
        store.ensure_initialized()
        user = store.authenticate(payload.username, payload.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        response.set_cookie(
            COOKIE_NAME,
            store.sign_user_token(user),
            max_age=TOKEN_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return {"user": user.__dict__}

    @app.post("/api/auth/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(user: AuthUser = Depends(_require_user)) -> dict[str, Any]:
        return {"user": user.__dict__}

    @app.get("/api/bootstrap")
    async def bootstrap(user: AuthUser = Depends(_require_user)) -> dict[str, Any]:
        sessions = [_build_session_summary(name, user) for name in _list_session_names(user)]
        return {
            "app_name": "Chippilot",
            "model_name": state.current_model,
            "supported_models": SUPPORTED_MODELS,
            "user": user.__dict__,
            "sessions": sessions,
            "starter_prompts": STARTER_PROMPTS,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

    @app.get("/api/config")
    async def get_config(user: AuthUser = Depends(_require_user)) -> dict[str, Any]:
        return {
            "current_model": state.current_model,
            "supported_models": SUPPORTED_MODELS,
            "execution_mode": state.config.execution_mode,
        }

    class SwitchModelRequest(BaseModel):
        model: str

    @app.post("/api/config/model")
    async def switch_model(
        payload: SwitchModelRequest,
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        new_model = payload.model.strip()
        if not new_model:
            raise HTTPException(status_code=400, detail="Model name is required.")
        if new_model not in SUPPORTED_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model: {new_model}. Supported: {', '.join(SUPPORTED_MODELS)}",
            )
        async with state.model_lock:
            if state.current_model != new_model:
                state.current_model = new_model
                state.config.model_name = new_model
                state.context_manager = ContextManager(model_name=new_model)
                # Invalidate agent cache so subsequent chats use the new model
                state.agent_cache.clear()
                state.thread_ids.clear()
        return {"current_model": state.current_model}

    @app.get("/api/sessions")
    async def list_sessions(user: AuthUser = Depends(_require_user)) -> dict[str, Any]:
        return {"sessions": [_build_session_summary(name, user) for name in _list_session_names(user)]}

    @app.post("/api/sessions")
    async def create_session(
        payload: CreateSessionRequest,
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        session = _activate_session(user, None, payload.name_hint.strip())
        return _build_session_detail(session.session_name, user)

    @app.get("/api/sessions/{session_name}")
    async def get_session(
        session_name: str,
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        return _build_session_detail(session_name, user)

    @app.get("/api/sessions/{session_name}/artifacts")
    async def get_session_artifacts(
        session_name: str,
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        resolved_name = _resolve_session_name(session_name, user)
        base_dir = _session_path(resolved_name, user)
        _session_meta(resolved_name, user)
        return {
            "session_name": resolved_name,
            "artifacts": list_session_artifacts(base_dir),
        }

    @app.get("/api/sessions/{session_name}/download")
    async def download_session_file(
        session_name: str,
        file_path: str,
        user: AuthUser = Depends(_require_user),
    ) -> FileResponse:
        resolved_name = _resolve_session_name(session_name, user)
        base_dir = _session_path(resolved_name, user)
        _session_meta(resolved_name, user)
        try:
            target = resolve_session_file(base_dir, file_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}") from exc

        allowed_paths = {
            artifact["path"] for artifact in list_session_artifacts(base_dir, include_inputs=True)
        }
        if file_path not in allowed_paths:
            raise HTTPException(status_code=400, detail="This file is not downloadable.")

        return FileResponse(
            path=target,
            filename=target.name,
            media_type="application/octet-stream",
        )

    @app.get("/api/sessions/{session_name}/download.zip")
    async def download_session_results(
        session_name: str,
        include_inputs: bool = False,
        user: AuthUser = Depends(_require_user),
    ) -> StreamingResponse:
        resolved_name = _resolve_session_name(session_name, user)
        base_dir = _session_path(resolved_name, user)
        _session_meta(resolved_name, user)
        filename, payload, file_count = build_artifact_zip(
            base_dir,
            resolved_name,
            include_inputs=include_inputs,
        )
        if file_count == 0:
            raise HTTPException(status_code=404, detail="No generated results are available to download yet.")

        return StreamingResponse(
            iter([payload]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/sessions/{session_name}/export.md")
    async def export_session_markdown(
        session_name: str,
        user: AuthUser = Depends(_require_user),
    ) -> Response:
        resolved_name = _resolve_session_name(session_name, user)
        _session_meta(resolved_name, user)
        markdown = _export_chat_markdown(resolved_name, user)
        filename = f"chippilot_{resolved_name}_conversation.md"
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.delete("/api/sessions/{session_name}")
    async def delete_session(
        session_name: str,
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        _delete_session_dir(session_name, user)
        return {"sessions": [_build_session_summary(name, user) for name in _list_session_names(user)]}

    @app.post("/api/files")
    async def upload_files(
        session_name: str | None = Form(default=None),
        files: list[UploadFile] = File(...),
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        valid_files = [upload for upload in files if upload.filename]
        if not valid_files:
            raise HTTPException(status_code=400, detail="No files were uploaded.")

        session = _activate_session(user, session_name, "uploaded_designs")
        attachments_dir = _attachments_dir(session)

        saved_files = []
        try:
            for upload in valid_files:
                saved_name = _write_uploaded_file(attachments_dir, upload)
                saved_files.append(saved_name)
        finally:
            for upload in valid_files:
                await upload.close()

        return {
            "uploaded_files": saved_files,
            "session": _build_session_detail(session.session_name, user),
        }

    @app.post("/api/files/delete")
    async def delete_file(
        payload: DeleteFileRequest,
        user: AuthUser = Depends(_require_user),
    ) -> dict[str, Any]:
        session = _activate_session(user, payload.session_name)
        try:
            target = _resolve_attachment_file(session, payload.file_path)
        except HTTPException:
            target = _resolve_design_file(session, payload.file_path)
        target.unlink()
        return {"session": _build_session_detail(session.session_name, user)}

    @app.post("/api/chat")
    async def chat(payload: ChatRequest, user: AuthUser = Depends(_require_user)) -> dict[str, Any]:
        message = payload.message.strip()
        attachments = [item for item in payload.attachments if item]
        if not message and not attachments:
            raise HTTPException(status_code=400, detail="Message cannot be empty.")

        session = _activate_session(user, payload.session_name, _sanitize_hint(message))
        initial_session_name = session.session_name
        model_message = _message_with_attachments(message, attachments, session)
        history = _conversation_history(session.session_name, user)
        agent, thread_id, created = _agent_for_session(user, session.session_name)
        session.log_message(
            "user",
            message,
            attachments=attachments,
            agent_context=model_message if model_message != message else None,
        )
        _store().touch_session(user.user_id, session.session_name)

        new_message = HumanMessage(content=model_message)
        input_messages = [*history, new_message] if created else [new_message]
        job_id = str(uuid.uuid4())
        job_type, timeout_seconds = _classify_chat_job(message, attachments)
        job = _store().create_job(
            job_id,
            user.user_id,
            session.session_name,
            message=(
                "Long-running agent job queued."
                if job_type == "long_running"
                else "Agent queued."
            ),
            job_type=job_type,
            timeout_seconds=timeout_seconds,
        )

        asyncio.create_task(
            _run_chat_job(
                job_id,
                user,
                session.session_name,
                initial_session_name,
                input_messages,
                agent,
                thread_id,
            )
        )

        return {
            "job": _public_job(job),
            "session": _build_session_detail(session.session_name, user),
            "assistant_reply": "",
        }

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, user: AuthUser = Depends(_require_user)) -> dict[str, Any]:
        job = _store().get_job(user.user_id, job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        return {"job": _public_job(job)}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, user: AuthUser = Depends(_require_user)) -> StreamingResponse:
        job = _store().get_job(user.user_id, job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")

        async def stream():
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            state.job_queues.setdefault(job_id, []).append(queue)
            try:
                initial = _store().get_job(user.user_id, job_id)
                if initial:
                    yield f"data: {json.dumps(_job_event_payload('status', initial), ensure_ascii=False)}\n\n"
                    if initial["status"] in {"completed", "failed", "cancelled"}:
                        return
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    event_job = event.get("job", {})
                    if event_job.get("status") in {"completed", "failed", "cancelled"}:
                        return
            finally:
                queues = state.job_queues.get(job_id, [])
                if queue in queues:
                    queues.remove(queue)
                if not queues:
                    state.job_queues.pop(job_id, None)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/")
    async def index() -> HTMLResponse:
        html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Chippilot</title>
    <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
    <link rel="stylesheet" href="/static/styles.css?v={STATIC_VERSION}" />
  </head>
  <body>
    <div id="root"></div>
    <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
    <script type="text/babel" data-presets="react" src="/static/app.jsx?v={STATIC_VERSION}"></script>
  </body>
</html>
"""
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/favicon.svg")
    async def favicon_svg() -> Response:
        return Response(
            content=CHIP_ICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(
            content=CHIP_ICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    def clean_env_value(name: str, default: str) -> str:
        return os.environ.get(name, default).strip().strip("\"'")

    host = clean_env_value("CHIPPILOT_WEB_HOST", "127.0.0.1")
    port_raw = clean_env_value("CHIPPILOT_WEB_PORT", "8000")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"CHIPPILOT_WEB_PORT must be an integer, got {port_raw!r}") from exc
    uvicorn.run("openroad_agent.web_api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
