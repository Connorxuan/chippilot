"""Tool: run KLayout for DEF → GDS conversion and capture output.

KLayout merges a routed DEF file with GDS cell libraries to produce the
final GDSII layout.  This tool uses KLayout's ``def2stream.py`` helper
from the OpenROAD-flow-scripts (ORFS) installation.

Supports two execution modes (matching openroad_runner / yosys_runner):
  - "local":  subprocess on this machine (requires klayout binary)
  - "ray":    submit to a remote Ray-on-K8s cluster

The mode is controlled by ``OPENROAD_EXEC_MODE`` env var.
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

# ── lazy config ────────────────────────────────────────────────────────
_cfg_instance: OpenROADConfig | None = None


def _get_cfg() -> OpenROADConfig:
    global _cfg_instance
    if _cfg_instance is None:
        _cfg_instance = OpenROADConfig()
    return _cfg_instance


# ── Platform-specific KLayout / GDS parameters ────────────────────────
# Paths are relative to the ORFS root directory on the cluster
# (/home/ray/OpenROAD-flow-scripts/).

# _ORFS_ROOT = "/home/ray/OpenROAD-flow-scripts"
_PLATFORMS_DIR = f"/mnt/shared/platforms"
_DEF2STREAM_SCRIPT = f"/mnt/shared/def2stream.py"

_PLATFORM_GDS_PARAMS: dict[str, dict] = {
    "nangate45": {
        "klayout_tech_file": f"{_PLATFORMS_DIR}/nangate45/FreePDK45.lyt",
        "gds_files": [
            f"{_PLATFORMS_DIR}/nangate45/gds/NangateOpenCellLibrary.gds",
        ],
        "gds_layer_map": "",          # embedded in .lyt
        "gds_allow_empty": "fakeram.*",
    },
    "sky130hd": {
        "klayout_tech_file": f"{_PLATFORMS_DIR}/sky130hd/sky130hd.lyt",
        "gds_files": [
            f"{_PLATFORMS_DIR}/sky130hd/gds/sky130_fd_sc_hd.gds",
        ],
        "gds_layer_map": "",
        "gds_allow_empty": "",
    },
    "sky130hs": {
        "klayout_tech_file": f"{_PLATFORMS_DIR}/sky130hs/sky130hs.lyt",
        "gds_files": [
            f"{_PLATFORMS_DIR}/sky130hs/gds/sky130_fd_sc_hs.gds",
        ],
        "gds_layer_map": "",
        "gds_allow_empty": "",
    },
    "asap7": {
        "klayout_tech_file": f"{_PLATFORMS_DIR}/asap7/KLayout/asap7.lyt",
        "gds_files": [
            f"{_PLATFORMS_DIR}/asap7/gds/asap7sc7p5t_28_R_220121a.gds",
            f"{_PLATFORMS_DIR}/asap7/gds/asap7sc7p5t_28_L_220121a.gds",
            f"{_PLATFORMS_DIR}/asap7/gds/asap7sc7p5t_28_SL_220121a.gds",
            f"{_PLATFORMS_DIR}/asap7/gds/asap7sc7p5t_28_SRAM_220121a.gds",
        ],
        "gds_layer_map": "",
        "gds_allow_empty": "",
    },
}


# ═══════════════════════════════════════════════════════════════════════
# LangChain Tools
# ═══════════════════════════════════════════════════════════════════════

@tool
def run_klayout_gds(
    def_file: str,
    design_name: str,
    platform: str,
    run_label: str = "gds",
    timeout_seconds: int = 600,
    extra_gds_files: list[str] | None = None,
) -> str:
    """Generate a GDSII layout from a routed DEF file using KLayout.

    This is the final step in the RTL-to-GDS flow.  It takes the routed
    DEF file produced by OpenROAD and merges it with the platform's GDS
    cell library to produce a complete GDSII layout file.

    KLayout must be available on the execution target.  On the Ray cluster
    it is already installed.

    Args:
        def_file: Path to the input DEF file (final routed DEF from
            OpenROAD).  Can be a remote path when running on Ray
            (e.g. ``/mnt/shared/sessions/<session>/<run>/results/<design>.def``),
            or a local absolute path.
        design_name: Top-level cell / design name (must match the DEF).
        platform: Target PDK platform (nangate45, sky130hd, sky130hs, asap7).
        run_label: Short label for file naming (default "gds").
        timeout_seconds: Max wall-clock seconds (default 600).
        extra_gds_files: Additional GDS files to merge (macro libraries, etc.).

    Returns:
        JSON with keys: success, stdout, stderr, gds_file (remote path),
        gds_file_local, elapsed_s, run_dir, remote, tool ("klayout").
    """
    cfg = _get_cfg()

    if platform not in _PLATFORM_GDS_PARAMS:
        return json.dumps({
            "success": False,
            "error": (
                f"Unsupported platform '{platform}' for GDS generation. "
                f"Supported: {list(_PLATFORM_GDS_PARAMS)}"
            ),
        })

    if cfg.execution_mode == "ray":
        return _run_klayout_via_ray(
            cfg, def_file, design_name, platform,
            run_label, timeout_seconds, extra_gds_files,
        )
    return _run_klayout_locally(
        cfg, def_file, design_name, platform,
        run_label, timeout_seconds, extra_gds_files,
    )


@tool
def list_gds_platforms() -> str:
    """List platforms supported for KLayout GDS generation.

    Returns:
        JSON with platform names and their GDS cell library info.
    """
    info = {}
    for plat, params in _PLATFORM_GDS_PARAMS.items():
        info[plat] = {
            "klayout_tech_file": params["klayout_tech_file"],
            "gds_files": [os.path.basename(f) for f in params["gds_files"]],
        }
    return json.dumps(info, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# Internal execution backends
# ═══════════════════════════════════════════════════════════════════════

def _run_klayout_locally(
    cfg: OpenROADConfig,
    def_file: str,
    design_name: str,
    platform: str,
    run_label: str,
    timeout_seconds: int,
    extra_gds_files: list[str] | None,
) -> str:
    """Run KLayout GDS generation on the local machine."""
    klayout_bin = cfg.klayout_bin

    # Check if klayout is available
    import shutil
    if not shutil.which(klayout_bin):
        result = {
            "success": False,
            "error": (
                f"KLayout binary '{klayout_bin}' not found locally. "
                "GDS generation is only available on the Ray cluster "
                "(set OPENROAD_EXEC_MODE=ray)."
            ),
        }
        register_current_run(run_label, "klayout", result)
        return json.dumps(result)

    params = _PLATFORM_GDS_PARAMS[platform]

    run_dir = resolve_run_dir(cfg.work_dir, run_label)
    results_dir = os.path.join(run_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    out_gds = os.path.join(results_dir, f"{design_name}_final.gds")

    # Build GDS file list
    gds_files_list = list(params["gds_files"])
    if extra_gds_files:
        gds_files_list.extend(extra_gds_files)
    in_files_str = " ".join(gds_files_list)

    # Build KLayout command
    cmd = [
        klayout_bin, "-zz",
        "-rd", f"design_name={design_name}",
        "-rd", f"in_def={def_file}",
        "-rd", f"in_files={in_files_str}",
        "-rd", f"tech_file={params['klayout_tech_file']}",
        "-rd", f"layer_map={params.get('gds_layer_map', '')}",
        "-rd", "seal_file=",
        "-rd", f"out_file={out_gds}",
        "-r", _DEF2STREAM_SCRIPT,
    ]

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    if params.get("gds_allow_empty"):
        env["GDS_ALLOW_EMPTY"] = params["gds_allow_empty"]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        elapsed = time.time() - t0
        success = proc.returncode == 0
        full_stdout = proc.stdout
        full_stderr = proc.stderr
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        result = {
            "success": False,
            "stdout": "",
            "stderr": f"KLayout timeout after {timeout_seconds}s",
            "elapsed_s": round(elapsed, 2),
            "run_dir": os.path.abspath(run_dir),
            "remote": False,
            "tool": "klayout",
        }
        register_current_run(run_label, "klayout", result)
        return json.dumps(result)

    # Persist logs
    log_file = os.path.join(run_dir, f"{run_label}.log")
    with open(log_file, "w") as lf:
        lf.write(full_stdout)
    if full_stderr:
        stderr_log = os.path.join(run_dir, f"{run_label}.stderr.log")
        with open(stderr_log, "w") as lf:
            lf.write(full_stderr)

    stdout = full_stdout[-8000:] if len(full_stdout) > 8000 else full_stdout
    stderr = full_stderr[-4000:] if len(full_stderr) > 4000 else full_stderr

    gds_exists = os.path.isfile(out_gds)

    result = {
        "success": success and gds_exists,
        "stdout": stdout,
        "stderr": stderr,
        "gds_file": out_gds if gds_exists else "",
        "elapsed_s": round(elapsed, 2),
        "log_file": log_file,
        "run_dir": os.path.abspath(run_dir),
        "remote": False,
        "tool": "klayout",
    }
    register_current_run(run_label, "klayout", result)
    return json.dumps(result)


def _run_klayout_via_ray(
    cfg: OpenROADConfig,
    def_file: str,
    design_name: str,
    platform: str,
    run_label: str,
    timeout_seconds: int,
    extra_gds_files: list[str] | None,
) -> str:
    """Submit KLayout GDS generation to the Ray cluster."""
    from openroad_agent.tools.ray_executor import run_klayout_on_ray

    params = _PLATFORM_GDS_PARAMS[platform]

    # Build GDS file list
    gds_files_list = list(params["gds_files"])
    if extra_gds_files:
        gds_files_list.extend(extra_gds_files)

    result = run_klayout_on_ray(
        def_file=def_file,
        design_name=design_name,
        gds_files=gds_files_list,
        klayout_tech_file=params["klayout_tech_file"],
        gds_layer_map=params.get("gds_layer_map", ""),
        gds_allow_empty=params.get("gds_allow_empty", ""),
        run_label=run_label,
        timeout_seconds=timeout_seconds,
        ray_address=cfg.ray_address,
        session_name=get_session_name(),
    )

    # Save local copies
    local_run_dir = resolve_run_dir(cfg.work_dir, run_label)

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
            skip_dirs=["upload", "src"],
        )
        if synced:
            result["synced_files"] = synced

    # Expose local GDS path
    remote_gds = result.get("gds_file", "")
    if remote_gds and remote_run_dir:
        local_gds = os.path.join(
            local_run_dir, os.path.relpath(remote_gds, remote_run_dir)
        )
        if os.path.isfile(local_gds):
            result["gds_file_local"] = local_gds

    session = SessionManager.get_current()
    if session:
        register_current_run(run_label, "klayout", result)
        push_session_meta_to_remote(
            session.base_dir, session.session_name, cfg.ray_address,
        )

    return json.dumps(result)
