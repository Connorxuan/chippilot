"""Tool: run OpenROAD with a TCL script and capture output/metrics.

Supports two execution modes:
  - "local":  subprocess on this machine (original behaviour)
  - "ray":    submit to a remote Ray-on-K8s cluster via ray_executor

The mode is controlled by ``OPENROAD_EXEC_MODE`` env var or
``OpenROADConfig.execution_mode``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from langchain_core.tools import tool

from openroad_agent.config import OpenROADConfig
from openroad_agent.tools.session_manager import (
    get_session_name,
    register_current_run,
    resolve_run_dir,
)


def _get_cfg() -> OpenROADConfig:
    """Lazy config accessor — ensures env is loaded before first use."""
    global _cfg_instance
    if _cfg_instance is None:
        _cfg_instance = OpenROADConfig()
    return _cfg_instance


_cfg_instance: OpenROADConfig | None = None


@tool
def run_openroad_tcl(
    tcl_script: str,
    run_label: str = "run",
    timeout_seconds: int = 3600,
) -> str:
    """Execute a TCL script with OpenROAD and return stdout + parsed metrics.

    Execution can happen locally (subprocess) or on a remote Ray cluster,
    controlled by the ``OPENROAD_EXEC_MODE`` setting ("local" or "ray").

    Args:
        tcl_script: Full TCL script content to execute.
        run_label: A short label for this run (used for file naming).
        timeout_seconds: Max wall-clock seconds before killing OpenROAD.

    Returns:
        JSON string with keys: success, stdout, stderr, metrics, elapsed_s,
        result_files, run_dir, remote (bool), host.
    """
    cfg = _get_cfg()

    # LLMs (especially Gemini) sometimes pass multi-line content with
    # literal two-char '\n' instead of real newlines.  Detect and fix.
    if '\\n' in tcl_script:
        tcl_script = tcl_script.replace('\\n', '\n')
    if '\\t' in tcl_script:
        tcl_script = tcl_script.replace('\\t', '\t')

    # ── Dispatch to the correct backend ────────────────────────────────
    if cfg.execution_mode == "ray":
        return _run_via_ray(cfg, tcl_script, run_label, timeout_seconds)
    return _run_locally(cfg, tcl_script, run_label, timeout_seconds)


def _run_via_ray(
    cfg: OpenROADConfig,
    tcl_script: str,
    run_label: str,
    timeout_seconds: int,
) -> str:
    """Submit the TCL script to the remote Ray cluster.

    Automatically detects local files referenced in the TCL script
    (synth_verilog, sdc_file, other read_verilog/read_sdc paths)
    and uploads them to the remote cluster.
    """
    from openroad_agent.tools.ray_executor import run_openroad_on_ray

    # ── Collect local files referenced in the script ──────────────────
    # Files that the remote cluster won't have:
    #   - ``set synth_verilog "..."``  → the gate-level netlist
    #   - ``set sdc_file "..."``       → timing constraints
    #   - ``read_verilog <path>``      → direct Verilog read commands
    #   - ``read_sdc <path>``          → direct SDC read commands
    #   - ``source <path>.sdc``        → sourced SDC
    source_files: dict[str, str] = {}  # {basename: content}

    def _try_collect(path: str) -> str | None:
        """If *path* is a local file not in test_dir, collect and return basename."""
        if not path or not os.path.isabs(path):
            return None
        # Don't upload platform files that already exist on remote
        # (they live under the remote test_dir with the same relative structure)
        try:
            rel = os.path.relpath(path, cfg.test_dir)
            if not rel.startswith(".."):
                # File is inside test_dir — the remote has it too
                return None
        except ValueError:
            pass
        if os.path.isfile(path):
            basename = os.path.basename(path)
            with open(path, "r") as f:
                source_files[basename] = f.read()
            return basename
        return None

    # Scan the TCL script line by line
    new_lines: list[str] = []
    for line in tcl_script.split("\n"):
        stripped = line.strip()

        # Match: set synth_verilog "some_path"
        m = re.match(r'^(set\s+synth_verilog\s+)"([^"]+)"', stripped)
        if m:
            bn = _try_collect(m.group(2))
            if bn:
                new_lines.append(f'{m.group(1)}"__UPLOAD_DIR__/{bn}"')
                continue

        # Match: set sdc_file "some_path"
        m = re.match(r'^(set\s+sdc_file\s+)"([^"]+)"', stripped)
        if m:
            bn = _try_collect(m.group(2))
            if bn:
                new_lines.append(f'{m.group(1)}"__UPLOAD_DIR__/{bn}"')
                continue

        # Match: read_verilog [-sv] <absolute_path>
        m = re.match(r'^(read_verilog\s+(?:-sv\s+)?)(\S+)', stripped)
        if m and os.path.isabs(m.group(2)):
            bn = _try_collect(m.group(2))
            if bn:
                new_lines.append(f'{m.group(1)}__UPLOAD_DIR__/{bn}')
                continue

        # Match: read_sdc <absolute_path>
        m = re.match(r'^(read_sdc\s+)(\S+)', stripped)
        if m and os.path.isabs(m.group(2)):
            bn = _try_collect(m.group(2))
            if bn:
                new_lines.append(f'{m.group(1)}__UPLOAD_DIR__/{bn}')
                continue

        new_lines.append(line)

    tcl_script = "\n".join(new_lines)

    result = run_openroad_on_ray(
        tcl_script=tcl_script,
        run_label=run_label,
        timeout_seconds=timeout_seconds,
        ray_address=cfg.ray_address,
        source_files=source_files if source_files else None,
        session_name=get_session_name(),
    )

    # Also save a local copy of the TCL + full log for reference
    local_run_dir = resolve_run_dir(cfg.work_dir, run_label)
    local_tcl = os.path.join(local_run_dir, f"{run_label}.tcl")
    with open(local_tcl, "w") as f:
        f.write(tcl_script)
    # Prefer full_log (complete stdout) over truncated stdout
    log_content = result.pop("full_log", None) or result.get("stdout", "")
    if log_content:
        local_log = os.path.join(local_run_dir, f"{run_label}.log")
        with open(local_log, "w") as f:
            f.write(log_content)
        result["local_log"] = local_log
    result["local_run_dir"] = os.path.abspath(local_run_dir)

    # ── Sync remote results → local & push session meta → remote ──────
    from openroad_agent.tools.ray_executor import (
        sync_remote_run_to_local,
        push_session_meta_to_remote,
    )
    from openroad_agent.tools.session_manager import SessionManager

    remote_run_dir = result.get("run_dir", "")
    if remote_run_dir:
        synced = sync_remote_run_to_local(
            remote_run_dir, local_run_dir, cfg.ray_address,
            skip_dirs=["upload"],
        )
        if synced:
            result["synced_files"] = synced

    session = SessionManager.get_current()
    if session:
        register_current_run(run_label, "openroad", result)
        push_session_meta_to_remote(
            session.base_dir, session.session_name, cfg.ray_address,
        )

    return json.dumps(result)


def _run_locally(
    cfg: OpenROADConfig,
    tcl_script: str,
    run_label: str,
    timeout_seconds: int,
) -> str:
    """Execute OpenROAD as a local subprocess (original behaviour)."""
    work = cfg.test_dir
    run_dir = resolve_run_dir(cfg.work_dir, run_label)

    tcl_path = os.path.abspath(os.path.join(run_dir, f"{run_label}.tcl"))
    with open(tcl_path, "w") as f:
        f.write(tcl_script)

    env = os.environ.copy()
    results_dir = os.path.abspath(os.path.join(run_dir, "results"))
    env["RESULTS_DIR"] = results_dir
    os.makedirs(results_dir, exist_ok=True)

    cmd = [cfg.openroad_bin, "-no_init", "-exit", tcl_path]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=work,
            env=env,
        )
        elapsed = time.time() - t0
        success = proc.returncode == 0
        full_stdout = proc.stdout
        full_stderr = proc.stderr

        # ── Persist full logs to disk ──────────────────────────────────
        stdout_log = os.path.join(run_dir, f"{run_label}.log")
        with open(stdout_log, "w") as lf:
            lf.write(full_stdout)
        if full_stderr:
            stderr_log = os.path.join(run_dir, f"{run_label}.stderr.log")
            with open(stderr_log, "w") as lf:
                lf.write(full_stderr)

        # Truncate for JSON response (keeps payload manageable for LLM)
        stdout = full_stdout[-8000:] if len(full_stdout) > 8000 else full_stdout
        stderr = full_stderr[-4000:] if len(full_stderr) > 4000 else full_stderr
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        result = {
            "success": False,
            "stdout": "",
            "stderr": f"Timeout after {timeout_seconds}s",
            "metrics": {},
            "elapsed_s": elapsed,
            "result_files": [],
            "log_file": "",
            "run_dir": os.path.abspath(run_dir),
            "remote": False,
        }
        register_current_run(run_label, "openroad", result)
        return json.dumps(result)

    # Collect result files
    result_files = []
    if os.path.isdir(results_dir):
        for f in os.listdir(results_dir):
            result_files.append(os.path.join(results_dir, f))

    # Parse metrics from stdout (OpenROAD utl::metric lines)
    metrics = _parse_metrics_from_output(stdout)

    # Also try to load metrics JSON if produced
    for rf in result_files:
        if rf.endswith(".metrics") or rf.endswith(".json"):
            try:
                with open(rf) as mf:
                    file_metrics = json.load(mf)
                    metrics.update(file_metrics)
            except Exception:
                pass

    result = {
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "metrics": metrics,
        "elapsed_s": round(elapsed, 2),
        "result_files": result_files,
        "tcl_path": tcl_path,
        "log_file": stdout_log,
        "run_dir": os.path.abspath(run_dir),
        "remote": False,
    }
    register_current_run(run_label, "openroad", result)
    return json.dumps(result)


@tool
def run_openroad_command(command: str) -> str:
    """Run a single OpenROAD command (wraps it in a minimal TCL script).

    Args:
        command: A single OpenROAD TCL command string.

    Returns:
        JSON with success, stdout, stderr.
    """
    tcl = f"""# Single command execution
{command}
exit
"""
    return run_openroad_tcl.invoke({
        "tcl_script": tcl,
        "run_label": f"cmd_{int(time.time())}",
    })


@tool
def read_openroad_log(log_path: str, tail_lines: int = 200) -> str:
    """Read the last N lines of an OpenROAD log or result file.

    Works for both local files and remote files on the Ray cluster.
    Remote paths (starting with /mnt/shared/) are fetched via Ray.

    Args:
        log_path: Path to the log file (relative to work_dir, or absolute).
        tail_lines: Number of lines from the end to return.

    Returns:
        The tail of the log file content.
    """
    cfg = _get_cfg()

    if not os.path.isabs(log_path):
        log_path = os.path.join(cfg.work_dir, log_path)

    # If it's a remote cluster path and we're in ray mode, fetch via Ray
    if log_path.startswith("/mnt/shared/") and cfg.execution_mode == "ray":
        from openroad_agent.tools.ray_executor import read_remote_log
        return read_remote_log(log_path, tail_lines, cfg.ray_address)

    # Local file
    try:
        with open(log_path) as f:
            lines = f.readlines()
        return "".join(lines[-tail_lines:])
    except FileNotFoundError:
        return f"File not found: {log_path}"


def _parse_metrics_from_output(stdout: str) -> dict:
    """Extract utl::metric key/value pairs from OpenROAD stdout."""
    metrics: dict = {}
    for line in stdout.splitlines():
        # OpenROAD prints metrics as: [INFO UTC-0001] metric_key : value
        if "metric" in line.lower() and ":" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                key = parts[0].strip().split()[-1] if parts[0].strip() else ""
                val = parts[1].strip()
                if key:
                    try:
                        metrics[key] = json.loads(val)
                    except (json.JSONDecodeError, ValueError):
                        metrics[key] = val
    return metrics
