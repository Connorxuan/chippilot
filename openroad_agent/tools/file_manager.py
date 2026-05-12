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
import re
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


_FILE_READ_HARD_LIMIT = 500_000


def _resolve_file_path(file_path: str) -> str:
    """Resolve a file path relative to the designs root."""
    if os.path.isabs(file_path):
        return file_path
    root = _designs_root()
    candidate = os.path.join(root, file_path)
    if os.path.exists(candidate):
        return candidate
    matches = list(Path(root).rglob(Path(file_path).name))
    if len(matches) == 1:
        return str(matches[0])
    return candidate


def _safe_read(path: str, limit: int = 200_000) -> str:
    """Read a text file with a hard size cap.

    Args:
        path: File path.
        limit: Max characters to read.

    Returns:
        File content, truncated if necessary with a trailing note.
    """
    cap = min(limit, _FILE_READ_HARD_LIMIT)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(cap + 1)
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except Exception as e:
        return f"Error reading file: {e}"

    if len(content) > cap:
        content = content[:cap] + (
            f"\n\n[File truncated after {cap} characters. "
            "Use read_file_chunk or search_in_file to read a specific section.]"
        )
    return content


def _count_lines(path: str) -> int:
    """Count lines in a text file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _large_file_guidance(file_path: str, size: int, max_chars: int) -> str:
    """Return a guidance message when a file is too large to read at once."""
    lines = _count_lines(file_path)
    lines_info = f" (~{lines} lines)" if lines else ""
    return (
        f"File '{os.path.basename(file_path)}' is {size:,} characters{lines_info}, "
        f"larger than the direct-read limit of {max_chars:,} characters.\n\n"
        "To read this file, use one of these approaches:\n"
        "1. read_file_chunk(file_path, line_start=1, line_end=500) — read a line range\n"
        "2. search_in_file(file_path, pattern='keyword', context_lines=10) — search with regex\n"
        "3. read_openroad_log(file_path, tail_lines=200) — if it's a log, read the tail\n"
    )


@tool
def read_design_file(file_path: str, max_chars: int = 200_000) -> str:
    """Read the content of a design file.

    If the file is larger than *max_chars*, a guidance message is returned
    instead of the raw content.  Use ``read_file_chunk`` or
    ``search_in_file`` to access large files in smaller pieces.

    Args:
        file_path: Absolute path to the file, or relative to work_dir/designs/.
        max_chars: Maximum characters to return directly.  Files larger than
                   this trigger the guidance response (hard limit 500k).

    Returns:
        The file content, or a guidance message telling you how to read it
        in chunks.
    """
    file_path = _resolve_file_path(file_path)

    try:
        size = os.path.getsize(file_path)
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    if size > max_chars:
        return _large_file_guidance(file_path, size, max_chars)

    return _safe_read(file_path, max_chars)


@tool
def read_file_chunk(
    file_path: str,
    line_start: int = 1,
    line_end: int = 500,
    max_chars: int = 200_000,
) -> str:
    """Read a specific line range from a text file.

    Use this for large design files, logs, or reports that cannot be
    read in one shot with ``read_design_file``.

    Args:
        file_path: Absolute path, or relative to work_dir/designs/.
        line_start: First line to read (1-indexed).
        line_end: Last line to read (inclusive).
        max_chars: Hard cap on characters returned (default 200k).

    Returns:
        The requested chunk, with a note if truncated.
    """
    file_path = _resolve_file_path(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    total_lines = len(lines)
    if line_start < 1:
        line_start = 1
    if line_end > total_lines:
        line_end = total_lines

    chunk = "".join(lines[line_start - 1 : line_end])
    cap = min(max_chars, _FILE_READ_HARD_LIMIT)
    if len(chunk) > cap:
        chunk = chunk[:cap] + (
            f"\n\n[Chunk truncated after {cap} characters. "
            "Narrow the line range or use search_in_file.]"
        )

    header = f"--- Lines {line_start}-{line_end} of {total_lines} ---\n"
    return header + chunk


@tool
def search_in_file(
    file_path: str,
    pattern: str,
    context_lines: int = 10,
    max_matches: int = 20,
) -> str:
    """Search inside a text file with a regex pattern.

    Returns each matching line together with *context_lines* of context
    before and after the match.  Ideal for locating keywords in large
    RTL files, TCL scripts, or log files without reading them whole.

    Args:
        file_path: Absolute path, or relative to work_dir/designs/.
        pattern: Regex pattern to search for (Python re syntax).
        context_lines: Lines to show before/after each match.
        max_matches: Maximum number of matches to return.

    Returns:
        Matching sections, or a message if no matches were found.
    """
    file_path = _resolve_file_path(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    total_lines = len(lines)
    compiled: re.Pattern | None = None
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    matches: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        if compiled.search(line):
            matches.append((idx, line.rstrip("\n")))
        if len(matches) >= max_matches:
            break

    if not matches:
        return (
            f"No matches for pattern '{pattern}' in {os.path.basename(file_path)} "
            f"({total_lines} lines)."
        )

    sections: list[str] = []
    for lineno, matched_line in matches:
        start = max(1, lineno - context_lines)
        end = min(total_lines, lineno + context_lines)
        section_lines = lines[start - 1 : end]
        # Mark the matched line
        marked = []
        for i, sl in enumerate(section_lines, start=start):
            prefix = ">>> " if i == lineno else "    "
            marked.append(f"{prefix}{i:4d}: {sl.rstrip(chr(10))}")
        sections.append("\n".join(marked))

    header = (
        f"Found {len(matches)} match(es) for '{pattern}' "
        f"in {os.path.basename(file_path)} ({total_lines} lines):\n"
    )
    return header + "\n\n---\n\n".join(sections)
