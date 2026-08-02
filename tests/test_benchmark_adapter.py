from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import httpx
from langchain_core.tools import tool

from benchmark_adapter.events import EventRecorder, ExecutionBudget
from benchmark_adapter.api import create_app
from benchmark_adapter.config import AdapterConfig
from benchmark_adapter.contracts import validate_execution_contract
from benchmark_adapter.models import Budget, RunRequest
from benchmark_adapter.store import RunStore
from benchmark_adapter.workspace import load_snapshot, prepare_run, resolve_inside
from openroad_agent.instrumentation import select_tools


def _snapshot(root: Path, *, bad_hash: bool = False) -> Path:
    root.mkdir()
    design = root / "rtl" / "top.v"
    design.parent.mkdir()
    design.write_text("module top; endmodule\n", encoding="utf-8")
    digest = hashlib.sha256(design.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "files": [
            {
                "path": "rtl/top.v",
                "sha256": "0" * 64 if bad_hash else digest,
                "role": "rtl",
                "immutable": True,
            }
        ],
        "ffe": {"full_flow_seconds": 100},
    }
    (root / "benchmark_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _request(snapshot: Path, request_id: str = "req-1") -> RunRequest:
    return RunRequest.model_validate(
        {
            "schema_version": "1.0",
            "request_id": request_id,
            "snapshot_path": str(snapshot.resolve()),
            "task": {
                "task_id": "L1-SIM-01",
                "level": 1,
                "domain": "simulation",
                "prompt": "Run the test",
                "tool_catalog": ["simulate"],
                "budget": {"timeout_seconds": 10, "max_total_tool_calls": 1},
            },
        }
    )


def test_snapshot_hash_and_workspace(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot")
    root, manifest = load_snapshot(str(snapshot.resolve()))
    assert root == snapshot.resolve()
    assert manifest.files[0].immutable
    run_dir = tmp_path / "run"
    prepare_run(run_dir, _request(snapshot))
    assert (run_dir / "input" / "rtl" / "top.v").is_file()


def test_snapshot_rejects_bad_hash_and_relative_path(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot", bad_hash=True)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_snapshot(str(snapshot.resolve()))
    with pytest.raises(ValueError, match="absolute"):
        load_snapshot("relative/snapshot")


def test_artifact_path_cannot_escape(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    with pytest.raises(ValueError):
        resolve_inside(root, "../secret")


def test_store_idempotency_and_claim(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "adapter.sqlite3")
    store.initialize()
    payload = _request(_snapshot(tmp_path / "snapshot")).model_dump(mode="json")
    first, created = store.create("run_one", payload)
    second, created_again = store.create("run_two", payload)
    assert created and not created_again
    assert first["run_id"] == second["run_id"] == "run_one"
    assert store.claim_next()["status"] == "preparing"
    assert store.recover_interrupted() == 1
    assert store.get("run_one")["status"] == "failed"


def test_instrumented_tool_filters_and_enforces_budget(tmp_path: Path) -> None:
    @tool
    def allowed(value: int) -> str:
        """Return a value."""
        return json.dumps({"value": value, "runtime_seconds": 1})

    @tool
    def hidden(value: int) -> str:
        """A hidden tool."""
        return str(value)

    store = RunStore(tmp_path / "adapter.sqlite3")
    store.initialize()
    snapshot = _snapshot(tmp_path / "snapshot")
    row, _ = store.create("run_one", _request(snapshot).model_dump(mode="json"))
    recorder = EventRecorder(tmp_path / "events.jsonl", "run_one")
    budget = ExecutionBudget(Budget(timeout_seconds=10, max_total_tool_calls=1), recorder, store, "run_one", {"allowed"}, 100)
    selected = select_tools([allowed, hidden], {"allowed"}, budget)
    assert [item.name for item in selected] == ["allowed"]
    assert json.loads(selected[0].invoke({"value": 7}))["value"] == 7
    with pytest.raises(RuntimeError, match="max_total_tool_calls"):
        selected[0].invoke({"value": 8})


@pytest.mark.asyncio
async def test_api_submit_is_idempotent_and_local_artifacts_are_guarded(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot")
    config = AdapterConfig(work_dir=tmp_path / "adapter", host="127.0.0.1", port=8010)
    payload = _request(snapshot).model_dump(mode="json")
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://adapter") as client:
        first = await client.post("/v1/benchmark/runs", json=payload)
        second = await client.post("/v1/benchmark/runs", json=payload)
        health = await client.get("/healthz")
        capabilities = await client.get("/v1/benchmark/capabilities")
    assert first.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert health.json()["status"] == "ok"
    assert capabilities.json()["levels"] == [1, 2, 3, 4, 5]


def test_unauthenticated_api_rejects_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        AdapterConfig(work_dir=tmp_path, host="0.0.0.0")


def test_level_three_contract_checks_capabilities_and_precedence() -> None:
    events = [
        {"type": "tool_started", "tool": "report_timing", "primary": True},
        {"type": "tool_started", "tool": "synthesize", "primary": True},
    ]
    violations = validate_execution_contract(
        3,
        {
            "required_capabilities": ["synthesize", "check_equivalence"],
            "precedence": ["synthesize before report_timing"],
        },
        events,
    )
    assert {item["type"] for item in violations} == {
        "required_capabilities_missing",
        "precedence_violation",
    }
