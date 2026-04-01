"""File and directory utilities."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def ensure_dir(path: str) -> str:
    """Create directory if it doesn't exist, return the path."""
    os.makedirs(path, exist_ok=True)
    return path


def safe_write(path: str, content: str) -> str:
    """Write content to file, creating parent dirs as needed."""
    ensure_dir(os.path.dirname(path))
    with open(path, "w") as f:
        f.write(content)
    return path


def find_files(directory: str, pattern: str = "*.tcl") -> list[str]:
    """Find files matching a glob pattern in a directory tree."""
    return [str(p) for p in Path(directory).rglob(pattern)]


def tail_file(path: str, n: int = 100) -> str:
    """Return the last n lines of a file."""
    try:
        with open(path) as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return f"File not found: {path}"


def cleanup_work_dir(work_dir: str, keep_latest: int = 5) -> None:
    """Remove old run directories, keeping the latest N."""
    if not os.path.isdir(work_dir):
        return
    dirs = sorted(
        [d for d in os.listdir(work_dir)
         if os.path.isdir(os.path.join(work_dir, d))],
        key=lambda d: os.path.getmtime(os.path.join(work_dir, d)),
        reverse=True,
    )
    for d in dirs[keep_latest:]:
        shutil.rmtree(os.path.join(work_dir, d), ignore_errors=True)
