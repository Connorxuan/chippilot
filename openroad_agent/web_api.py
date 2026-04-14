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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from openroad_agent.agents.orchestrator import _extract_subagent_result, set_stream_callback
from openroad_agent.config import OpenROADConfig
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


class AppState:
    def __init__(self) -> None:
        self.config = OpenROADConfig()
        self.chat_lock = asyncio.Lock()
        self.agent_cache: dict[str, Any] = {}
        self.thread_ids: dict[str, str] = {}


state = AppState()


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


def _sanitize_hint(raw: str) -> str:
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if not collapsed:
        return f"chat_{uuid.uuid4().hex[:8]}"
    words = collapsed.split(" ")
    return " ".join(words[:6])[:48]


def _session_timestamp_prefix(session_name: str) -> str:
    match = re.match(r"^\d{8}_\d{6}", session_name)
    return match.group(0) if match else ""


def _resolve_session_name(session_name: str) -> str:
    sessions_root = Path(state.config.work_dir) / "sessions"
    exact = sessions_root / session_name
    if exact.is_dir():
        return session_name

    prefix = _session_timestamp_prefix(session_name)
    current = SessionManager.get_current()
    if prefix and current and _session_timestamp_prefix(current.session_name) == prefix:
        current_path = sessions_root / current.session_name
        if current_path.is_dir():
            return current.session_name

    if prefix and sessions_root.exists():
        matches = sorted(
            path.name
            for path in sessions_root.iterdir()
            if path.is_dir() and _session_timestamp_prefix(path.name) == prefix
        )
        if len(matches) == 1:
            return matches[0]

    return session_name


def _session_path(session_name: str) -> Path:
    return Path(state.config.work_dir) / "sessions" / _resolve_session_name(session_name)


def _safe_filename(name: str) -> str:
    base = Path(name or "upload").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return cleaned or "upload"


def _session_meta(session_name: str) -> dict[str, Any]:
    meta_path = _session_path(session_name) / "session.json"
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


def _read_chat_messages(session_name: str) -> list[dict[str, Any]]:
    chat_log_path = _session_path(session_name) / "chat_log.jsonl"
    if not chat_log_path.exists():
        return []

    messages: list[dict[str, Any]] = []
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
                label = f"{raw.get('agent_name')}: Tool Call" if raw.get("agent_name") else "Tool Call"
                kind = "activity"
            elif role in {"tool_result", "sub_agent_tool_result"}:
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


def _build_session_summary(session_name: str) -> dict[str, Any]:
    session_name = _resolve_session_name(session_name)
    meta = _session_meta(session_name)
    messages = _read_chat_messages(session_name)
    base_dir = _session_path(session_name)
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


def _build_session_detail(session_name: str) -> dict[str, Any]:
    session_name = _resolve_session_name(session_name)
    meta = _session_meta(session_name)
    base_dir = _session_path(session_name)
    messages = _read_chat_messages(session_name)
    designs_dir = base_dir / "designs"

    design_files = []
    if designs_dir.exists():
        for path in sorted(designs_dir.rglob("*")):
            if path.is_file():
                design_files.append(str(path.relative_to(designs_dir)))

    run_dirs = list_run_dirs(base_dir)

    summary = _build_session_summary(session_name)
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


def _conversation_history(session_name: str) -> list[HumanMessage | AIMessage]:
    history: list[HumanMessage | AIMessage] = []
    for message in _read_chat_messages(session_name):
        if message["role"] == "user":
            content = message.get("agent_context") or message["content"]
            if content:
                history.append(HumanMessage(content=content))
        elif message["role"] == "assistant" and message["content"]:
            history.append(AIMessage(content=message["content"]))
    return history[-16:]


def _list_session_names() -> list[str]:
    sessions_root = Path(state.config.work_dir) / "sessions"
    if not sessions_root.exists():
        return []
    return sorted(
        [path.name for path in sessions_root.iterdir() if path.is_dir()],
        reverse=True,
    )


def _delete_session_dir(session_name: str) -> None:
    resolved_name = _resolve_session_name(session_name)
    sessions_root = (Path(state.config.work_dir) / "sessions").resolve()
    target = (sessions_root / resolved_name).resolve()

    if sessions_root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid session path.")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_name}")

    current = SessionManager.get_current()
    if current and current.session_name == resolved_name:
        SessionManager.clear()

    state.agent_cache.pop(resolved_name, None)
    state.thread_ids.pop(resolved_name, None)
    shutil.rmtree(target)


def _activate_session(session_name: str | None, name_hint: str = "") -> SessionManager:
    if session_name:
        session_name = _resolve_session_name(session_name)
        current = SessionManager.get_current()
        if current and current.session_name == session_name:
            return current
        return SessionManager.load_existing(state.config.work_dir, session_name)
    return SessionManager.start_new(state.config.work_dir, name_hint=name_hint)


def _agent_for_session(session_name: str) -> tuple[Any, str, bool]:
    agent = state.agent_cache.get(session_name)
    created = False
    if agent is None:
        agent = create_agent(config=state.config)
        state.agent_cache[session_name] = agent
        state.thread_ids[session_name] = str(uuid.uuid4())
        created = True
    return agent, state.thread_ids[session_name], created


