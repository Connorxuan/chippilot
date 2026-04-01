"""Tool: manage design files — save, list, and read user-provided content.

Provides the LLM agent with the ability to save user-provided RTL Verilog,
SDC constraints, or other design files to the local work directory so they
can be used by Yosys synthesis and OpenROAD physical design flows.

Files are stored under ``<work_dir>/designs/<design_name>/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_core.tools import tool

from openroad_agent.config import OpenROADConfig
from openroad_agent.tools.session_manager import SessionManager


def _get_cfg() -> OpenROADConfig:
    global _cfg_instance
    if _cfg_instance is None:
        _cfg_instance = OpenROADConfig()
    return _cfg_instance


_cfg_instance: OpenROADConfig | None = None


def _designs_root() -> str:
    """Return the root directory for user designs.

    If a session is active, returns the session's ``designs/`` subdir.
    Otherwise falls back to ``<work_dir>/designs/``.
    """
    session = SessionManager.get_current()
    if session:
        return session.designs_dir
    cfg = _get_cfg()
    d = os.path.join(cfg.work_dir, "designs")
    os.makedirs(d, exist_ok=True)
    return d


@tool
def save_design_files(
    design_name: str,
    files: dict[str, str],
) -> str:
    """Save one or more design files (Verilog, SDC, etc.) to the work directory.

    Use this tool when the user provides RTL Verilog code, SDC constraints,
    or any other design file content in the conversation.  The files are
    written to ``<work_dir>/designs/<design_name>/`` and their absolute paths
    are returned so they can be passed to Yosys or OpenROAD tools.

    Args:
        design_name: A short name for the design (e.g. "counter", "alu").
                     Used as the sub-directory name.
        files: A dict mapping filename → file content.
               Example: {"counter.v": "module counter ...", "counter.sdc": "create_clock ..."}

    Returns:
        JSON with "design_dir" and "saved_files" (list of absolute paths).
    """
    root = _designs_root()
    design_dir = os.path.join(root, design_name)
    os.makedirs(design_dir, exist_ok=True)

    saved: list[str] = []
    for filename, content in files.items():
        fp = os.path.join(design_dir, filename)
        with open(fp, "w") as f:
            f.write(content)
        saved.append(os.path.abspath(fp))

    return json.dumps({
        "design_dir": os.path.abspath(design_dir),
        "saved_files": saved,
    })


@tool
def list_design_files(design_name: str = "") -> str:
    """List saved design files.

    If *design_name* is given, list files in that design's directory.
    Otherwise, list all designs and their files.

    Args:
        design_name: Optional design name to filter by.

    Returns:
        JSON listing designs and their files.
    """
    root = _designs_root()

    if design_name:
        design_dir = os.path.join(root, design_name)
        if not os.path.isdir(design_dir):
            return json.dumps({"error": f"Design '{design_name}' not found."})
        files = []
        for f in sorted(os.listdir(design_dir)):
            fp = os.path.join(design_dir, f)
            if os.path.isfile(fp):
                files.append({
                    "name": f,
                    "path": os.path.abspath(fp),
                    "size_bytes": os.path.getsize(fp),
                })
        return json.dumps({"design": design_name, "files": files})

    # List all designs
    designs = {}
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            dp = os.path.join(root, d)
            if os.path.isdir(dp):
                files = [f for f in os.listdir(dp) if os.path.isfile(os.path.join(dp, f))]
                designs[d] = {
                    "path": os.path.abspath(dp),
                    "files": sorted(files),
                }
    return json.dumps(designs)


@tool
def read_design_file(file_path: str) -> str:
    """Read the content of a design file.

    Args:
        file_path: Absolute path to the file, or relative to work_dir/designs/.

    Returns:
        The file content as a string.
    """
    if not os.path.isabs(file_path):
        root = _designs_root()
        file_path = os.path.join(root, file_path)

    try:
        with open(file_path, "r") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"
