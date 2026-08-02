"""Persistent local worker that runs each benchmark in a child process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from benchmark_adapter.config import AdapterConfig
from benchmark_adapter.models import RunRequest, RunStatus, TERMINAL_STATUSES
from benchmark_adapter.store import RunStore
from benchmark_adapter.workspace import prepare_run


class Worker:
    def __init__(self, config: AdapterConfig):
        self.config = config
        self.config.ensure()
        self.store = RunStore(config.db_path)
        self.store.initialize()

    def run(self, *, once: bool = False) -> None:
        self.store.recover_interrupted()
        while True:
            row = self.store.claim_next()
            if row:
                self._execute(row)
                if once:
                    return
            elif once:
                return
            else:
                time.sleep(self.config.poll_seconds)

    def _execute(self, row: dict) -> None:
        run_id = row["run_id"]
        run_dir = self.config.runs_dir / run_id
        try:
            request = RunRequest.model_validate(row["request"])
            prepare_run(run_dir, request)
        except Exception as exc:
            self.store.update(run_id, RunStatus.failed.value, error=f"Snapshot preparation failed: {exc}")
            return

        stdout_path = run_dir / "stdout.log"
        env = os.environ.copy()
        env["OPENROAD_BENCHMARK_RUN_ID"] = run_id
        env["OPENROAD_BENCHMARK_WORK_DIR"] = str(self.config.work_dir)
        with stdout_path.open("ab", buffering=0) as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "benchmark_adapter.runtime", "--run-id", run_id],
                stdout=output,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            self.store.update(run_id, RunStatus.running.value, worker_pid=process.pid)
            deadline = time.monotonic() + request.task.budget.timeout_seconds
            forced_status = None
            while process.poll() is None:
                current = self.store.get(run_id)
                if current and current["status"] == RunStatus.cancelling.value:
                    forced_status = RunStatus.cancelled.value
                    self._terminate(process)
                    break
                if time.monotonic() >= deadline:
                    forced_status = RunStatus.budget_exceeded.value
                    self._terminate(process)
                    break
                time.sleep(0.25)
            process.wait()

        current = self.store.get(run_id)
        if forced_status:
            self._stop_remote_jobs(run_id)
            self.store.update(run_id, forced_status, error="Run cancelled" if forced_status == "cancelled" else "Wall-time budget exceeded")
        elif current and current["status"] not in TERMINAL_STATUSES:
            self.store.update(run_id, RunStatus.failed.value, error=f"Runner exited with code {process.returncode}")

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + self.config.cancel_grace_seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _stop_remote_jobs(self, run_id: str) -> None:
        from openroad_agent.tools.ray_executor import _job_client

        for job in self.store.active_remote_jobs(run_id):
            try:
                _job_client(job["address"]).stop_job(job["job_id"])
            except Exception:
                self.store.update_remote_job(run_id, job["job_id"], "orphaned")
            else:
                self.store.update_remote_job(run_id, job["job_id"], "stopped")
