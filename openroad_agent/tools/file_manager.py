"""Tool: manage design files — save, list, and read user-provided content.

Provides the LLM agent with the ability to save user-provided RTL Verilog,
SDC constraints, or other design files to the local work directory so they
can be used by Yosys synthesis and OpenROAD physical design flows.

Files may be stored either under ``<work_dir>/designs/<design_name>/`` or
directly in ``<work_dir>/designs/`` when uploaded through the web UI.
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


def _file_entry(root: str, fp: str) -> dict[str, str | int]:
    """Return a JSON-serialisable file description."""
    return {
        "name": os.path.basename(fp),
        "relative_path": os.path.relpath(fp, root),
        "path": os.path.abspath(fp),
        "size_bytes": os.path.getsize(fp),
    }


def _root_files(root: str) -> list[dict[str, str | int]]:
    """List files stored directly in the designs root."""
    files: list[dict[str, str | int]] = []
    if not os.path.isdir(root):
        return files
    for entry in sorted(os.listdir(root)):
        fp = os.path.join(root, entry)
        if os.path.isfile(fp):
            files.append(_file_entry(root, fp))
    return files


def _design_subdirs(root: str) -> dict[str, dict[str, str | list[dict[str, str | int]]]]:
    """List files grouped by design sub-directory."""
    designs: dict[str, dict[str, str | list[dict[str, str | int]]]] = {}
    if not os.path.isdir(root):
        return designs
    for design_name in sorted(os.listdir(root)):
        design_dir = os.path.join(root, design_name)
        if not os.path.isdir(design_dir):
            continue
        files: list[dict[str, str | int]] = []
        for base, _dirs, filenames in os.walk(design_dir):
            for filename in sorted(filenames):
                fp = os.path.join(base, filename)
                files.append(_file_entry(root, fp))
        designs[design_name] = {
            "path": os.path.abspath(design_dir),
            "files": files,
        }
    return designs


def _matching_root_files(root: str, design_name: str) -> list[dict[str, str | int]]:
    """Find files in the root that likely belong to the given design."""
    matches: list[dict[str, str | int]] = []
    for entry in _root_files(root):
        rel = str(entry["relative_path"])
        stem = Path(rel).stem
        if stem == design_name or stem.startswith(f"{design_name}_") or rel.startswith(f"{design_name}."):
            matches.append(entry)
    return matches


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
        subdir_files: list[dict[str, str | int]] = []
        if os.path.isdir(design_dir):
            for base, _dirs, filenames in os.walk(design_dir):
                for filename in sorted(filenames):
                    subdir_files.append(_file_entry(root, os.path.join(base, filename)))

        root_matches = _matching_root_files(root, design_name)
        files = subdir_files + [f for f in root_matches if f not in subdir_files]
        if not files:
            return json.dumps({
                "error": f"Design '{design_name}' not found.",
                "available_root_files": _root_files(root),
                "available_designs": sorted(_design_subdirs(root).keys()),
            })

        return json.dumps({
            "design": design_name,
            "files": files,
        }, indent=2)

    return json.dumps({
        "root_files": _root_files(root),
        "design_directories": _design_subdirs(root),
    }, indent=2)


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
        candidate = os.path.join(root, file_path)
        if os.path.exists(candidate):
            file_path = candidate
        else:
            matches = list(Path(root).rglob(Path(file_path).name))
            if len(matches) == 1:
                file_path = str(matches[0])
            else:
                file_path = candidate

    try:
        with open(file_path, "r") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"
