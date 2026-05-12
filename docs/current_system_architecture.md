# Current System Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                                User Browser                                 │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Web Frontend                                   │
│                                                                             │
│  React App                                                                  │
│  openroad_agent/web_static/app.jsx                                          │
│                                                                             │
│  ┌─────────────────────────────┐    ┌────────────────────────────────────┐  │
│  │ Auth Screen                 │    │ Workspace UI                       │  │
│  │ login / register / logout   │    │ sessions / chat / uploads / files  │  │
│  └─────────────────────────────┘    └────────────────────────────────────┘  │
│                                                                             │
│  All API calls use same-origin fetch with browser-managed cookies.          │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI Backend                                │
│                              openroad_agent/web_api.py                      │
│                                                                             │
│  ┌────────────────────┐  ┌────────────────────┐  ┌───────────────────────┐  │
│  │ Static Routes      │  │ Auth APIs          │  │ Bootstrap / Sessions  │  │
│  │ / and /static      │  │ /api/auth/*        │  │ /api/bootstrap        │  │
│  │                    │  │                    │  │ /api/sessions/*       │  │
│  └────────────────────┘  └─────────┬──────────┘  └───────────┬───────────┘  │
│                                    │                         │              │
│  ┌────────────────────┐  ┌─────────▼──────────┐              │              │
│  │ File APIs          │  │ Chat + Job APIs    │              │              │
│  │ /api/files*        │  │ /api/chat          │              │              │
│  │                    │  │ /api/jobs/*        │              │              │
│  └─────────┬──────────┘  └─────────┬──────────┘              │              │
└────────────┼───────────────────────┼─────────────────────────┼──────────────┘
             │                       │                         │
             ▼                       ▼                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Auth, Metadata, Workspace                           │
│                                                                             │
│  ┌────────────────────────────┐       ┌──────────────────────────────────┐  │
│  │ AuthStore                  │       │ SQLite                           │  │
│  │ signed cookie sessions     │◄─────►│ openroad_work/chippilot.sqlite3  │  │
│  │                            │       │ users / sessions / jobs / threads│  │
│  └─────────────┬──────────────┘       └──────────────────────────────────┘  │
│                │                                                            │
│                │ verifies / sets / deletes                                  │
│                ▼                                                            │
│  ┌────────────────────────────┐                                             │
│  │ HttpOnly Cookie            │                                             │
│  │ chippilot_session          │                                             │
│  └────────────────────────────┘                                             │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ Per-user workspace                                                    │  │
│  │ openroad_work/users/{user_id}/sessions/                               │  │
│  │                                                                       │  │
│  │ session.json  chat_log.jsonl  designs/  attachments/  runs/ artifacts/│  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                               LLM Agent Layer                               │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ Master Orchestrator                                                   │  │
│  │ openroad_agent/agents/orchestrator.py                                 │  │
│  └───────────────┬───────────────────────────────┬───────────────────────┘  │
│                  │                               │                          │
│                  ▼                               ▼                          │
│  ┌────────────────────────────┐       ┌──────────────────────────────────┐  │
│  │ LangChain Checkpointer     │       │ Delegated Sub-agents             │  │
│  │ thread state               │       │ TCL Generator / Analyser         │  │
│  │                            │       │ Optimiser / Explorer             │  │
│  └────────────────────────────┘       └────────────────┬─────────────────┘  │
└─────────────────────────────────────────────────────────┼───────────────────┘
                                                          │
                                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                EDA Tool Layer                               │
│                                                                             │
│  SessionManager       File Manager          TCL Template Tools              │
│  Metrics Parsers      Design Analyzer       Yosys Runner                    │
│  OpenROAD Runner      KLayout Runner                                        │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Execution Backends                             │
│                                                                             │
│  ┌────────────────────────────────────┐  ┌───────────────────────────────┐  │
│  │ Local Execution                    │  │ Remote Ray Cluster            │  │
│  │ OPENROAD_EXEC_MODE=local           │  │ OPENROAD_EXEC_MODE=ray        │  │
│  │ Yosys / OpenROAD / KLayout         │  │ Yosys / OpenROAD / KLayout    │  │
│  └────────────────────────────────────┘  └───────────────┬───────────────┘  │
│                                                          │                  │
│                                                          ▼                  │
│                                             /mnt/shared/sessions            │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
                         Results sync back to session dirs
                         and are surfaced in the React UI.
```

## Main Request Flow

1. The browser loads the React app from FastAPI static routes.
2. On startup, the app calls `/api/auth/me`; the browser sends the `chippilot_session` cookie automatically.
3. Protected APIs use `_require_user()` to verify the signed cookie and resolve the current `AuthUser`.
4. User-specific data is isolated under `openroad_work/users/{user_id}/sessions/`.
5. Chat requests create or load a session, enqueue a job, and run the LangChain orchestrator.
6. The orchestrator can answer directly, call EDA tools, or delegate to specialized sub-agents.
7. Yosys, OpenROAD, and KLayout runners execute locally or through the configured Ray backend.
8. Logs, metrics, generated files, and chat history are written back into the active session directory and surfaced to the UI.
