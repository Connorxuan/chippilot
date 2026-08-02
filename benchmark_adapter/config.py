"""Adapter configuration sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdapterConfig:
    work_dir: Path = Path(os.environ.get("OPENROAD_BENCHMARK_WORK_DIR", "openroad_benchmark_work")).resolve()
    host: str = os.environ.get("OPENROAD_BENCHMARK_HOST", "127.0.0.1")
    port: int = int(os.environ.get("OPENROAD_BENCHMARK_PORT", "8010"))
    poll_seconds: float = float(os.environ.get("OPENROAD_BENCHMARK_POLL_SECONDS", "1"))
    cancel_grace_seconds: float = float(os.environ.get("OPENROAD_BENCHMARK_CANCEL_GRACE_SECONDS", "10"))

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "The unauthenticated benchmark adapter may only bind to a loopback address"
            )

    @property
    def runs_dir(self) -> Path:
        return self.work_dir / "runs"

    @property
    def db_path(self) -> Path:
        return self.work_dir / "benchmark_adapter.sqlite3"

    def ensure(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
