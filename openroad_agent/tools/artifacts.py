"""Helpers for listing and packaging session artifacts."""

from __future__ import annotations

import os
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any


_SKIP_FILENAMES = {"session.json", "chat_log.jsonl"}
_INPUT_DIRS = {"designs", "attachments"}


def classify_artifact(relative_path: str) -> str:
    """Return a small user-facing category for a session file."""
    parts = Path(relative_path).parts
    name = parts[-1].lower() if parts else relative_path.lower()

    if parts and parts[0] == "designs":
        return "input"
    if parts and parts[0] == "attachments":
        return "attachment"
    if "reports" in parts:
        return "report"
    if "results" in parts:
        return "result"
    if name.endswith(".log"):
        return "log"
    return "artifact"


def is_downloadable_artifact(relative_path: str, include_inputs: bool = False) -> bool:
    """Decide whether a file should appear in download listings."""
    parts = Path(relative_path).parts
    if not parts or parts[-1] in _SKIP_FILENAMES:
        return False
    if any(part == "__pycache__" for part in parts):
        return False
    if parts[0] in _INPUT_DIRS and not include_inputs:
        return False
    return True


def resolve_session_file(session_dir: Path, relative_path: str) -> Path:
    """Safely resolve a relative path inside a session directory."""
    root = session_dir.resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("Invalid file path.")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(relative_path)
    return candidate


def list_session_artifacts(session_dir: Path, include_inputs: bool = False) -> list[dict[str, Any]]:
    """List files in a session that the user can download."""
    artifacts: list[dict[str, Any]] = []
    if not session_dir.exists():
        return artifacts

    for path in sorted(session_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(session_dir).as_posix()
        if not is_downloadable_artifact(relative_path, include_inputs=include_inputs):
            continue
        stat = path.stat()
        artifacts.append(
            {
                "path": relative_path,
                "name": path.name,
                "kind": classify_artifact(relative_path),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    return artifacts


def build_artifact_zip(
    session_dir: Path,
    session_name: str,
    include_inputs: bool = False,
    selected_paths: list[str] | None = None,
) -> tuple[str, bytes, int]:
    """Build an in-memory zip for a session's downloadable artifacts."""
    selected = set(selected_paths or [])
    artifacts = list_session_artifacts(session_dir, include_inputs=include_inputs)
    if selected:
        artifacts = [item for item in artifacts if item["path"] in selected]

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for artifact in artifacts:
            path = resolve_session_file(session_dir, artifact["path"])
            archive.write(path, arcname=os.path.join(session_name, artifact["path"]))

    return f"{session_name}_results.zip", buffer.getvalue(), len(artifacts)
