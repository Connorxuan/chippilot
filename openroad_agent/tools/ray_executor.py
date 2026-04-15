"""Remote OpenROAD execution via Ray Job Submission API on Kubernetes.

Submits OpenROAD TCL jobs to a remote Ray cluster through the Ray dashboard
Job Submission API.  A small self-contained runner is uploaded as the job
working directory, and the cluster shared volume ``/mnt/shared/`` is used as
the working directory so results persist across pods.

Typical cluster layout (inside each pod):
    /mnt/shared/OpenROAD/  ← OpenROAD root
    /mnt/shared/                                        ← shared NFS volume
"""

from __future__ import annotations

import json
import logging
import os
import base64
import shutil
import tempfile
import time
from typing import Any

from ray.job_submission import JobStatus, JobSubmissionClient

logger = logging.getLogger(__name__)

# ── Constants (remote-side paths) ──────────────────────────────────────
# _ENV_SCRIPT = "/mnt/shared/OpenROAD-flow-scripts/env.sh"
_REMOTE_OPENROAD_ROOT = "/mnt/shared/OpenROAD"
_REMOTE_TEST_DIR = f"{_REMOTE_OPENROAD_ROOT}/test"
_REMOTE_WORK_BASE = "/mnt/shared/openroad_work"
_REMOTE_SESSION_BASE = "/mnt/shared/sessions"
_RESULT_MARKER = "__CHIPPILOT_RESULT_JSON__"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip().strip("\"'")
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s must be an integer, got %r; using %d", name, raw, default)
        return default


_JOB_POLL_INTERVAL_SECONDS = _env_int("OPENROAD_RAY_JOB_POLL_INTERVAL_SECONDS", 2)


# ── Job Submission API helpers ─────────────────────────────────────


def _job_address(ray_address: str) -> str:
    configured = os.environ.get("RAY_JOB_ADDRESS", "").strip().strip("\"'")
    if configured:
        return configured
    if ray_address.startswith("ray://"):
        host = ray_address[len("ray://"):].split(":", 1)[0]
        return f"http://{host}:8265"
    return ray_address


def _job_client(ray_address: str) -> JobSubmissionClient:
    return JobSubmissionClient(_job_address(ray_address))


def _terminal_status(status: Any) -> bool:
    return status in {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.STOPPED,
    } or str(status).upper().split(".")[-1] in {"SUCCEEDED", "FAILED", "STOPPED"}


def _status_name(status: Any) -> str:
    return str(status).upper().split(".")[-1]


