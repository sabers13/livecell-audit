"""Shared staged-output and completion-marker helpers.

Milestones 2 (warehouse) and 3 (features) reuse the same contract the audit
established in milestone 1: every output is written into a hidden ``.staging``
directory inside the target, promoted into place only after all files are
written, and finally marked complete by an atomically published ``COMPLETED``
file. A failed or interrupted build removes the marker and the staging tree,
so a partial directory can never be mistaken for a completed run.

The audit module keeps its own private copies of this logic (it was shipped in
milestone 1 and its tests pin the exact behaviour); new stages use this module
so the contract is stated once for the expansion work.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Any, Dict, Optional, Tuple

COMPLETION_MARKER = "COMPLETED"
STAGING_DIRNAME = ".staging"
MANIFEST_NAME = "manifest.json"


def sha256_file(path: str) -> Tuple[str, int]:
    """Return ``(hex digest, byte size)`` streaming from disk."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_manifest(
    stage: str,
    schema_version: str,
    base_dir: str,
    relative_paths,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a size+SHA-256 manifest for a stage's required output files.

    A valid JSON completion marker is not enough on its own: a downstream stage
    hashes every listed file and compares it against this manifest, so a
    modified or swapped artifact (database, Parquet, JSON export) is rejected.
    """
    files = []
    for relative in sorted({p.replace(os.sep, "/") for p in relative_paths}):
        digest, size = sha256_file(os.path.join(base_dir, relative))
        files.append({"path": relative, "bytes": size, "sha256": digest})
    payload: Dict[str, Any] = {
        "stage": stage,
        "schema_version": schema_version,
        "files": files,
    }
    if extra:
        payload.update(extra)
    return payload


def verify_manifest(
    base_dir: str,
    stage: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    required_paths=(),
) -> Dict[str, Any]:
    """Verify a stage manifest and every file it lists.

    Returns ``{"ok": bool, "problems": [...], "manifest": ...}``. Callers raise
    their own stage-specific error when ``ok`` is false. Problems cover a
    missing/unparsable manifest, a manifest whose own SHA-256 does not match the
    marker, a listed file that is missing or whose size/SHA-256 changed, and a
    required path absent from the manifest.
    """
    problems = []
    manifest_path = os.path.join(base_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return {"ok": False, "problems": [f"missing manifest: {manifest_path}"], "manifest": None}
    if expected_sha256 is not None:
        digest, _size = sha256_file(manifest_path)
        if digest != expected_sha256:
            problems.append(
                f"manifest SHA-256 mismatch: file={digest} marker={expected_sha256}"
            )
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "problems": [f"unparsable manifest: {exc}"], "manifest": None}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return {"ok": False, "problems": ["manifest has no files list"], "manifest": manifest}
    if stage is not None and manifest.get("stage") != stage:
        problems.append(f"manifest stage mismatch: {manifest.get('stage')!r} != {stage!r}")
    listed = set()
    for entry in manifest["files"]:
        relative = entry.get("path")
        listed.add(relative)
        path = os.path.join(base_dir, relative) if relative else None
        if not path or not os.path.isfile(path):
            problems.append(f"manifest file missing: {relative}")
            continue
        digest, size = sha256_file(path)
        if digest != entry.get("sha256"):
            problems.append(
                f"content SHA-256 mismatch for {relative}: file={digest} manifest={entry.get('sha256')}"
            )
        if entry.get("bytes") is not None and size != entry["bytes"]:
            problems.append(
                f"content size mismatch for {relative}: file={size} manifest={entry['bytes']}"
            )
    for required in required_paths:
        if required not in listed:
            problems.append(f"required path not in manifest: {required}")
    return {"ok": not problems, "problems": problems, "manifest": manifest}



def count_lines(path: str) -> int:
    """Count newline-terminated records in a JSONL file (streaming)."""
    count = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            count += chunk.count(b"\n")
    return count


def clear_stale(out_dir: str, marker_name: str = COMPLETION_MARKER) -> None:
    """Invalidate any previous completion evidence before fallible work.

    Removes the marker (and an interrupted ``*.tmp`` marker) and the staging
    tree, so a failed rerun into a reused directory can never keep looking
    like a completed run.
    """
    marker_path = os.path.join(out_dir, marker_name)
    for stale in (marker_path, marker_path + ".tmp"):
        if os.path.exists(stale):
            os.remove(stale)
    staging = os.path.join(out_dir, STAGING_DIRNAME)
    if os.path.exists(staging):
        shutil.rmtree(staging)


def promote_staged(staging_dir: str, out_dir: str) -> None:
    """Move staged outputs into ``out_dir``, replacing previous files."""
    os.makedirs(out_dir, exist_ok=True)
    for name in sorted(os.listdir(staging_dir)):
        source = os.path.join(staging_dir, name)
        destination = os.path.join(out_dir, name)
        if os.path.isdir(source):
            if os.path.exists(destination):
                shutil.rmtree(destination)
            shutil.move(source, destination)
        else:
            os.replace(source, destination)
    os.rmdir(staging_dir)


def write_completion_marker(marker_path: str, payload: Any) -> None:
    """Write the run-completion marker atomically.

    Serialization goes to a sibling ``*.tmp`` file published with
    :func:`os.replace`, so a partial marker is never observable: either the
    complete marker exists or it does not.
    """
    tmp_path = marker_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, marker_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def dump_json(path: str, payload: Any) -> None:
    """Write deterministic, human-readable JSON (sorted keys, trailing newline)."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_completion_marker(out_dir: str, marker_name: str = COMPLETION_MARKER) -> Optional[Dict[str, Any]]:
    """Return the parsed completion marker, or ``None`` if absent/invalid.

    A marker that exists but cannot be parsed or does not declare
    ``status == "completed"`` is treated as *not completed* rather than raising,
    so callers can produce a clear "not a completed run" error.
    """
    marker_path = os.path.join(out_dir, marker_name)
    if not os.path.isfile(marker_path):
        return None
    try:
        payload = read_json(marker_path)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return None
    return payload


def dir_size_bytes(path: str) -> int:
    """Total bytes of every file under ``path`` (0 when absent)."""
    total = 0
    if not os.path.isdir(path):
        return 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total
