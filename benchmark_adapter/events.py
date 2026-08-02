"""Append-only event stream and budget enforcement."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_adapter.models import Budget
from benchmark_adapter.store import RunStore


class BudgetExceeded(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventRecorder:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._sequence = 0
        self._lock = threading.Lock()

    def emit(self, event_type: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "event_id": f"{self.run_id}:{self._sequence}",
                "run_id": self.run_id,
                "type": event_type,
                "timestamp": utc_now(),
                **data,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            return event


class ExecutionBudget:
    """Shared run budget consumed by instrumented main and sub-agent tools."""

    def __init__(
        self,
        budget: Budget,
        recorder: EventRecorder,
        store: RunStore,
        run_id: str,
        primary_tools: set[str],
        full_flow_seconds: float = 0,
    ):
        self.budget = budget
        self.recorder = recorder
        self.store = store
        self.run_id = run_id
        self.primary_tools = primary_tools
        self.full_flow_seconds = full_flow_seconds
        self.started = time.monotonic()
        self.tool_calls = 0
        self.primary_tool_calls = 0
        self.failed_tool_calls = 0
        self.llm_turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.tool_runtime_seconds = 0.0
        self.cpu_core_hours = 0.0
        self.active_tools = 0
        self._lock = threading.Lock()

    def _check_cancelled(self) -> None:
        row = self.store.get(self.run_id)
        if row and row["status"] == "cancelling":
            raise asyncio_cancelled_error()

    def before_tool(self, tool_name: str, arguments: Any) -> dict[str, Any]:
        self._check_cancelled()
        with self._lock:
            if time.monotonic() - self.started >= self.budget.timeout_seconds:
                raise BudgetExceeded("timeout_seconds exceeded")
            if self.budget.max_failed_runs is not None and self.failed_tool_calls >= self.budget.max_failed_runs:
                raise BudgetExceeded("max_failed_runs exceeded")
            if self.budget.cpu_core_hours is not None and self.cpu_core_hours >= self.budget.cpu_core_hours:
                raise BudgetExceeded("cpu_core_hours exceeded")
            if (
                self.budget.full_flow_equivalents is not None
                and self.full_flow_seconds > 0
                and self.tool_runtime_seconds / self.full_flow_seconds >= self.budget.full_flow_equivalents
            ):
                raise BudgetExceeded("full_flow_equivalents exceeded")
            if self.active_tools >= self.budget.max_parallelism:
                raise BudgetExceeded("max_parallelism exceeded")
            if self.budget.max_total_tool_calls is not None and self.tool_calls >= self.budget.max_total_tool_calls:
                raise BudgetExceeded("max_total_tool_calls exceeded")
            is_primary = tool_name in self.primary_tools
            if (
                is_primary
                and self.budget.max_primary_tool_calls is not None
                and self.primary_tool_calls >= self.budget.max_primary_tool_calls
            ):
                raise BudgetExceeded("max_primary_tool_calls exceeded")
            self.tool_calls += 1
            if is_primary:
                self.primary_tool_calls += 1
            self.active_tools += 1
        return self.recorder.emit(
            "tool_started", tool=tool_name, agent="agent", arguments=arguments, primary=is_primary
        )

    def after_tool(
        self, tool_name: str, started_event: dict[str, Any], result: Any = None, error: str = ""
    ) -> None:
        with self._lock:
            self.active_tools = max(0, self.active_tools - 1)
            if error:
                self.failed_tool_calls += 1
        parsed = result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                parsed = {"output": result[:4000]}
        if isinstance(parsed, dict):
            with self._lock:
                self.tool_runtime_seconds += float(
                    parsed.get("runtime_seconds", parsed.get("elapsed_s", 0)) or 0
                )
                resource = parsed.get("resource_usage", {})
                if isinstance(resource, dict):
                    self.cpu_core_hours += float(resource.get("cpu_core_hours", 0) or 0)
        self.recorder.emit(
            "tool_finished",
            tool=tool_name,
            parent_event_id=started_event["event_id"],
            status="failed" if error else "success",
            result=parsed,
            effective_parameters=parsed.get("effective_parameters", {}) if isinstance(parsed, dict) else {},
            output_artifacts=parsed.get("artifacts", []) if isinstance(parsed, dict) else [],
            metrics=parsed.get("metrics", {}) if isinstance(parsed, dict) else {},
            error=error,
        )
        if self.budget.max_failed_runs is not None and self.failed_tool_calls > self.budget.max_failed_runs:
            raise BudgetExceeded("max_failed_runs exceeded")
        if self.budget.cpu_core_hours is not None and self.cpu_core_hours > self.budget.cpu_core_hours:
            raise BudgetExceeded("cpu_core_hours exceeded")
        if (
            self.budget.full_flow_equivalents is not None
            and self.full_flow_seconds > 0
            and self.tool_runtime_seconds / self.full_flow_seconds > self.budget.full_flow_equivalents
        ):
            raise BudgetExceeded("full_flow_equivalents exceeded")

    def consume_llm_turn(self, usage: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.llm_turns += 1
            usage = usage or {}
            self.input_tokens += int(usage.get("input_tokens", usage.get("input_token_count", 0)) or 0)
            self.output_tokens += int(usage.get("output_tokens", usage.get("output_token_count", 0)) or 0)
            if self.budget.max_llm_turns is not None and self.llm_turns > self.budget.max_llm_turns:
                raise BudgetExceeded("max_llm_turns exceeded")

    def usage(self) -> dict[str, Any]:
        return {
            "wall_time_seconds": round(time.monotonic() - self.started, 3),
            "tool_calls": self.tool_calls,
            "primary_tool_calls": self.primary_tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "llm_calls": self.llm_turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cpu_core_hours": round(self.cpu_core_hours, 6),
            "full_flow_equivalents": (
                self.tool_runtime_seconds / self.full_flow_seconds if self.full_flow_seconds > 0 else 0
            ),
        }


def asyncio_cancelled_error() -> BaseException:
    import asyncio

    return asyncio.CancelledError("Benchmark run cancelled")
