"""Remote OpenROAD execution via Ray on Kubernetes.

Submits OpenROAD TCL jobs to a remote Ray cluster.  Files are transferred
through Ray's object store (no shared filesystem needed between local and
cluster).  Within the cluster the shared volume ``/mnt/shared/`` is used as
the working directory so results persist across pods.

Typical cluster layout (inside each pod):
    /mnt/shared/OpenROAD/  ← OpenROAD root
    /mnt/shared/                                        ← shared NFS volume
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import ray

logger = logging.getLogger(__name__)

# ── Constants (remote-side paths) ──────────────────────────────────────
# _ENV_SCRIPT = "/mnt/shared/OpenROAD-flow-scripts/env.sh"
_REMOTE_OPENROAD_ROOT = "/mnt/shared/OpenROAD"
_REMOTE_TEST_DIR = f"{_REMOTE_OPENROAD_ROOT}/test"
_REMOTE_WORK_BASE = "/mnt/shared/openroad_work"
_REMOTE_SESSION_BASE = "/mnt/shared/sessions"


# ── Singleton connection management ───────────────────────────────────

_ray_initialised = False


def _ensure_ray(address: str) -> None:
    """Connect to the remote Ray cluster (idempotent)."""
    global _ray_initialised
    if _ray_initialised and ray.is_initialized():
        return
    logger.info("Connecting to Ray cluster at %s …", address)
    ray.init(address=address, ignore_reinit_error=True)
    _ray_initialised = True
    logger.info("Ray connected.  Nodes: %d", len(ray.nodes()))


def disconnect_ray() -> None:
    """Gracefully disconnect from the Ray cluster."""
    global _ray_initialised
    if ray.is_initialized():
        ray.shutdown()
    _ray_initialised = False


# ── Remote task definition ─────────────────────────────────────────────
#
# This function is serialized and shipped to the worker pod.
# It must be self-contained: all imports inside the function body.

@ray.remote
def _run_openroad_remote(
    tcl_script: str,
    run_label: str,
    timeout_seconds: int,
    source_files: dict | None = None,
    session_name: str | None = None,
) -> dict:
    """Execute an OpenROAD TCL script on the remote cluster node.

    Steps:
        1. Create run directory under /mnt/shared/sessions/<session>/<run_label>/
           (or /mnt/shared/openroad_work/<run_label>/ if no session).
        2. Write uploaded source files (netlist, SDC, etc.) to an upload/ subdir.
        3. Write the TCL script to disk.
        4. Execute:  bash -c 'source env.sh && openroad -no_init -exit <tcl>'
           with cwd = OpenROAD/test  so helpers.tcl etc. resolve.
        5. Collect stdout, stderr, result files, metrics.
        6. Return everything as a dict (Ray serialises it back).
    """
    import json as _json
    import os as _os
    import subprocess as _sp
    import time as _time

    # env_script = "/mnt/shared/OpenROAD-flow-scripts/env.sh"
    test_dir = "/mnt/shared/OpenROAD/test"
    work_base = "/mnt/shared/openroad_work"
    session_base_root = "/mnt/shared/sessions"

    if session_name:
        session_base = _os.path.join(session_base_root, session_name)
        run_dir = _os.path.join(session_base, run_label)
    else:
        run_dir = _os.path.join(work_base, run_label)
    _os.makedirs(run_dir, exist_ok=True)

    results_dir = _os.path.join(run_dir, "results")
    _os.makedirs(results_dir, exist_ok=True)

    # Write uploaded source files to an upload/ subdir
    upload_dir = _os.path.join(run_dir, "upload")
    if source_files:
        _os.makedirs(upload_dir, exist_ok=True)
        for fname, content in source_files.items():
            with open(_os.path.join(upload_dir, fname), "w") as f:
                f.write(content)

    # Substitute __UPLOAD_DIR__ placeholder with the actual path
    tcl_script = tcl_script.replace("__UPLOAD_DIR__", upload_dir)

    # Inject result_dir override right after 'source "helpers.tcl"'
    # The remote cluster's helpers.tcl may not honour the RESULTS_DIR
    # env var, so we explicitly override the TCL variable.
    import re as _re
    _result_dir_override = (
        f'\n# ── result_dir override (injected by ray_executor) ──\n'
        f'set result_dir "{results_dir}"\n'
    )
    tcl_patched = _re.sub(
        r'(source\s+"helpers\.tcl"\s*\n)',
        r'\1' + _result_dir_override.replace('\\', '\\\\'),
        tcl_script,
        count=1,
    )
    # Fallback: if helpers.tcl source line not found, prepend override
    if tcl_patched == tcl_script:
        tcl_patched = _result_dir_override + tcl_script

    tcl_path = _os.path.join(run_dir, f"{run_label}.tcl")
    with open(tcl_path, "w") as f:
        f.write(tcl_patched)

    # Build the shell command:
    #   source env.sh  →  sets PATH, LD_LIBRARY_PATH, etc.
    #   RESULTS_DIR=…  →  tells flow.tcl where to write results (belt & suspenders)
    #   openroad -no_init -exit <tcl>
    shell_cmd = (
        # f'source {env_script} && '
        f'export RESULTS_DIR={results_dir} && '
        f'openroad -no_init -exit {tcl_path}'
    )

    t0 = _time.time()
    try:
        proc = _sp.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=test_dir,
        )
        elapsed = _time.time() - t0
        success = proc.returncode == 0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
    except _sp.TimeoutExpired:
        elapsed = _time.time() - t0
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Timeout after {timeout_seconds}s",
            "metrics": {},
            "elapsed_s": round(elapsed, 2),
            "result_files": [],
            "tcl_path": tcl_path,
            "log_file": "",
            "run_dir": run_dir,
            "remote": True,
            "host": _os.uname().nodename,
        }

    # ── Persist full logs ──────────────────────────────────────────────
    log_file = _os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as lf:
        lf.write(full_stdout)
    if full_stderr:
        stderr_log = _os.path.join(run_dir, f"{run_label}.stderr.log")
        with open(stderr_log, "w") as lf:
            lf.write(full_stderr)

    # ── Truncate for JSON response ─────────────────────────────────────
    stdout = full_stdout[-8000:] if len(full_stdout) > 8000 else full_stdout
    stderr = full_stderr[-4000:] if len(full_stderr) > 4000 else full_stderr

    # ── Collect result files ───────────────────────────────────────────
    result_files: list[str] = []
    if _os.path.isdir(results_dir):
        for fname in _os.listdir(results_dir):
            result_files.append(_os.path.join(results_dir, fname))

    # ── Parse metrics from stdout ──────────────────────────────────────
    import re as _re2
    metrics: dict = {}

    # 1) Worst slack (min / max)
    for m in _re2.finditer(
        r'worst slack\s+(min|max)\s+([-\d.]+)', full_stdout, _re2.IGNORECASE
    ):
        metrics[f"worst_slack_{m.group(1)}"] = float(m.group(2))

    # 2) TNS
    m = _re2.search(r'tns\s+([-\d.]+)', full_stdout, _re2.IGNORECASE)
    if m:
        metrics["tns"] = float(m.group(1))

    # 3) Design area & utilization  (e.g.  "Design area 578 u^2 10% utilization.")
    m = _re2.search(
        r'Design area\s+([\d.]+)\s+u\^2\s+([\d.]+)%\s+utilization',
        full_stdout,
    )
    if m:
        metrics["design_area_um2"] = float(m.group(1))
        metrics["utilization_pct"] = float(m.group(2))

    # 4) Total power (last Total line from report_power)
    for m in _re2.finditer(
        r'^Total\s+([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)\s+([\d.e+-]+)',
        full_stdout,
        _re2.MULTILINE,
    ):
        metrics["internal_power"] = float(m.group(1))
        metrics["switching_power"] = float(m.group(2))
        metrics["leakage_power"] = float(m.group(3))
        metrics["total_power"] = float(m.group(4))

    # 5) DRC violations
    m = _re2.search(r'Number of violations\s*=\s*(\d+)', full_stdout)
    if m:
        metrics["drc_violations"] = int(m.group(1))

    # 6) Antenna violations
    for m in _re2.finditer(
        r'\[INFO ANT-000[12]\]\s+Found\s+(\d+)\s+(pin|net)\s+violations',
        full_stdout,
    ):
        metrics[f"ant_{m.group(2)}_violations"] = int(m.group(1))

    # 7) Instance / cell counts from GPL
    m = _re2.search(r'\[INFO GPL-0006\]\s+Number of instances:\s+(\d+)', full_stdout)
    if m:
        metrics["num_instances"] = int(m.group(1))

    # 8) Generic "metric: value" lines (fallback)
    for line in full_stdout.splitlines():
        if "metric" in line.lower() and ":" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                key = parts[0].strip().split()[-1] if parts[0].strip() else ""
                val = parts[1].strip()
                if key and key not in metrics:
                    try:
                        metrics[key] = float(val)
                    except ValueError:
                        metrics[key] = val

    # ── Also load metrics JSON files if produced ───────────────────────
    for rf in result_files:
        if rf.endswith(".metrics") or rf.endswith(".json"):
            try:
                with open(rf) as mf:
                    metrics.update(_json.load(mf))
            except Exception:
                pass

    return {
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "full_log": full_stdout,
        "metrics": metrics,
        "elapsed_s": round(elapsed, 2),
        "result_files": result_files,
        "tcl_path": tcl_path,
        "log_file": log_file,
        "run_dir": run_dir,
        "remote": True,
        "host": _os.uname().nodename,
    }


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
    _ensure_ray(ray_address)

    ref = _run_openroad_remote.remote(
        tcl_script, run_label, timeout_seconds, source_files, session_name
    )

    # Wait with a generous local-side timeout (network + scheduling + run)
    local_timeout = timeout_seconds + 120
    try:
        result = ray.get(ref, timeout=local_timeout)
    except ray.exceptions.GetTimeoutError:
        ray.cancel(ref, force=True)
        return {
            "success": False,
            "stdout": "",
            "stderr": (
                f"Ray task did not complete within {local_timeout}s "
                f"(remote timeout: {timeout_seconds}s)"
            ),
            "metrics": {},
            "elapsed_s": local_timeout,
            "result_files": [],
            "tcl_path": "",
            "log_file": "",
            "run_dir": "",
            "remote": True,
            "host": "",
        }
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray execution error: {type(exc).__name__}: {exc}",
            "metrics": {},
            "elapsed_s": 0,
            "result_files": [],
            "tcl_path": "",
            "log_file": "",
            "run_dir": "",
            "remote": True,
            "host": "",
        }

    return result


# ── Yosys remote task ──────────────────────────────────────────────────

@ray.remote
def _run_yosys_remote(
    synth_script: str,
    run_label: str,
    timeout_seconds: int,
    source_files: dict | None = None,
    session_name: str | None = None,
) -> dict:
    """Execute a Yosys synthesis script on the remote cluster node.

    Steps:
        1. Create run directory under /mnt/shared/sessions/<session>/<run_label>/
           (or /mnt/shared/openroad_work/<run_label>/ if no session).
        2. Write the Yosys script to disk.
        3. Execute:  bash -c 'source env.sh && yosys -c <script>'
        4. Collect stdout, stderr, result files, metrics.
        5. Return everything as a dict.
    """
    import json as _json
    import os as _os
    import re as _re
    import subprocess as _sp
    import time as _time

    # env_script = "/home/ray/OpenROAD-flow-scripts/env.sh"
    test_dir = "/mnt/shared/OpenROAD/test"
    work_base = "/mnt/shared/openroad_work"
    session_base_root = "/mnt/shared/sessions"

    if session_name:
        session_base = _os.path.join(session_base_root, session_name)
        run_dir = _os.path.join(session_base, run_label)
    else:
        run_dir = _os.path.join(work_base, run_label)
    _os.makedirs(run_dir, exist_ok=True)

    reports_dir = _os.path.join(run_dir, "reports")
    _os.makedirs(reports_dir, exist_ok=True)

    results_dir = _os.path.join(run_dir, "results")
    _os.makedirs(results_dir, exist_ok=True)

    # Write uploaded source files to a src/ subdir
    src_dir = _os.path.join(run_dir, "src")
    if source_files:
        _os.makedirs(src_dir, exist_ok=True)
        for fname, content in source_files.items():
            with open(_os.path.join(src_dir, fname), "w") as f:
                f.write(content)

    # Substitute path placeholders in the script
    synth_script = synth_script.replace("__REPORTS_DIR__", reports_dir)
    synth_script = synth_script.replace("__RESULTS_DIR__", results_dir)
    synth_script = synth_script.replace("__SRC_DIR__", src_dir)

    script_path = _os.path.join(run_dir, f"{run_label}.ys")
    with open(script_path, "w") as f:
        f.write(synth_script)

    shell_cmd = (
        # f'source {env_script} && '
        f'/home/ray/oss-cad-suite/bin/yosys -c {script_path}'
    )

    t0 = _time.time()
    try:
        proc = _sp.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=test_dir,
        )
        elapsed = _time.time() - t0
        success = proc.returncode == 0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
    except _sp.TimeoutExpired:
        elapsed = _time.time() - t0
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Yosys timeout after {timeout_seconds}s",
            "metrics": {},
            "elapsed_s": round(elapsed, 2),
            "result_files": [],
            "script_path": script_path,
            "log_file": "",
            "run_dir": run_dir,
            "remote": True,
            "host": _os.uname().nodename,
            "tool": "yosys",
        }

    # Persist logs
    log_file = _os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as lf:
        lf.write(full_stdout)
    if full_stderr:
        stderr_log = _os.path.join(run_dir, f"{run_label}.stderr.log")
        with open(stderr_log, "w") as lf:
            lf.write(full_stderr)

    # Truncate for JSON
    stdout = full_stdout[-8000:] if len(full_stdout) > 8000 else full_stdout
    stderr = full_stderr[-4000:] if len(full_stderr) > 4000 else full_stderr

    # Collect result files
    result_files: list[str] = []
    for d in [results_dir, reports_dir]:
        if _os.path.isdir(d):
            for fname in _os.listdir(d):
                result_files.append(_os.path.join(d, fname))

    # Parse metrics
    metrics: dict = {}
    m = _re.search(r'Number of cells:\s+(\d+)', full_stdout)
    if m:
        metrics["num_cells"] = int(m.group(1))
    m = _re.search(r'Number of wires:\s+(\d+)', full_stdout)
    if m:
        metrics["num_wires"] = int(m.group(1))
    for m_iter in _re.finditer(
        r'Chip area for (?:module|top module)\s+[^\s:]+\s*:\s*([\d.]+)',
        full_stdout,
    ):
        metrics["chip_area"] = float(m_iter.group(1))
    m = _re.search(r'Estimated number of transistors:\s+(\d+)', full_stdout)
    if m:
        metrics["estimated_transistors"] = int(m.group(1))

    return {
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "full_log": full_stdout,
        "metrics": metrics,
        "elapsed_s": round(elapsed, 2),
        "result_files": result_files,
        "script_path": script_path,
        "log_file": log_file,
        "run_dir": run_dir,
        "remote": True,
        "host": _os.uname().nodename,
        "tool": "yosys",
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
    _ensure_ray(ray_address)

    ref = _run_yosys_remote.remote(
        synth_script, run_label, timeout_seconds, source_files, session_name
    )

    local_timeout = timeout_seconds + 120
    try:
        result = ray.get(ref, timeout=local_timeout)
    except ray.exceptions.GetTimeoutError:
        ray.cancel(ref, force=True)
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray Yosys task did not complete within {local_timeout}s",
            "metrics": {},
            "elapsed_s": local_timeout,
            "result_files": [],
            "script_path": "",
            "log_file": "",
            "run_dir": "",
            "remote": True,
            "host": "",
            "tool": "yosys",
        }
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray Yosys error: {type(exc).__name__}: {exc}",
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

    return result


# ── Utility: fetch a remote log back to local ─────────────────────────

@ray.remote(num_cpus=0)
def _read_remote_file(path: str, tail_lines: int) -> str:
    """Read a file on the cluster and return its last N lines."""
    try:
        with open(path) as f:
            lines = f.readlines()
        return "".join(lines[-tail_lines:])
    except FileNotFoundError:
        return f"File not found on cluster: {path}"


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
    _ensure_ray(ray_address)
    ref = _read_remote_file.remote(log_path, tail_lines)
    try:
        return ray.get(ref, timeout=30)
    except Exception as exc:
        return f"Error reading remote log: {exc}"


# ── Remote ↔ Local sync utilities ─────────────────────────────────────

_REMOTE_SESSION_ROOT = "/mnt/shared/sessions"


@ray.remote(num_cpus=0)
def _list_remote_dir(dir_path: str, skip_dirs: list | None = None) -> list:
    """Recursively list files under *dir_path* on the remote cluster.

    Returns a list of relative paths (relative to *dir_path*).
    Skips sub-directories whose basenames are in *skip_dirs*.
    """
    import os as _os

    skip = set(skip_dirs or [])
    found: list[str] = []
    for root, dirs, files in _os.walk(dir_path):
        # In-place filter dirs to skip
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            abs_path = _os.path.join(root, fn)
            rel_path = _os.path.relpath(abs_path, dir_path)
            found.append(rel_path)
    return found


@ray.remote(num_cpus=0)
def _read_remote_file_bytes(path: str) -> bytes | None:
    """Read a file on the cluster and return its bytes."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


