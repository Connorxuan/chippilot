"""Snapshot validation, workspace preparation, and artifact manifests."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from benchmark_adapter.models import RunRequest, SnapshotManifest


MANIFEST_NAME = "benchmark_manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Invalid relative path: {value}")
    return path


def resolve_inside(root: Path, relative_path: str, *, must_exist: bool = True) -> Path:
    relative = safe_relative(relative_path)
    base = root.resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ValueError("Path escapes run directory")
    if must_exist and (not target.exists() or not target.is_file()):
        raise FileNotFoundError(relative_path)
    return target


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"Snapshot root cannot be a symbolic link: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Snapshot contains a symbolic link: {path.relative_to(root)}")


def load_snapshot(path_value: str) -> tuple[Path, SnapshotManifest]:
    supplied = Path(path_value)
    if not supplied.is_absolute():
        raise ValueError("snapshot_path must be absolute")
    root = supplied.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("snapshot_path must reference a directory")
    _reject_symlinks(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"Snapshot is missing {MANIFEST_NAME}")
    manifest = SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    declared = set()
    for item in manifest.files:
        relative = safe_relative(item.path)
        target = root / relative
        if not target.is_file():
            raise ValueError(f"Declared snapshot file does not exist: {item.path}")
        actual = sha256_file(target)
        if actual.lower() != item.sha256.lower():
            raise ValueError(f"SHA-256 mismatch for snapshot file: {item.path}")
        declared.add(relative.as_posix())
    return root, manifest


def prepare_run(run_dir: Path, request: RunRequest) -> SnapshotManifest:
    source, manifest = load_snapshot(request.snapshot_path)
    run_dir.mkdir(parents=True, exist_ok=False)
    input_dir = run_dir / "input"
    shutil.copytree(source, input_dir, symlinks=False)
    (run_dir / "workspace").mkdir()
    (run_dir / "submission").mkdir()
    (run_dir / "task.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
    initial = immutable_hashes(input_dir, manifest, request.task.immutable)
    (run_dir / "immutable_initial.json").write_text(
        json.dumps(initial, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def immutable_hashes(
    root: Path, manifest: SnapshotManifest, patterns: list[str]
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for item in manifest.files:
        if item.immutable or any(fnmatch.fnmatch(item.path, pattern) for pattern in patterns):
            target = resolve_inside(root, item.path)
            hashes[item.path] = sha256_file(target)
    return hashes


def protect_immutable(root: Path, immutable: dict[str, str]) -> None:
    for relative in immutable:
        path = resolve_inside(root, relative)
        path.chmod(path.stat().st_mode & ~0o222)


def verify_immutable(root: Path, initial: dict[str, str]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for relative, expected in initial.items():
        try:
            actual = sha256_file(resolve_inside(root, relative))
        except FileNotFoundError:
            violations.append({"type": "immutable_deleted", "path": relative})
            continue
        if actual != expected:
            violations.append(
                {"type": "immutable_modified", "path": relative, "expected": expected, "actual": actual}
            )
    return violations


def artifact_manifest(root: Path, *, exclude_inputs: bool = True) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if not root.is_dir():
        return artifacts
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "artifact_manifest.json":
            continue
        if exclude_inputs and (
            relative == "input"
            or relative.startswith("input/")
            or "/designs/" in f"/{relative}"
            or relative in {"task.json", "immutable_initial.json"}
        ):
            continue
        stat = path.stat()
        artifacts.append(
            {
                "artifact_id": "sha256:" + sha256_file(path),
                "path": relative,
                "size_bytes": stat.st_size,
                "sha256": sha256_file(path),
                "producer_event_id": None,
                "consumers": [],
            }
        )
    return artifacts
