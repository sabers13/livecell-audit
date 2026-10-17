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


def sha256_file(path: str) -> Tuple[str, int]:
    """Return ``(hex digest, byte size)`` streaming from disk."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


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
