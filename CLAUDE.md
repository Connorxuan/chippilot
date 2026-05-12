# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenROAD Pilot (`openroad-agent`) is an LLM-powered EDA automation system built on DeepAgents + LangChain/LangGraph. It drives the OpenROAD RTL-to-GDS physical design flow through an orchestrator agent that delegates to specialized sub-agents (TCL Generator, Analyser, Optimiser, Explorer).

The project has two interfaces:
- **CLI**: `openroad-agent` — interactive chat or single-task mode.
- **Web UI**: `openroad-agent-web` — FastAPI backend serving a React frontend.

## Common Commands

```bash
# Install dependencies (editable)
pip install -e .

# Run the CLI in interactive mode
openroad-agent

# Run a single task
openroad-agent --task "Run the full flow for aes on nangate45"

# Start the web UI (default http://127.0.0.1:8000)
openroad-agent-web

# Lint / format
ruff check .
ruff format .

# Run tests
pytest

# Run benchmark suite
python experiment/run_benchmark.py
pytest experiment/test_benchmark.py -v -k "D1_gcd and I1"
```

## High-Level Architecture

### Agent Layer

The master orchestrator (`openroad_agent/agents/orchestrator.py`) is a LangGraph `create_agent` with two sets of tools:
1. **Direct tools** (EDA runners, parsers, templates, session management) — see `_all_tools()`.
2. **Sub-agent delegation tools** — lightweight react-agents created on-demand via `_make_subagent_tool()` for `tcl_generator`, `analyser`, `optimiser`, and `explorer`. Sub-agents have no memory of the parent conversation; all context must be passed in the `task` string.

Sub-agent streaming is controlled by `_stream_callback` (a `ContextVar`). In CLI mode, `main.py` sets a callback that prints sub-agent events with box-drawing indentation. In non-interactive/test mode, sub-agents run silently.

### Web Stack

- **Frontend**: React SPA in `openroad_agent/web_static/app.jsx`, served by FastAPI static routes.
- **Backend**: FastAPI (`openroad_agent/web_api.py`) with endpoints grouped into auth (`/api/auth/*`), bootstrap/sessions (`/api/bootstrap`, `/api/sessions/*`), chat/jobs (`/api/chat`, `/api/jobs/*`), and files (`/api/files*`).
- **Auth**: Signed-cookie sessions (`chippilot_session`) backed by `AuthStore` (`auth.py`). `CHIPPILOT_SECRET_KEY` must be set in production; otherwise an ephemeral secret is generated on startup and existing logins become invalid after restart.
- **Storage**: `ChippilotStore` (`storage.py`) uses SQLite (`openroad_work/chippilot.sqlite3`) for users, sessions, jobs, and threads. Per-user workspaces live under `openroad_work/users/{user_id}/sessions/`.

### Execution Backends

Tool runners support two execution modes, controlled by `OPENROAD_EXEC_MODE` (default `ray`):
- **local**: Runs OpenROAD / Yosys / KLayout as local subprocesses.
- **ray**: Submits jobs to a remote Ray-on-K8s cluster via `ray.job_submission.JobSubmissionClient` (`ray_executor.py`). The cluster is expected to mount shared storage at `/mnt/shared/`.

### Session & Checkpointer Model

- **SessionManager** (`tools/session_manager.py`) is a module-level singleton + `ContextVar`. It groups all artifacts from one conversation into a timestamped directory under `openroad_work/sessions/` (or `openroad_work/users/{user_id}/sessions/` in web mode). All tools discover the active session through this singleton without threading extra arguments through every call chain.
- **LangGraph persistence**: A custom SQLite checkpointer (`checkpoint.py`) implements LangGraph's `BaseCheckpointSaver` protocol and stores thread state in `openroad_work/chippilot.sqlite3` with WAL mode enabled.

### Key Data Flows

1. **CLI flow**: `main.py` → `create_orchestrator_agent()` → auto-starts a `SessionManager` → streams updates via `agent.astream(..., stream_mode="updates")`.
2. **Web chat flow**: React calls `/api/chat` → FastAPI creates/loads session, enqueues a job, runs the orchestrator with the same `SessionManager` and SQLite checkpointer, and streams SSE back to the browser.
3. **Tool result extraction**: Sub-agent outputs are normalized via `_extract_subagent_result()` in `orchestrator.py`, which prefers the last AI message, falls back to concatenating all AI messages, and finally compiles a JSON summary from tool results.

## Important Code Locations

- `openroad_agent/config.py` — `OpenROADConfig` dataclass, environment variable defaults, and `FLOW_STAGES` list.
- `openroad_agent/agents/orchestrator.py` — Master agent composition and sub-agent factory.
- `openroad_agent/tools/ray_executor.py` — Ray Job Submission API integration.
- `openroad_agent/checkpoint.py` — Custom SQLite `BaseCheckpointSaver` for LangGraph.
- `openroad_agent/auth.py` — Cookie-session auth and `AuthStore`.
- `openroad_agent/storage.py` — SQLite schema and `ChippilotStore`.
- `experiment/run_benchmark.py` — Benchmark runner for Design × Task evaluations.
