"""Public request and result models for the benchmark adapter."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RunStatus(str, Enum):
    queued = "queued"
    preparing = "preparing"
    running = "running"
    cancelling = "cancelling"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    budget_exceeded = "budget_exceeded"


TERMINAL_STATUSES = {
    RunStatus.succeeded.value,
    RunStatus.failed.value,
    RunStatus.cancelled.value,
    RunStatus.budget_exceeded.value,
}


class Budget(BaseModel):
    timeout_seconds: int = Field(default=3600, ge=1, le=7 * 24 * 3600)
    max_primary_tool_calls: int | None = Field(default=None, ge=0)
    max_total_tool_calls: int | None = Field(default=None, ge=0)
    max_llm_turns: int | None = Field(default=None, ge=0)
    max_parallelism: int = Field(default=1, ge=1, le=256)
    max_failed_runs: int | None = Field(default=None, ge=0)
    cpu_core_hours: float | None = Field(default=None, ge=0)
    full_flow_equivalents: float | None = Field(default=None, ge=0)


class BenchmarkTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    level: int = Field(ge=1, le=5)
    domain: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    tool_catalog: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    immutable: list[str] = Field(default_factory=list)
    forbidden_operations: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)
    level_contract: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_catalog")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class RunRequest(BaseModel):
    schema_version: str = "1.0"
    request_id: str = Field(min_length=1, max_length=200)
    snapshot_path: str = Field(min_length=1)
    task: BenchmarkTask

    @field_validator("schema_version")
    @classmethod
    def supported_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("Only schema_version 1.0 is supported")
        return value


class SnapshotFile(BaseModel):
    path: str
    sha256: str
    role: str = "input"
    immutable: bool = False


class SnapshotManifest(BaseModel):
    schema_version: str = "1.0"
    files: list[SnapshotFile]
    design: dict[str, Any] = Field(default_factory=dict)
    starting_checkpoint: str | None = None
    platform: str | None = None
    ffe: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    wall_time_seconds: float = 0
    cpu_core_hours: float = 0
    tool_calls: int = 0
    primary_tool_calls: int = 0
    failed_tool_calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    full_flow_equivalents: float = 0


class RunResult(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    task_id: str
    status: RunStatus
    answer: str = ""
    submission: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    violations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""
