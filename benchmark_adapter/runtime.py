"""Single-run child process entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import traceback
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from benchmark_adapter.config import AdapterConfig
from benchmark_adapter.contracts import validate_execution_contract
from benchmark_adapter.eda_tools import BENCHMARK_TOOLS
from benchmark_adapter.events import BudgetExceeded, EventRecorder, ExecutionBudget
from benchmark_adapter.models import RunRequest, RunResult, RunStatus, SnapshotManifest, Usage
from benchmark_adapter.store import RunStore
from benchmark_adapter.workspace import (
    artifact_manifest,
    immutable_hashes,
    protect_immutable,
    verify_immutable,
)


def build_prompt(request: RunRequest, design_dir: Path) -> str:
    task = request.task
    return f"""You are executing EDA-Agent-Bench task {task.task_id} (Level {task.level}).

Objective:
{task.prompt}

Input design directory: {design_dir}
Available tools: {', '.join(task.tool_catalog)}
Budget: {task.budget.model_dump_json()}
Immutable paths: {json.dumps(task.immutable)}
Forbidden operations: {json.dumps(task.forbidden_operations)}
Required outputs: {json.dumps(task.required_outputs)}
Level contract: {json.dumps(task.level_contract, ensure_ascii=False)}

Use only the visible tools. Do not modify immutable inputs or relax constraints. Keep all
outputs in the active session. Finish with a JSON object containing `conclusion`, `metrics`,
`effective_parameters`, `artifacts`, and, when applicable, `solutions`.
"""


def _content(message: Any) -> str:
    value = getattr(message, "content", "")
    if isinstance(value, list):
        return "\n".join(part.get("text", str(part)) if isinstance(part, dict) else str(part) for part in value)
    return str(value or "")


def _submission(answer: str) -> dict[str, Any]:
    stripped = answer.strip()
    candidates = [stripped]
    if "```json" in stripped:
        candidates.insert(0, stripped.rsplit("```json", 1)[-1].split("```", 1)[0].strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return {"conclusion": {"text": answer}, "metrics": {}, "effective_parameters": {}, "artifacts": []}


async def execute(run_id: str, cfg: AdapterConfig, store: RunStore) -> RunResult:
    row = store.get(run_id)
    if not row:
        raise ValueError(f"Unknown run: {run_id}")
    request = RunRequest.model_validate(row["request"])
    run_dir = cfg.runs_dir / run_id
    recorder = EventRecorder(run_dir / "events.jsonl", run_id)
    manifest = SnapshotManifest.model_validate_json(
        (run_dir / "input" / "benchmark_manifest.json").read_text(encoding="utf-8")
    )
    primary = set(request.task.tool_catalog)
    budget = ExecutionBudget(
        request.task.budget,
        recorder,
        store,
        run_id,
        primary,
        float(manifest.ffe.get("full_flow_seconds", 0) or 0),
    )
    recorder.emit("run_started", task_id=request.task.task_id, level=request.task.level)

    work_root = run_dir / "workspace"
    os.environ["OPENROAD_WORK_DIR"] = str(work_root)
    from openroad_agent.config import OpenROADConfig
    from openroad_agent.main import create_agent
    from openroad_agent.tools.ray_executor import set_ray_job_callback
    from openroad_agent.tools.session_manager import SessionManager

    SessionManager.clear()
    def on_remote_job(event_type: str, job_id: str, address: str) -> None:
        if event_type == "submitted":
            store.register_remote_job(run_id, job_id, address)
        else:
            store.update_remote_job(run_id, job_id, event_type)
        recorder.emit("remote_job", event_type=event_type, job_id=job_id, address=address)

    set_ray_job_callback(on_remote_job)
    config = OpenROADConfig()
    config.work_dir = str(work_root)
    session = SessionManager.start_new(config.work_dir, request.task.task_id)
    shutil.copytree(run_dir / "input", session.designs_dir, dirs_exist_ok=True)
    initial = immutable_hashes(Path(session.designs_dir), manifest, request.task.immutable)
    protect_immutable(Path(session.designs_dir), initial)

    catalog = set(request.task.tool_catalog) if request.task.tool_catalog else {tool.name for tool in BENCHMARK_TOOLS}
    agent = create_agent(
        config=config,
        tool_catalog=catalog,
        execution_observer=budget,
        extra_tools=BENCHMARK_TOOLS,
    )
    messages: list[Any] = [HumanMessage(content=build_prompt(request, Path(session.designs_dir)))]
    answer = ""
    try:
        async for update in agent.astream(
            {"messages": messages},
            config={"configurable": {"thread_id": run_id}},
            stream_mode="updates",
        ):
            for node, output in update.items():
                for message in output.get("messages", []):
                    messages.append(message)
                    if isinstance(message, AIMessage):
                        usage_meta = getattr(message, "usage_metadata", None) or {}
                        budget.consume_llm_turn(usage_meta)
                        answer = _content(message) or answer
                        recorder.emit("llm_turn", node=node, content=_content(message)[:4000], usage=usage_meta)
        from openroad_agent.agents.orchestrator import _extract_subagent_result

        answer = _extract_subagent_result({"messages": messages}) or answer
        status = RunStatus.succeeded
        error = ""
    except BudgetExceeded as exc:
        status = RunStatus.budget_exceeded
        error = str(exc)
    except asyncio.CancelledError as exc:
        status = RunStatus.cancelled
        error = str(exc)
    except Exception as exc:
        status = RunStatus.failed
        error = f"{type(exc).__name__}: {exc}"
        recorder.emit("run_error", error=error, traceback=traceback.format_exc()[-12000:])

    violations = verify_immutable(Path(session.designs_dir), initial)
    event_file = run_dir / "events.jsonl"
    events = []
    if event_file.is_file():
        events = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines() if line]
    used_tools = {event.get("tool") for event in events if event.get("type") == "tool_started"}
    for operation in request.task.forbidden_operations:
        if operation in used_tools:
            violations.append({"type": "forbidden_operation", "operation": operation})
    violations.extend(
        validate_execution_contract(request.task.level, request.task.level_contract, events)
    )

    artifacts = artifact_manifest(run_dir)
    (run_dir / "artifact_manifest.json").write_text(
        json.dumps({"run_id": run_id, "artifacts": artifacts}, indent=2), encoding="utf-8"
    )
    submission = _submission(answer)
    submission["artifact_manifest"] = "artifact_manifest.json"
    missing = []
    available_paths = [item["path"] for item in artifacts]
    for pattern in request.task.required_outputs:
        import fnmatch
        if not any(fnmatch.fnmatch(path, f"*{pattern}") or fnmatch.fnmatch(path, pattern) for path in available_paths):
            missing.append(pattern)
    if missing:
        violations.append({"type": "required_outputs_missing", "paths": missing})

    usage_values = budget.usage()
    result = RunResult(
        run_id=run_id,
        task_id=request.task.task_id,
        status=status,
        answer=answer,
        submission=submission,
        usage=Usage(**usage_values),
        violations=violations,
        error=error,
    )
    recorder.emit("run_finished", status=status.value, violations=violations, usage=result.usage.model_dump())
    (run_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    final_artifacts = artifact_manifest(run_dir)
    producers: dict[str, str] = {}
    for event in events:
        if event.get("type") != "tool_finished":
            continue
        for value in event.get("output_artifacts", []):
            try:
                relative = Path(value).resolve().relative_to(run_dir.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            producers[relative] = event.get("event_id")
    for artifact in final_artifacts:
        artifact["producer_event_id"] = producers.get(artifact["path"])
    (run_dir / "artifact_manifest.json").write_text(
        json.dumps({"run_id": run_id, "artifacts": final_artifacts}, indent=2), encoding="utf-8"
    )
    set_ray_job_callback(None)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    cfg = AdapterConfig()
    store = RunStore(cfg.db_path)
    store.initialize()
    try:
        result = asyncio.run(execute(args.run_id, cfg, store))
        store.update(args.run_id, result.status.value, result=result.model_dump(mode="json"), error=result.error)
    except Exception as exc:
        store.update(args.run_id, RunStatus.failed.value, error=f"Runner bootstrap failed: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
