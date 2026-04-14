"""FastAPI app for the Chippilot web interface."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from openroad_agent.agents.orchestrator import _extract_subagent_result
from openroad_agent.config import OpenROADConfig
from openroad_agent.main import create_agent
from openroad_agent.tools.session_manager import SessionManager

STATIC_DIR = Path(__file__).resolve().parent / "web_static"

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
    message: str = Field(min_length=1)
    session_name: str | None = None


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


def _session_path(session_name: str) -> Path:
    return Path(state.config.work_dir) / "sessions" / session_name


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

            role = raw.get("role", "unknown")
            content = str(raw.get("content", ""))
            label = role.replace("_", " ").title()
            kind = "message"

            if role == "tool_call":
                tool_name = raw.get("tool_name", "tool")
                content = f"{tool_name}({_brief_args(raw.get('args', {}))})"
                label = "Tool Call"
                kind = "activity"
            elif role in {"tool_result", "sub_agent_tool_result"}:
                label = raw.get("tool_name", "Tool Result")
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
                }
            )

    return messages


def _build_session_summary(session_name: str) -> dict[str, Any]:
    meta = _session_meta(session_name)
    messages = _read_chat_messages(session_name)
    base_dir = _session_path(session_name)
    designs_dir = base_dir / "designs"
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
        "run_count": len(meta.get("runs", [])),
        "design_file_count": _count_design_files(designs_dir),
    }


def _build_session_detail(session_name: str) -> dict[str, Any]:
    meta = _session_meta(session_name)
    base_dir = _session_path(session_name)
    messages = _read_chat_messages(session_name)
    designs_dir = base_dir / "designs"

    design_files = []
    if designs_dir.exists():
        for path in sorted(designs_dir.rglob("*")):
            if path.is_file():
                design_files.append(str(path.relative_to(designs_dir)))

    run_dirs = []
    if base_dir.exists():
        for path in sorted(base_dir.iterdir()):
            if path.is_dir() and path.name != "designs":
                run_dirs.append(path.name)

    summary = _build_session_summary(session_name)
    summary.update(
        {
            "base_dir": str(base_dir),
            "messages": messages,
            "design_files": design_files,
            "run_dirs": run_dirs,
            "runs": meta.get("runs", []),
        }
    )
    return summary


def _conversation_history(session_name: str) -> list[HumanMessage | AIMessage]:
    history: list[HumanMessage | AIMessage] = []
    for message in _read_chat_messages(session_name):
        if message["role"] == "user" and message["content"]:
            history.append(HumanMessage(content=message["content"]))
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


def _activate_session(session_name: str | None, name_hint: str = "") -> SessionManager:
    if session_name:
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


def create_app() -> FastAPI:
    app = FastAPI(title="Chippilot Web UI")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> dict[str, Any]:
        message = payload.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message cannot be empty.")

        async with state.chat_lock:
            session = _activate_session(payload.session_name, _sanitize_hint(message))
            history = _conversation_history(session.session_name)
            agent, thread_id, created = _agent_for_session(session.session_name)
            session.log_message("user", message)

            input_messages = [*history, HumanMessage(content=message)] if created else [HumanMessage(content=message)]
            collected_messages = list(input_messages)

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
            except Exception as exc:
                session.log_message("error", str(exc))
                raise HTTPException(
                    status_code=500,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc

            assistant_reply = _extract_subagent_result({"messages": collected_messages})
            session.log_message("assistant", assistant_reply)

            return {
                "session": _build_session_detail(session.session_name),
                "assistant_reply": assistant_reply,
            }

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("CHIPPILOT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("CHIPPILOT_WEB_PORT", "8000"))
    uvicorn.run("openroad_agent.web_api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