@ray.remote(num_cpus=0)
def _write_remote_file(path: str, content: bytes) -> bool:
    """Write bytes to a file on the remote cluster (for pushing metadata)."""
    import os as _os
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return True
    except Exception:
        return False


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
    _ensure_ray(ray_address)

    if skip_dirs is None:
        skip_dirs = ["upload", "src"]   # we already have these locally

    # 1. List remote files
    try:
        remote_files = ray.get(
            _list_remote_dir.remote(remote_run_dir, skip_dirs), timeout=30
        )
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

    # 3. Fetch missing files in parallel via Ray
    refs = {
        rel: _read_remote_file_bytes.remote(
            os.path.join(remote_run_dir, rel)
        )
        for rel in to_fetch
    }

    synced: list[str] = []
    for rel, ref in refs.items():
        try:
            data = ray.get(ref, timeout=60)
            if data is not None:
                local_path = os.path.join(local_run_dir, rel)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(data)
                synced.append(rel)
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", rel, exc)

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
    _ensure_ray(ray_address)
    with open(local_path, "rb") as f:
        data = f.read()
    try:
        return ray.get(_write_remote_file.remote(remote_path, data), timeout=30)
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


# ── KLayout remote task (DEF → GDS) ───────────────────────────────────

@ray.remote
def _run_klayout_remote(
    def_file: str,
    design_name: str,
    gds_files: list[str],
    klayout_tech_file: str,
    gds_layer_map: str,
    gds_allow_empty: str,
    run_label: str,
    timeout_seconds: int,
    session_name: str | None = None,
) -> dict:
    """Execute KLayout GDS merging on the remote cluster node.

    Uses the ORFS ``def2stream.py`` script to:
        1. Read the routed DEF via KLayout's LEF/DEF reader.
        2. Merge with GDS cell libraries.
        3. Write the final GDSII file.

    The DEF file is expected to already reside on the cluster filesystem
    (produced by a prior OpenROAD P&R run).
    """
    import json as _json
    import os as _os
    import subprocess as _sp
    import time as _time

    # env_script = "/home/ray/OpenROAD-flow-scripts/env.sh"
    def2stream = "/mnt/shared/def2stream.py"
    work_base = "/mnt/shared/openroad_work"
    session_base_root = "/mnt/shared/sessions"

    if session_name:
        session_base = _os.path.join(session_base_root, session_name)
        run_dir = _os.path.join(session_base, run_label)
    else:
        run_dir = _os.path.join(work_base, run_label)
    _os.makedirs(run_dir, exist_ok=True)

    results_dir = _os.path.join(run_dir, "results")
    _os.makedirs(results_dir, exist_ok=True)

    out_gds = _os.path.join(results_dir, f"{design_name}_final.gds")
    in_files_str = " ".join(gds_files)

    # Build the KLayout shell command
    #   source env.sh → sets PATH (though klayout may be system-installed)
    #   QT_QPA_PLATFORM=offscreen → headless operation
    #   klayout -zz → batch mode, no GUI
    #   -rd key=value → pass variables to the script
    #   -r def2stream.py → run the GDS merge script
    shell_cmd = (
        # f'source {env_script} && '
        f'export QT_QPA_PLATFORM=offscreen && '
    )
    if gds_allow_empty:
        shell_cmd += f'export GDS_ALLOW_EMPTY="{gds_allow_empty}" && '

    shell_cmd += (
        f'klayout -zz '
        f'-rd design_name={design_name} '
        f'-rd in_def={def_file} '
        f'-rd "in_files={in_files_str}" '
        f'-rd tech_file={klayout_tech_file} '
        f'-rd "layer_map={gds_layer_map}" '
        f'-rd seal_file= '
        f'-rd out_file={out_gds} '
        f'-r {def2stream}'
    )

    t0 = _time.time()
    try:
        proc = _sp.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        elapsed = _time.time() - t0
        success = proc.returncode == 0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
    except _sp.TimeoutExpired:
        elapsed = _time.time() - t0
        return {
            "success": False,
            "stdout": "",
            "stderr": f"KLayout timeout after {timeout_seconds}s",
            "metrics": {},
            "elapsed_s": round(elapsed, 2),
            "gds_file": "",
            "result_files": [],
            "run_dir": run_dir,
            "remote": True,
            "host": _os.uname().nodename,
            "tool": "klayout",
        }

    # Persist logs
    log_file = _os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as lf:
        lf.write(full_stdout)
    if full_stderr:
        stderr_log = _os.path.join(run_dir, f"{run_label}.stderr.log")
        with open(stderr_log, "w") as lf:
            lf.write(full_stderr)

    # Truncate for JSON
    stdout = full_stdout[-8000:] if len(full_stdout) > 8000 else full_stdout
    stderr = full_stderr[-4000:] if len(full_stderr) > 4000 else full_stderr

    # Collect result files
    result_files: list[str] = []
    if _os.path.isdir(results_dir):
        for fname in _os.listdir(results_dir):
            result_files.append(_os.path.join(results_dir, fname))

    # Check if GDS was actually produced
    gds_exists = _os.path.isfile(out_gds)
    gds_size = _os.path.getsize(out_gds) if gds_exists else 0

    # Parse KLayout messages for metrics
    metrics: dict = {}
    if gds_exists:
        metrics["gds_file_size_bytes"] = gds_size
        metrics["gds_file_size_mb"] = round(gds_size / (1024 * 1024), 2)

    # Count errors / warnings from def2stream.py output
    import re as _re
    errors = len(_re.findall(r'\[ERROR\]', full_stdout))
    warnings = len(_re.findall(r'\[WARNING\]', full_stdout))
    infos = len(_re.findall(r'\[INFO\]', full_stdout))
    metrics["klayout_errors"] = errors
    metrics["klayout_warnings"] = warnings
    metrics["klayout_info_msgs"] = infos

    # Check for "All LEF cells have matching GDS" message
    metrics["all_cells_matched"] = (
        "All LEF cells have matching GDS/OAS cells" in full_stdout
    )
    metrics["no_orphan_cells"] = (
        "No orphan cells in the final layout" in full_stdout
    )

    return {
        "success": success and gds_exists,
        "stdout": stdout,
        "stderr": stderr,
        "full_log": full_stdout,
        "metrics": metrics,
        "elapsed_s": round(elapsed, 2),
        "gds_file": out_gds if gds_exists else "",
        "result_files": result_files,
        "log_file": log_file,
        "run_dir": run_dir,
        "remote": True,
        "host": _os.uname().nodename,
        "tool": "klayout",
    }


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
    _ensure_ray(ray_address)

    ref = _run_klayout_remote.remote(
        def_file, design_name, gds_files, klayout_tech_file,
        gds_layer_map, gds_allow_empty, run_label, timeout_seconds,
        session_name,
    )

    local_timeout = timeout_seconds + 120
    try:
        result = ray.get(ref, timeout=local_timeout)
    except ray.exceptions.GetTimeoutError:
        ray.cancel(ref, force=True)
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray KLayout task did not complete within {local_timeout}s",
            "metrics": {},
            "elapsed_s": local_timeout,
            "gds_file": "",
            "result_files": [],
            "run_dir": "",
            "remote": True,
            "host": "",
            "tool": "klayout",
        }
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Ray KLayout error: {type(exc).__name__}: {exc}",
            "metrics": {},
            "elapsed_s": 0,
            "gds_file": "",
            "result_files": [],
            "run_dir": "",
            "remote": True,
            "host": "",
            "tool": "klayout",
        }

    return result