def _sync_agent_session_key(previous_name: str, current_name: str) -> None:
    if previous_name == current_name:
        return
    if previous_name in state.agent_cache and current_name not in state.agent_cache:
        state.agent_cache[current_name] = state.agent_cache.pop(previous_name)
    if previous_name in state.thread_ids and current_name not in state.thread_ids:
        state.thread_ids[current_name] = state.thread_ids.pop(previous_name)


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


def create_app() -> FastAPI:
    app = FastAPI(title="Chippilot Web UI")
    app.mount(
        "/static",
        NoCacheStaticFiles(directory=str(STATIC_DIR), html=False, follow_symlink=False),
        name="static",
    )

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        sessions = [_build_session_summary(name) for name in _list_session_names()]
        return {
            "app_name": "Chippilot",
            "model_name": state.config.model_name,
            "sessions": sessions,
            "starter_prompts": STARTER_PROMPTS,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": [_build_session_summary(name) for name in _list_session_names()]}

    @app.post("/api/sessions")
    async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        session = _activate_session(None, payload.name_hint.strip())
        return _build_session_detail(session.session_name)

    @app.get("/api/sessions/{session_name}")
    async def get_session(session_name: str) -> dict[str, Any]:
        return _build_session_detail(session_name)

    @app.get("/api/sessions/{session_name}/artifacts")
    async def get_session_artifacts(session_name: str) -> dict[str, Any]:
        resolved_name = _resolve_session_name(session_name)
        base_dir = _session_path(resolved_name)
        _session_meta(resolved_name)
        return {
            "session_name": resolved_name,
            "artifacts": list_session_artifacts(base_dir),
        }

    @app.get("/api/sessions/{session_name}/download")
    async def download_session_file(session_name: str, file_path: str) -> FileResponse:
        resolved_name = _resolve_session_name(session_name)
        base_dir = _session_path(resolved_name)
        _session_meta(resolved_name)
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
    async def download_session_results(session_name: str, include_inputs: bool = False) -> StreamingResponse:
        resolved_name = _resolve_session_name(session_name)
        base_dir = _session_path(resolved_name)
        _session_meta(resolved_name)
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

    @app.delete("/api/sessions/{session_name}")
    async def delete_session(session_name: str) -> dict[str, Any]:
        _delete_session_dir(session_name)
        return {"sessions": [_build_session_summary(name) for name in _list_session_names()]}

    @app.post("/api/files")
    async def upload_files(
        session_name: str | None = Form(default=None),
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        valid_files = [upload for upload in files if upload.filename]
        if not valid_files:
            raise HTTPException(status_code=400, detail="No files were uploaded.")

        session = _activate_session(session_name, "uploaded_designs")
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
            "session": _build_session_detail(session.session_name),
        }

    @app.post("/api/files/delete")
    async def delete_file(payload: DeleteFileRequest) -> dict[str, Any]:
        session = _activate_session(payload.session_name)
        try:
            target = _resolve_attachment_file(session, payload.file_path)
        except HTTPException:
            target = _resolve_design_file(session, payload.file_path)
        target.unlink()
        return {"session": _build_session_detail(session.session_name)}

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> dict[str, Any]:
        message = payload.message.strip()
        attachments = [item for item in payload.attachments if item]
        if not message and not attachments:
            raise HTTPException(status_code=400, detail="Message cannot be empty.")

        async with state.chat_lock:
            session = _activate_session(payload.session_name, _sanitize_hint(message))
            initial_session_name = session.session_name
            model_message = _message_with_attachments(message, attachments, session)
            history = _conversation_history(session.session_name)
            agent, thread_id, created = _agent_for_session(session.session_name)
            session.log_message(
                "user",
                message,
                attachments=attachments,
                agent_context=model_message if model_message != message else None,
            )

            new_message = HumanMessage(content=model_message)
            input_messages = [*history, new_message] if created else [new_message]
            collected_messages = list(input_messages)

            def on_subagent_event(event_type: str, agent_name: str, msg: Any) -> None:
                _log_subagent_stream_event(session, event_type, agent_name, msg)
                _sync_agent_session_key(initial_session_name, session.session_name)

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
                            _sync_agent_session_key(initial_session_name, session.session_name)
            except Exception as exc:
                session.log_message("error", str(exc))
                _sync_agent_session_key(initial_session_name, session.session_name)
                raise HTTPException(
                    status_code=500,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            finally:
                set_stream_callback(None)

            assistant_reply = _extract_subagent_result({"messages": collected_messages})
            session.log_message("assistant", assistant_reply)
            _sync_agent_session_key(initial_session_name, session.session_name)

            return {
                "session": _build_session_detail(session.session_name),
                "assistant_reply": assistant_reply,
            }

    @app.get("/")
    async def index() -> HTMLResponse:
        html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Chippilot</title>
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

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("CHIPPILOT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("CHIPPILOT_WEB_PORT", "8000"))
    uvicorn.run("openroad_agent.web_api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
