"""FastAPI control plane for EDA-Agent-Bench runs."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from benchmark_adapter.config import AdapterConfig
from benchmark_adapter.models import RunRequest, TERMINAL_STATUSES
from benchmark_adapter.store import RunStore
from benchmark_adapter.workspace import load_snapshot, resolve_inside


CAPABILITIES = {
    "schema_versions": ["1.0"],
    "levels": [1, 2, 3, 4, 5],
    "execution_backends": ["local", "ray"],
    "tools": [
        "simulate", "run_regression", "synthesize", "check_equivalence",
        "report_timing", "initialize_floorplan", "global_place", "detailed_place",
        "clock_tree_synthesis", "global_route", "detailed_route", "extract_parasitics",
        "run_klayout_gds", "run_drc", "run_lvs", "report_power", "query_waveform", "apply_text_replacement",
        "cross_probe", "create_checkpoint", "rollback_checkpoint", "create_trial",
        "register_trial_result", "register_solution", "compute_pareto_front",
    ],
    "note": "Run completion is not a benchmark pass; hidden evaluation is external.",
}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "request"} | {
        "task": row["request"]["task"]
    }


def create_app(config: AdapterConfig | None = None) -> FastAPI:
    cfg = config or AdapterConfig()
    cfg.ensure()
    store = RunStore(cfg.db_path)
    store.initialize()

    app = FastAPI(title="EDA-Agent-Bench Adapter", version="1.0")
    app.state.adapter_config = cfg
    app.state.run_store = store

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "work_dir": str(cfg.work_dir)}

    @app.get("/v1/benchmark/capabilities")
    async def capabilities() -> dict[str, Any]:
        from benchmark_adapter.eda_tools import _binary

        executables = {}
        for name in ("openroad", "yosys", "iverilog", "vvp", "verilator", "eqy", "klayout", "netgen"):
            try:
                executables[name] = {"available": True, "path": _binary(name)}
            except FileNotFoundError:
                executables[name] = {"available": False, "path": ""}
        return {**CAPABILITIES, "executables": executables}

    @app.post("/v1/benchmark/runs", status_code=202)
    async def submit(payload: RunRequest) -> dict[str, Any]:
        try:
            load_snapshot(payload.snapshot_path)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_id = "run_" + uuid.uuid4().hex
        row, created = store.create(run_id, payload.model_dump(mode="json"))
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "created": created,
            "status_url": f"/v1/benchmark/runs/{row['run_id']}",
        }

    @app.get("/v1/benchmark/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        row = store.get(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="Unknown run")
        return _public(row)

    @app.post("/v1/benchmark/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> dict[str, Any]:
        row = store.request_cancel(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="Unknown run")
        return _public(row)

    @app.get("/v1/benchmark/runs/{run_id}/events")
    async def events(run_id: str) -> StreamingResponse:
        if not store.get(run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        event_path = cfg.runs_dir / run_id / "events.jsonl"

        async def stream():
            offset = 0
            while True:
                if event_path.is_file():
                    with event_path.open("r", encoding="utf-8") as handle:
                        handle.seek(offset)
                        for line in handle:
                            yield f"data: {line.rstrip()}\n\n"
                        offset = handle.tell()
                row = store.get(run_id)
                if row and row["status"] in TERMINAL_STATUSES:
                    yield f"data: {json.dumps({'type': 'terminal', 'status': row['status']})}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/v1/benchmark/runs/{run_id}/manifest")
    async def manifest(run_id: str) -> dict[str, Any]:
        if not store.get(run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        path = cfg.runs_dir / run_id / "artifact_manifest.json"
        if not path.is_file():
            return {"run_id": run_id, "artifacts": []}
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/v1/benchmark/runs/{run_id}/artifacts")
    async def artifacts(run_id: str) -> dict[str, Any]:
        return await manifest(run_id)

    @app.get("/v1/benchmark/runs/{run_id}/artifacts/{relative_path:path}")
    async def artifact(run_id: str, relative_path: str) -> FileResponse:
        if not store.get(run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        try:
            path = resolve_inside(cfg.runs_dir / run_id, relative_path)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Unknown artifact") from exc
        if (
            relative_path.startswith("input/")
            or "/designs/" in f"/{relative_path}"
            or relative_path in {"task.json", "immutable_initial.json"}
        ):
            raise HTTPException(status_code=403, detail="Input files are not downloadable")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    return app