def _run_job_payload(
    ray_address: str,
    kind: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Submit a self-contained Ray job and return its marker JSON."""
    client = _job_client(ray_address)
    temp_dir = tempfile.mkdtemp(prefix="chippilot_ray_job_")
    try:
        runner_path = os.path.join(temp_dir, "runner.py")
        payload_path = os.path.join(temp_dir, "payload.json")
        with open(runner_path, "w") as f:
            f.write(_JOB_RUNNER_SOURCE)
        with open(payload_path, "w") as f:
            json.dump({"kind": kind, "payload": payload}, f, ensure_ascii=False)

        job_id = client.submit_job(
            entrypoint="python runner.py payload.json",
            runtime_env={"working_dir": temp_dir},
        )
        deadline = time.time() + timeout_seconds
        while True:
            status = client.get_job_status(job_id)
            if _terminal_status(status):
                break
            if time.time() > deadline:
                client.stop_job(job_id)
                logs = client.get_job_logs(job_id)
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"Ray job {job_id} did not complete within {timeout_seconds}s",
                    "metrics": {},
                    "elapsed_s": timeout_seconds,
                    "result_files": [],
                    "run_dir": "",
                    "remote": True,
                    "host": "",
                    "ray_job_id": job_id,
                    "ray_job_logs": logs[-4000:],
                }
            time.sleep(_JOB_POLL_INTERVAL_SECONDS)

        logs = client.get_job_logs(job_id)
        result = _extract_job_result(logs)
        if not result:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Ray job {job_id} finished without a Chippilot result marker.",
                "metrics": {},
                "elapsed_s": 0,
                "result_files": [],
                "run_dir": "",
                "remote": True,
                "host": "",
                "ray_job_id": job_id,
                "ray_job_logs": logs[-4000:],
            }
        result.setdefault("remote", True)
        result["ray_job_id"] = job_id
        if _status_name(status) != "SUCCEEDED" and result.get("success") is not True:
            result["stderr"] = result.get("stderr") or f"Ray job ended with status {status}."
        return result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _extract_job_result(logs: str) -> dict[str, Any] | None:
    for line in reversed(logs.splitlines()):
        if line.startswith(_RESULT_MARKER):
            try:
                return json.loads(line[len(_RESULT_MARKER):])
            except json.JSONDecodeError:
                return None
    return None


_JOB_RUNNER_SOURCE = r'''
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time

MARKER = "__CHIPPILOT_RESULT_JSON__"
REMOTE_OPENROAD_ROOT = "/mnt/shared/OpenROAD"
REMOTE_TEST_DIR = f"{REMOTE_OPENROAD_ROOT}/test"
REMOTE_WORK_BASE = "/mnt/shared/openroad_work"
REMOTE_SESSION_BASE = "/mnt/shared/sessions"


def emit(result):
    print(MARKER + json.dumps(result, ensure_ascii=False), flush=True)


def run_dir_for(session_name, run_label):
    if session_name:
        return os.path.join(REMOTE_SESSION_BASE, session_name, run_label)
    return os.path.join(REMOTE_WORK_BASE, run_label)


def write_sources(base_dir, source_files, subdir):
    target = os.path.join(base_dir, subdir)
    if source_files:
        os.makedirs(target, exist_ok=True)
        for fname, content in source_files.items():
            with open(os.path.join(target, fname), "w") as f:
                f.write(content)
    return target


def collect_files(*dirs):
    result = []
    for directory in dirs:
        if os.path.isdir(directory):
            for fname in os.listdir(directory):
                result.append(os.path.join(directory, fname))
    return result


def run_openroad(payload):
    tcl_script = payload["tcl_script"]
    run_label = payload["run_label"]
    timeout_seconds = int(payload["timeout_seconds"])
    run_dir = run_dir_for(payload.get("session_name"), run_label)
    os.makedirs(run_dir, exist_ok=True)
    results_dir = os.path.join(run_dir, "results")
    os.makedirs(results_dir, exist_ok=True)
    upload_dir = write_sources(run_dir, payload.get("source_files"), "upload")
    tcl_script = tcl_script.replace("__UPLOAD_DIR__", upload_dir)
    result_dir_override = f'\n# result_dir override (injected by ray job)\nset result_dir "{results_dir}"\n'
    patched = re.sub(r'(source\s+"helpers\.tcl"\s*\n)', r'\1' + result_dir_override.replace("\\", "\\\\"), tcl_script, count=1)
    if patched == tcl_script:
        patched = result_dir_override + tcl_script
    tcl_path = os.path.join(run_dir, f"{run_label}.tcl")
    with open(tcl_path, "w") as f:
        f.write(patched)
    shell_cmd = f"export RESULTS_DIR={results_dir} && openroad -no_init -exit {tcl_path}"
    t0 = time.time()
    try:
        proc = subprocess.run(["bash", "-c", shell_cmd], capture_output=True, text=True, timeout=timeout_seconds, cwd=REMOTE_TEST_DIR)
        elapsed = time.time() - t0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
        success = proc.returncode == 0
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return {"success": False, "stdout": "", "stderr": f"Timeout after {timeout_seconds}s", "metrics": {}, "elapsed_s": round(elapsed, 2), "result_files": [], "tcl_path": tcl_path, "log_file": "", "run_dir": run_dir, "remote": True, "host": os.uname().nodename}
    log_file = os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as f:
        f.write(full_stdout)
    if full_stderr:
        with open(os.path.join(run_dir, f"{run_label}.stderr.log"), "w") as f:
            f.write(full_stderr)
    metrics = {}
    for m in re.finditer(r"worst slack\s+(min|max)\s+([-\d.]+)", full_stdout, re.IGNORECASE):
        metrics[f"worst_slack_{m.group(1)}"] = float(m.group(2))
    m = re.search(r"tns\s+([-\d.]+)", full_stdout, re.IGNORECASE)
    if m:
        metrics["tns"] = float(m.group(1))
    m = re.search(r"Design area\s+([\d.]+)\s+u\^2\s+([\d.]+)%\s+utilization", full_stdout)
    if m:
        metrics["design_area_um2"] = float(m.group(1)); metrics["utilization_pct"] = float(m.group(2))
    for m in re.finditer(r"^Total\s+([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)", full_stdout, re.MULTILINE):
        metrics["internal_power"] = float(m.group(1))
        metrics["switching_power"] = float(m.group(2))
        metrics["leakage_power"] = float(m.group(3))
        metrics["total_power"] = float(m.group(4))
    m = re.search(r"Number of violations\s*=\s*(\d+)", full_stdout)
    if m:
        metrics["drc_violations"] = int(m.group(1))
    for m in re.finditer(r"\[INFO ANT-000[12]\]\s+Found\s+(\d+)\s+(pin|net)\s+violations", full_stdout):
        metrics[f"ant_{m.group(2)}_violations"] = int(m.group(1))
    m = re.search(r"\[INFO GPL-0006\]\s+Number of instances:\s+(\d+)", full_stdout)
    if m:
        metrics["num_instances"] = int(m.group(1))
    for line in full_stdout.splitlines():
        if "metric" in line.lower() and ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip().split()[-1] if parts[0].strip() else ""
            val = parts[1].strip()
            if key and key not in metrics:
                try:
                    metrics[key] = float(val)
                except ValueError:
                    metrics[key] = val
    for rf in collect_files(results_dir):
        if rf.endswith(".metrics") or rf.endswith(".json"):
            try:
                with open(rf) as mf:
                    metrics.update(json.load(mf))
            except Exception:
                pass
    return {"success": success, "stdout": full_stdout[-8000:], "stderr": full_stderr[-4000:], "full_log": full_stdout, "metrics": metrics, "elapsed_s": round(elapsed, 2), "result_files": collect_files(results_dir), "tcl_path": tcl_path, "log_file": log_file, "run_dir": run_dir, "remote": True, "host": os.uname().nodename}


def run_yosys(payload):
    synth_script = payload["synth_script"]
    run_label = payload["run_label"]
    timeout_seconds = int(payload["timeout_seconds"])
    run_dir = run_dir_for(payload.get("session_name"), run_label)
    os.makedirs(run_dir, exist_ok=True)
    reports_dir = os.path.join(run_dir, "reports")
    results_dir = os.path.join(run_dir, "results")
    os.makedirs(reports_dir, exist_ok=True); os.makedirs(results_dir, exist_ok=True)
    src_dir = write_sources(run_dir, payload.get("source_files"), "src")
    synth_script = synth_script.replace("__REPORTS_DIR__", reports_dir).replace("__RESULTS_DIR__", results_dir).replace("__SRC_DIR__", src_dir)
    script_path = os.path.join(run_dir, f"{run_label}.ys")
    with open(script_path, "w") as f:
        f.write(synth_script)
    yosys_bin = "/home/ray/oss-cad-suite/bin/yosys"
    shell_cmd = f"{yosys_bin} -c {script_path}"
    t0 = time.time()
    try:
        proc = subprocess.run(["bash", "-c", shell_cmd], capture_output=True, text=True, timeout=timeout_seconds, cwd=REMOTE_TEST_DIR)
        elapsed = time.time() - t0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
        success = proc.returncode == 0
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return {"success": False, "stdout": "", "stderr": f"Yosys timeout after {timeout_seconds}s", "metrics": {}, "elapsed_s": round(elapsed, 2), "result_files": [], "script_path": script_path, "log_file": "", "run_dir": run_dir, "remote": True, "host": os.uname().nodename, "tool": "yosys"}
    log_file = os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as f:
        f.write(full_stdout)
    if full_stderr:
        with open(os.path.join(run_dir, f"{run_label}.stderr.log"), "w") as f:
            f.write(full_stderr)
    metrics = {}
    m = re.search(r"Number of cells:\s+(\d+)", full_stdout)
    if m: metrics["num_cells"] = int(m.group(1))
    m = re.search(r"Number of wires:\s+(\d+)", full_stdout)
    if m: metrics["num_wires"] = int(m.group(1))
    for item in re.finditer(r"Chip area for (?:module|top module)\s+[^\s:]+\s*:\s*([\d.]+)", full_stdout):
        metrics["chip_area"] = float(item.group(1))
    m = re.search(r"Estimated number of transistors:\s+(\d+)", full_stdout)
    if m:
        metrics["estimated_transistors"] = int(m.group(1))
    return {"success": success, "stdout": full_stdout[-8000:], "stderr": full_stderr[-4000:], "full_log": full_stdout, "metrics": metrics, "elapsed_s": round(elapsed, 2), "result_files": collect_files(results_dir, reports_dir), "script_path": script_path, "log_file": log_file, "run_dir": run_dir, "remote": True, "host": os.uname().nodename, "tool": "yosys"}


def run_klayout(payload):
    run_label = payload["run_label"]
    timeout_seconds = int(payload["timeout_seconds"])
    design_name = payload["design_name"]
    run_dir = run_dir_for(payload.get("session_name"), run_label)
    os.makedirs(run_dir, exist_ok=True)
    results_dir = os.path.join(run_dir, "results")
    os.makedirs(results_dir, exist_ok=True)
    out_gds = os.path.join(results_dir, f"{design_name}_final.gds")
    in_files_str = " ".join(payload.get("gds_files") or [])
    layer_map = payload.get("gds_layer_map", "")
    shell_cmd = "export QT_QPA_PLATFORM=offscreen && "
    if payload.get("gds_allow_empty"):
        shell_cmd += f'export GDS_ALLOW_EMPTY="{payload["gds_allow_empty"]}" && '
    shell_cmd += (
        f'klayout -zz -rd design_name={design_name} -rd in_def={payload["def_file"]} '
        f'-rd "in_files={in_files_str}" -rd tech_file={payload["klayout_tech_file"]} '
        f'-rd "layer_map={layer_map}" -rd seal_file= '
        f'-rd out_file={out_gds} -r /mnt/shared/def2stream.py'
    )
    t0 = time.time()
    try:
        proc = subprocess.run(["bash", "-c", shell_cmd], capture_output=True, text=True, timeout=timeout_seconds)
        elapsed = time.time() - t0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
        success = proc.returncode == 0
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        return {"success": False, "stdout": "", "stderr": f"KLayout timeout after {timeout_seconds}s", "metrics": {}, "elapsed_s": round(elapsed, 2), "gds_file": "", "result_files": [], "run_dir": run_dir, "remote": True, "host": os.uname().nodename, "tool": "klayout"}
    log_file = os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as f:
        f.write(full_stdout)
    if full_stderr:
        with open(os.path.join(run_dir, f"{run_label}.stderr.log"), "w") as f:
            f.write(full_stderr)
    gds_exists = os.path.isfile(out_gds)
    metrics = {"klayout_errors": len(re.findall(r"\[ERROR\]", full_stdout)), "klayout_warnings": len(re.findall(r"\[WARNING\]", full_stdout)), "klayout_info_msgs": len(re.findall(r"\[INFO\]", full_stdout)), "all_cells_matched": "All LEF cells have matching GDS/OAS cells" in full_stdout, "no_orphan_cells": "No orphan cells in the final layout" in full_stdout}
    if gds_exists:
        size = os.path.getsize(out_gds); metrics["gds_file_size_bytes"] = size; metrics["gds_file_size_mb"] = round(size / (1024 * 1024), 2)
    return {"success": success and gds_exists, "stdout": full_stdout[-8000:], "stderr": full_stderr[-4000:], "full_log": full_stdout, "metrics": metrics, "elapsed_s": round(elapsed, 2), "gds_file": out_gds if gds_exists else "", "result_files": collect_files(results_dir), "log_file": log_file, "run_dir": run_dir, "remote": True, "host": os.uname().nodename, "tool": "klayout"}


def read_log(payload):
    try:
        with open(payload["path"]) as f:
            lines = f.readlines()
        return {"success": True, "content": "".join(lines[-int(payload["tail_lines"]):])}
    except FileNotFoundError:
        return {"success": False, "content": f"File not found on cluster: {payload['path']}"}
    except Exception as exc:
        return {"success": False, "content": f"Error reading remote log: {exc}"}


def list_dir(payload):
    skip = set(payload.get("skip_dirs") or [])
    base = payload["dir_path"]
    found = []
    sizes = {}
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip]
        for filename in files:
            abs_path = os.path.join(root, filename)
            rel_path = os.path.relpath(abs_path, base)
            found.append(rel_path)
            try:
                sizes[rel_path] = os.path.getsize(abs_path)
            except OSError:
                sizes[rel_path] = None
    return {"success": True, "files": found, "sizes": sizes}


def read_files(payload):
    base = payload["base_dir"]
    files = {}
    for rel in payload.get("files") or []:
        try:
            with open(os.path.join(base, rel), "rb") as f:
                files[rel] = base64.b64encode(f.read()).decode("ascii")
        except Exception:
            files[rel] = None
    return {"success": True, "files": files}


def read_file_chunk(payload):
    path = os.path.join(payload["base_dir"], payload["file"])
    offset = int(payload.get("offset") or 0)
    max_bytes = int(payload.get("max_bytes") or 524288)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
        next_byte = f.read(1)
    return {
        "success": True,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "next_offset": offset + len(data),
        "eof": not next_byte,
    }


def write_file(payload):
    path = payload["path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(base64.b64decode(payload["content_b64"]))
    return {"success": True}


def main():
    with open(sys.argv[1]) as f:
        request = json.load(f)
    kind = request["kind"]
    payload = request["payload"]
    handlers = {
        "openroad": run_openroad,
        "yosys": run_yosys,
        "klayout": run_klayout,
        "read_log": read_log,
        "list_dir": list_dir,
        "read_files": read_files,
        "read_file_chunk": read_file_chunk,
        "write_file": write_file,
    }
    try:
        result = handlers[kind](payload)
    except Exception as exc:
        result = {"success": False, "stdout": "", "stderr": f"Ray job runner error: {type(exc).__name__}: {exc}", "metrics": {}, "elapsed_s": 0, "result_files": [], "run_dir": "", "remote": True, "host": os.uname().nodename}
    emit(result)


if __name__ == "__main__":
    main()
'''


# ── Public synchronous API (called from LangChain tools) ──────────────

def run_openroad_on_ray(
    tcl_script: str,
    run_label: str,
    timeout_seconds: int,
    ray_address: str,
    source_files: dict | None = None,
    session_name: str | None = None,
) -> dict:
    """Submit an OpenROAD run to the Ray cluster and wait for the result.

    This is the main entry point used by ``openroad_runner.py`` when
    ``execution_mode == "ray"``.

    Args:
        tcl_script: The full TCL script content.
        run_label: Short label for file naming on the remote side.
        timeout_seconds: Max wall-clock seconds for OpenROAD.
        ray_address: Ray client address, e.g. ``ray://10.244.70.138:10001``.
        source_files: Optional dict of {filename: content} to upload to
                      the remote cluster (netlist, SDC, etc.).
        session_name: Optional session name for grouping remote outputs.

    Returns:
        A dict identical in structure to the local runner's JSON response.
    """
    try:
        return _run_job_payload(
            ray_address,
            "openroad",
            {
                "tcl_script": tcl_script,
                "run_label": run_label,
                "timeout_seconds": timeout_seconds,
                "source_files": source_files,
                "session_name": session_name,
            },
            timeout_seconds + 120,
        )
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray Job Submission error: {type(exc).__name__}: {exc}",
            "metrics": {},
            "elapsed_s": 0,
            "result_files": [],
            "tcl_path": "",
            "log_file": "",
            "run_dir": "",
            "remote": True,
            "host": "",
        }


def run_yosys_on_ray(
    synth_script: str,
    run_label: str,
    timeout_seconds: int,
    ray_address: str,
    source_files: dict | None = None,
    session_name: str | None = None,
) -> dict:
    """Submit a Yosys synthesis run to the Ray cluster.

    Args:
        synth_script: Complete Yosys TCL/command script.
        run_label: Short label for file naming.
        timeout_seconds: Max wall-clock seconds.
        ray_address: Ray client address.
        source_files: Optional dict of {filename: content} to upload.
        session_name: Optional session name for grouping remote outputs.

    Returns:
        Dict with success, stdout, stderr, metrics, result_files, etc.
    """
    try:
        return _run_job_payload(
            ray_address,
            "yosys",
            {
                "synth_script": synth_script,
                "run_label": run_label,
                "timeout_seconds": timeout_seconds,
                "source_files": source_files,
                "session_name": session_name,
            },
            timeout_seconds + 120,
        )
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray Job Submission Yosys error: {type(exc).__name__}: {exc}",
            "metrics": {},
            "elapsed_s": 0,
            "result_files": [],
            "script_path": "",
            "log_file": "",
            "run_dir": "",
            "remote": True,
            "host": "",
            "tool": "yosys",
        }


# ── Utility: fetch a remote log back to local ─────────────────────────

def read_remote_log(
    log_path: str,
    tail_lines: int,
    ray_address: str,
) -> str:
    """Fetch the tail of a log file from the remote cluster.

    Args:
        log_path: Absolute path on the cluster (e.g. /mnt/shared/…).
        tail_lines: Number of lines from the end.
        ray_address: Ray client address.

    Returns:
        The last *tail_lines* lines of the file.
    """
    try:
        result = _run_job_payload(
            ray_address,
            "read_log",
            {"path": log_path, "tail_lines": tail_lines},
            60,
        )
        return result.get("content") or result.get("stderr") or ""
    except Exception as exc:
        return f"Error reading remote log: {exc}"


# ── Remote ↔ Local sync utilities ─────────────────────────────────────

_REMOTE_SESSION_ROOT = "/mnt/shared/sessions"


def sync_remote_run_to_local(
    remote_run_dir: str,
    local_run_dir: str,
    ray_address: str,
    skip_dirs: list | None = None,
) -> list[str]:
    """Pull all files from a remote run directory to the local session.

    Files that already exist locally (same relative path) are skipped.

    Args:
        remote_run_dir: Absolute path on the cluster, e.g.
            ``/mnt/shared/sessions/<session>/<run_label>/``.
        local_run_dir: Corresponding local directory.
        ray_address: Ray client address.
        skip_dirs: Sub-directory basenames to skip (e.g. ``["upload", "src"]``).

    Returns:
        List of relative paths synced.
    """
    if not remote_run_dir:
        return []
    return _sync_remote_run_to_local_connected(
        remote_run_dir,
        local_run_dir,
        ray_address,
        skip_dirs,
    )


def _sync_remote_run_to_local_connected(
    remote_run_dir: str,
    local_run_dir: str,
    ray_address: str,
    skip_dirs: list | None = None,
) -> list[str]:
    """Pull remote files through Ray Job Submission API."""

    if skip_dirs is None:
        skip_dirs = ["upload", "src"]   # we already have these locally

    # 1. List remote files
    try:
        listed = _run_job_payload(
            ray_address,
            "list_dir",
            {"dir_path": remote_run_dir, "skip_dirs": skip_dirs},
            120,
        )
        if not listed.get("success"):
            logger.warning(
                "Could not list remote dir %s: %s",
                remote_run_dir,
                listed.get("stderr") or listed.get("content") or listed,
            )
            return []
        remote_files = listed.get("files") or []
    except Exception as exc:
        logger.warning("Could not list remote dir %s: %s", remote_run_dir, exc)
        return []

    if not remote_files:
        return []

    # 2. Filter to files we don't already have locally
    to_fetch = []
    for rel in remote_files:
        local_path = os.path.join(local_run_dir, rel)
        if not os.path.exists(local_path):
            to_fetch.append(rel)

    if not to_fetch:
        return []

    # 3. Fetch missing files in chunks. Job logs carry the marker JSON, so avoid
    # returning large GDS/result files as one oversized log line.
    synced: list[str] = []
    chunk_bytes = max(4096, _env_int("OPENROAD_RAY_SYNC_CHUNK_BYTES", 524288))
    for rel in to_fetch:
        local_path = os.path.join(local_run_dir, rel)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        offset = 0
        try:
            with open(local_path, "wb") as f:
                while True:
                    result = _run_job_payload(
                        ray_address,
                        "read_file_chunk",
                        {
                            "base_dir": remote_run_dir,
                            "file": rel,
                            "offset": offset,
                            "max_bytes": chunk_bytes,
                        },
                        120,
                    )
                    if not result.get("success"):
                        raise RuntimeError(result.get("stderr") or result)
                    encoded = result.get("content_b64") or ""
                    f.write(base64.b64decode(encoded))
                    offset = int(result.get("next_offset") or offset)
                    if result.get("eof"):
                        break
            synced.append(rel)
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", rel, exc)
            try:
                os.remove(local_path)
            except OSError:
                pass

    logger.info(
        "Synced %d/%d files from remote → local (%s)",
        len(synced), len(to_fetch), local_run_dir,
    )
    return synced


def push_file_to_remote(
    local_path: str,
    remote_path: str,
    ray_address: str,
) -> bool:
    """Push a single local file to the remote cluster.

    Used to sync session metadata (session.json, chat_log.jsonl) to
    the shared ``/mnt/shared/sessions/`` directory.

    Args:
        local_path: Local file to push.
        remote_path: Destination path on the cluster.
        ray_address: Ray client address.

    Returns:
        True if successful.
    """
    if not os.path.isfile(local_path):
        return False
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        result = _run_job_payload(
            ray_address,
            "write_file",
            {
                "path": remote_path,
                "content_b64": base64.b64encode(data).decode("ascii"),
            },
            120,
        )
        return bool(result.get("success"))
    except Exception as exc:
        logger.warning("Failed to push %s → %s: %s", local_path, remote_path, exc)
        return False


def push_session_meta_to_remote(
    session_base_dir: str,
    session_name: str,
    ray_address: str,
) -> None:
    """Push session.json and chat_log.jsonl to the remote shared dir.

    The remote mirror is ``/mnt/shared/sessions/<session_name>/``.
    """
    remote_session = os.path.join(_REMOTE_SESSION_ROOT, session_name)

    for fname in ("session.json", "chat_log.jsonl"):
        local = os.path.join(session_base_dir, fname)
        if os.path.isfile(local):
            remote = os.path.join(remote_session, fname)
            push_file_to_remote(local, remote, ray_address)


def run_klayout_on_ray(
    def_file: str,
    design_name: str,
    gds_files: list[str],
    klayout_tech_file: str,
    gds_layer_map: str,
    gds_allow_empty: str,
    run_label: str,
    timeout_seconds: int,
    ray_address: str,
    session_name: str | None = None,
) -> dict:
    """Submit a KLayout GDS generation job to the Ray cluster.

    Args:
        def_file: Absolute path to the input DEF file on the cluster.
        design_name: Top-level cell name.
        gds_files: List of GDS cell library paths on the cluster.
        klayout_tech_file: Path to the .lyt tech file on the cluster.
        gds_layer_map: Optional layer map file path (empty = use .lyt embedded).
        gds_allow_empty: Regex for allowed-empty cells (e.g. "fakeram.*").
        run_label: Short label for file naming.
        timeout_seconds: Max wall-clock seconds.
        ray_address: Ray client address.
        session_name: Optional session name for grouping remote outputs.

    Returns:
        Dict with success, stdout, stderr, metrics, gds_file, etc.
    """
    try:
        return _run_job_payload(
            ray_address,
            "klayout",
            {
                "def_file": def_file,
                "design_name": design_name,
                "gds_files": gds_files,
                "klayout_tech_file": klayout_tech_file,
                "gds_layer_map": gds_layer_map,
                "gds_allow_empty": gds_allow_empty,
                "run_label": run_label,
                "timeout_seconds": timeout_seconds,
                "session_name": session_name,
            },
            timeout_seconds + 120,
        )
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray Job Submission KLayout error: {type(exc).__name__}: {exc}",
            "metrics": {},
            "elapsed_s": 0,
            "gds_file": "",
            "result_files": [],
            "run_dir": "",
            "remote": True,
            "host": "",
            "tool": "klayout",
        }
