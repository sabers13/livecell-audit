#!/usr/bin/env python3
"""Release driver for the LIVECell pipeline, called by ``tools/run_release.sh``.

This module owns the parts of a release run that need real Python: input
fingerprinting, per-stage run records (configuration, seeds, source/data/code/
environment/output hashes, wall time and peak memory), cross-stage validation,
the pipeline completion marker, and the portable ZIP (assemble + verify).

Design rules (they mirror the project's existing manifest discipline):

* ``tools/run_release.sh`` orchestrates processes, launcher invocations and
  logs; this module never launches itself. Every invocation of this module on
  the data server is expected to be wrapped by ``run-job``.
* A stage record is written only after the stage's own ``COMPLETED`` marker and
  required outputs are present and valid; a failed stage writes a *failure*
  record and exits non-zero. A stage is never run when an input stage lacks a
  valid ``COMPLETED`` marker or fails its cross-stage identity check.
* Output identity is a tree hash over ``(relative path, bytes, sha256)`` of
  every file in a stage output directory (see :func:`hash_tree`), so a modified
  or swapped artifact is detectable after the fact.
* The pipeline completion marker (``COMPLETED`` at the run root) is written last
  and atomically, and the release receipt is derived from the stage records --
  never hand-written.

Subcommands (all take ``--run`` where a run directory is involved):

``preflight``       environment/input checks that start nothing heavy
``fingerprint``     input identity for the run ID (COCO sha256 + image set,
                    optional per-TIFF sha256)
``run-start``       environment, code and data identity for the run
``stage``           run one stage under its record/cross-check discipline
``verify-chain``    re-verify every stage marker, manifest, cross-link and
                    recorded output hash of a finished run
``run-complete``    write the pipeline completion marker + release receipt
``package``         assemble and verify the portable release ZIP
``verify-package``  unzip a release ZIP to a temp dir and re-resolve its links
``list``            list runs under an output root with their completion state

No container is used or assumed: this runs directly in the project virtualenv
inside the existing ``run-job`` launcher (1536 MiB RAM, 1 CPU, one job, 30 min).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

SCHEMA_VERSION = 1
RUN_RECORDS_DIR = "records"
RUN_LOGS_DIR = "logs"
RUN_PROVENANCE_DIR = "provenance"
COMPLETION_MARKER = "COMPLETED"
SITE_INPUTS_DIR = "site-inputs"
#: Names the website tooling expects inside its ``--inputs`` directory.
SITE_RANK_DIRNAME = "ranking-fixed-mem"
SITE_EVAL_DIRNAME = "evaluation-fixed-mem"
PREVIEWS_DIRNAME = "previews"

#: The release chain. ``previews`` is optional (see ``--with-previews``).
STAGE_ORDER = (
    "audit",
    "warehouse",
    "features",
    "ranking",
    "evaluation",
    "site-seed",
    "previews",
    "site",
    "site-check",
)

#: Artifacts that must never appear in the portable release.
EXCLUDED_SUFFIXES = (
    ".tif",
    ".tiff",
    ".duckdb",
    ".wal",
    ".joblib",
    ".parquet",
    ".pkl",
    ".pt",
    ".pth",
    ".zip",
    ".tar",
    ".sqlite",
    ".lock",
)

#: Substrings that must never appear in a packaged text file. ``/home/`` and the
#: other names come from tools/check_review_site.py; the rest are secrets.
PRIVATE_MARKERS = (
    "/home/",
    "/Users/",
    "C:\\",
    "saber_s13",
    "firstmate",
    "livecell-mvp",
    "-----BEGIN",
    "AKIA",
    "ghp_",
    "github_pat_",
    "ssh-rsa ",
    "ssh-ed25519 ",
)

#: Absolute-path shapes that must never appear in a packaged text file.
ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s\"'=(,:])(/(?:home|Users|mnt|media|srv|opt|root|tmp|var|etc|usr)/|"
    r"[A-Za-z]:\\)"
)

#: Absolute paths, redacted out of packaged documentation and command lines.
GENERIC_ABS_PATH_RE = re.compile(
    r"(?:/(?:home|Users|mnt|media|srv|opt|root|tmp|var|etc|usr)[^\s\"'`)\],;:]*"
    r"|[A-Za-z]:\\\\[^\s\"'`]*)"
)

#: ``user@host`` style targets, redacted out of packaged documentation.
SSH_TARGET_RE = re.compile(r"\b[A-Za-z0-9._-]+@\d{1,3}(?:\.\d{1,3}){3}\b")

#: Words that identify the private working environment, redacted in packaged docs.
DOC_REDACTIONS = (
    ("firstmate", "<supervisor-tree>"),
    ("livecell-mvp", "<legacy-run>"),
)

#: Files copied into every site build directory before the builder runs: the
#: hand-written shell of the review website (HTML + CSS + JS).
SITE_SHELL_FILES = (
    "index.html",
    os.path.join("assets", "sartorius-logo.svg"),
    os.path.join("assets", "review.css"),
    os.path.join("assets", "review.js"),
    os.path.join("assets", "fonts", "inter-latin-wght-normal.woff2"),
    os.path.join("assets", "fonts", "inter-latin-wght-italic.woff2"),
    os.path.join("assets", "fonts", "OFL-inter.txt"),
)

ATTRIBUTION = (
    "LIVECell dataset and annotations: Sartorius Corporate Research "
    "(Edlund et al., Nature Methods 2021), CC BY-NC 4.0.",
    "Cross-split overlap / duplicate report (upstream issue #26): community reporter OasisArtisan.",
    "Incomplete-annotation report (upstream issue #21): community reporter ivan-ea; "
    "maintainer reply by Rickard Sjögren (Sartorius).",
    "Alternate-annotation-version discussion (upstream issue #14): Christoffer Edlund (Sartorius).",
    "Audit, warehouse, feature, ranking, evaluation and review tooling in this "
    "repository: Saber Sojudi (2026).",
)

LIMITATIONS = (
    "Scores are unusualness indicators, not probabilities that a label is wrong.",
    "The nine previously reported sparse-label cases are a regression exercise, not "
    "recall over all bad labels.",
    "Synthetic annotation-loss metrics are simulation metrics with a structured loss "
    "mask, not real annotation-error precision.",
    "No scientific reviewer was supplied: real-world precision is unmeasured and the "
    "review sample ships with blank judgments.",
    "Raw TIFF pixels are content-hashed only where the run recorded a TIFF identity "
    "manifest; COCO hashes alone cannot detect changed image pixels.",
    "Acquisition groups are a coarse leakage proxy, not proof of biological independence.",
    "Content manifests are integrity evidence, not protection against an attacker who "
    "rewrites both a marker and its manifest.",
)

DEFERRED = (
    "Cloud execution: not run and not authorized.",
    "Public website integration / deployment: deferred by the user.",
    "Independent expert scientific review: not obtained.",
    "Container execution: no docker, podman or buildah is available on the data "
    "server (inspected), so no image was built and none is claimed.",
    "Upstream contribution: none submitted.",
)

#: The one completed end-to-end release run this tooling ships against, verified
#: against the run records, the chain verification and the packaged archive.
#: It is recorded here -- and in ``docs/12_release.md`` -- so a reader of any
#: package can see what a finished run of this tooling actually proved. Values are
#: facts of that run (ids, counts, hashes, byte sizes), never machine paths, host
#: names, account names or SSH targets.
REFERENCE_RUN = {
    "run_id": "20261018T231214Z-0e359b9bf088",
    "verified_by": "check of the run records, chain verification and packaged archive",
    "chain_verification": {"checks": 92, "failures": 0},
    "inputs": {
        "coco_files_fingerprinted": 3,
        "images_fingerprinted": 5239,
    },
    "audit": {"image_records": 5387, "annotations": 1662447, "distinct_filenames": 5239},
    "warehouse": {"schema_version": 1, "view_version": 1},
    "evaluation": {
        "ranking_manifest_sha256": (
            "f4364e5ca4b5dcbcdabdcfb137c7477961cab7e77f9422bb8f625eb78dbffe6a"
        ),
        "match": "exact match against the ranking stage's manifest",
    },
    "site_check": {
        "checks": 1120,
        "failures": 0,
        "skipped": 1,
        "skipped_reason": "JavaScript syntax check: node is not installed on the data server",
    },
    "previews": {"rendered": 39, "hash_verified": 39, "label_layers": 30, "guided_layers": 2},
    "stage_peak_memory_mb": {
        "audit": 199,
        "warehouse": 326,
        "features": 275,
        "ranking": 340,
        "evaluation": 350,
        "previews": 60,
        "site": 110,
    },
    "peak_memory_method": (
        "process peak RSS of each stage command (getrusage RUSAGE_CHILDREN ru_maxrss), "
        "one job at a time"
    ),
    "launcher_cgroup": (
        "the launcher's cgroup memory.peak also counts page cache: the audit stage reached "
        "772 MB and the warehouse stage 585 MB, the features stage reached the 1 GiB throttle "
        "and used 244 MB of job swap; no stage reached the 1536 MiB limit"
    ),
    "package": {
        "file": "livecell-audit-release-20261018T231214Z-0e359b9bf088.zip",
        "bytes": 10943796,
        "sha256": "aacac6ed3e33e0fa158af60ca00db89bff600e74c9f5d8e83e2088d6485b5522",
        "verified": True,
    },
}


class ReleaseError(Exception):
    """A release step that must stop instead of guessing."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str, payload) -> None:
    """Write deterministic JSON atomically (tmp + replace, sorted keys)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def relpath_posix(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def file_bytes(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def list_tree(root: str) -> list:
    """Every regular file under ``root`` as a sorted list of relative paths.

    Symlinks are skipped (the ``site-inputs`` directory uses them for staging
    only, and a release must never contain one).
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".staging")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            found.append(relpath_posix(root, full))
    return sorted(found)


def hash_tree(root: str) -> dict:
    """Content identity of a directory: per-file hashes plus one tree hash.

    ``tree_sha256`` is the SHA-256 of the newline-joined
    ``"<relative path>\\t<bytes>\\t<sha256>"`` lines, sorted by path, so it
    changes if any file is added, removed, resized or altered.
    """
    files = []
    lines = []
    for relative in list_tree(root):
        full = os.path.join(root, relative)
        digest = sha256_file(full)
        size = file_bytes(full)
        files.append({"path": relative, "bytes": size, "sha256": digest})
        lines.append(f"{relative}\t{size}\t{digest}")
    payload = "\n".join(lines)
    return {
        "dir": root,
        "file_count": len(files),
        "bytes_total": sum(entry["bytes"] for entry in files),
        "tree_sha256": sha256_text(payload) if payload else None,
        "hash_method": (
            "sha256 over newline-joined '<relative path>\\t<bytes>\\t<sha256>' lines, "
            "sorted by path; symlinks and .staging excluded"
        ),
        "files": files,
    }


def load_marker(stage_dir: str):
    """Parsed ``COMPLETED`` marker or ``None`` when absent/invalid."""
    path = os.path.join(stage_dir, COMPLETION_MARKER)
    if not os.path.isfile(path):
        return None
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return None
    return payload


def require_marker(stage_dir: str, stage: str) -> dict:
    """A valid ``COMPLETED`` marker for ``stage``, or an actionable error."""
    if not os.path.isdir(stage_dir):
        raise ReleaseError(
            f"stage output directory is missing: {stage_dir} "
            f"(stage {stage!r} has not been run in this run directory)"
        )
    marker = load_marker(stage_dir)
    if marker is None:
        raise ReleaseError(
            f"stage {stage!r} has no valid {COMPLETION_MARKER} marker in {stage_dir}: "
            "refusing to consume unfinished outputs (rerun that stage instead of "
            "fabricating a marker)"
        )
    declared = marker.get("stage")
    if declared is not None and declared != stage:
        raise ReleaseError(
            f"{stage_dir}: {COMPLETION_MARKER} declares stage {declared!r}, expected {stage!r}"
        )
    return marker


def stage_dir(run_dir: str, name: str) -> str:
    return os.path.join(run_dir, SPEC[name].out_rel) if SPEC[name].out_rel else run_dir


def record_path(run_dir: str, name: str) -> str:
    return os.path.join(run_dir, RUN_RECORDS_DIR, f"stage-{name}.json")


def log_path(run_dir: str, name: str) -> str:
    return os.path.join(run_dir, RUN_LOGS_DIR, f"{name}.log")


def load_record(run_dir: str, name: str):
    path = record_path(run_dir, name)
    if not os.path.isfile(path):
        return None
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def require_record(run_dir: str, name: str) -> dict:
    record = load_record(run_dir, name)
    if record is None:
        raise ReleaseError(f"no run record for stage {name!r} in {run_dir}")
    if record.get("status") != "completed":
        raise ReleaseError(
            f"stage {name!r} did not complete (record status {record.get('status')!r}); "
            "start a new run instead of reusing this directory"
        )
    return record


def record_sha256(run_dir: str, name: str):
    path = record_path(run_dir, name)
    return sha256_file(path) if os.path.isfile(path) else None


def cgroup_info() -> dict:
    """Actual enforced cgroup v2 limits/peak of the current job, when readable."""
    info = {}
    for key, name in (
        ("memory.max", "memory_max_bytes"),
        ("memory.peak", "memory_peak_bytes"),
        ("memory.high", "memory_high_bytes"),
        ("cpu.max", "cpu_max"),
        ("pids.max", "pids_max"),
    ):
        path = os.path.join("/sys/fs/cgroup", key)
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if name.endswith("_bytes") and raw.isdigit():
            info[name] = int(raw)
        else:
            info[name] = raw
    return info


def peak_memory_mb() -> dict:
    """Peak RSS of the child processes waited for by this process."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak_kb = int(usage.ru_maxrss)  # Linux reports KiB
    return {
        "peak_memory_mb": round(peak_kb / 1024.0, 3),
        "method": (
            "resource.getrusage(RUSAGE_CHILDREN).ru_maxrss of the stage command "
            "(KiB on Linux), measured inside the run-job cgroup"
        ),
        "cpu_seconds": round(float(usage.ru_utime + usage.ru_stime), 3),
    }


def find_error_lines(log_file: str, limit: int = 6) -> list:
    """A short, actionable tail of a failed stage log."""
    if not os.path.isfile(log_file):
        return []
    interesting = []
    with open(log_file, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            lowered = stripped.lower()
            if "error" in lowered or "traceback" in lowered or "exception" in lowered:
                interesting.append(stripped.strip()[:400])
    if not interesting:
        with open(log_file, encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\n") for line in handle if line.strip()]
        interesting = [line.strip()[:400] for line in lines[-limit:]]
    return interesting[-limit:]


class Checker:
    """Collects named checks and fails loudly on the first failure."""

    def __init__(self, name: str, collect: bool = False):
        self.name = name
        self.collect = collect
        self.checks = []

    def add(self, label: str, ok: bool, detail: str = "") -> None:
        entry = {"check": label, "ok": bool(ok), "detail": detail}
        self.checks.append(entry)
        if not ok and not self.collect:
            raise ReleaseError(f"{self.name}: {label}{(': ' + detail) if detail else ''}")

    def require(self, label: str, condition: bool, detail: str = "") -> None:
        self.add(label, condition, detail)

    def warn(self, label: str, detail: str = "") -> None:
        """Record an advisory note that is reported but never fails the checker."""
        self.checks.append({"check": label, "ok": True, "warning": True, "detail": detail})


# --------------------------------------------------------------------------- #
# stage specifications and commands
# --------------------------------------------------------------------------- #


class StageSpec:
    def __init__(self, name, out_rel, inputs, required, marker, config):
        self.name = name
        self.out_rel = out_rel
        self.inputs = tuple(inputs)
        self.required = tuple(required)
        self.marker = marker
        self.config = config


SPEC = {
    "audit": StageSpec(
        "audit",
        "audit",
        (),
        (
            "report.json",
            "findings.json",
            "run.json",
            "manifests/sources.json",
            "manifests/val_membership.json",
            "tables/images.jsonl",
            "tables/annotations.jsonl",
            COMPLETION_MARKER,
        ),
        True,
        {"splits": ["train", "val", "test"], "low_count_threshold": 10, "check_dimensions": True},
    ),
    "warehouse": StageSpec(
        "warehouse",
        "warehouse",
        ("audit",),
        (
            "audit.duckdb",
            "integrity.json",
            "manifest.json",
            "run.json",
            "parquet",
            COMPLETION_MARKER,
        ),
        True,
        {"schema_version": "1", "view_version": 1, "parquet_compression": "zstd"},
    ),
    "features": StageSpec(
        "features",
        "features",
        ("warehouse",),
        (
            "features.parquet",
            "features.jsonl",
            "feature_dictionary.json",
            "cell_line_summary.json",
            "run.json",
            "manifest.json",
            COMPLETION_MARKER,
        ),
        True,
        {"feature_schema_version": "1", "max_filenames": None},
    ),
    "ranking": StageSpec(
        "ranking",
        "ranking",
        ("features", "warehouse"),
        (
            "candidates.jsonl",
            "candidates.csv",
            "candidates.parquet",
            "version_provenance.jsonl",
            "partition_manifest.json",
            "model_card.json",
            "fit_reference.json",
            "summary.json",
            "run.json",
            "manifest.json",
            "models",
            COMPLETION_MARKER,
        ),
        True,
        {"ranking_schema_version": "1", "seed": 42, "budgets": [10, 25, 50], "sensitivity_seeds": [7, 42, 101]},
    ),
    "evaluation": StageSpec(
        "evaluation",
        "evaluation",
        ("features", "warehouse", "ranking"),
        (
            "known_cases.json",
            "known_cases.csv",
            "synthetic_annotation_loss.json",
            "synthetic_paired_deltas.csv",
            "review_sample.json",
            "review_sample.csv",
            "evaluation_summary.json",
            "run.json",
            "manifest.json",
            COMPLETION_MARKER,
        ),
        True,
        {"evaluation_schema_version": "1", "budgets": [10, 25, 50], "annotation_loss_mask_seed": 42},
    ),
    "site-seed": StageSpec(
        "site-seed",
        "site-seed",
        ("audit", "ranking", "evaluation"),
        (
            "index.html",
            "assets/review.css",
            "assets/review.js",
            "data/site-data.js",
            "data/rankings.js",
            "data/curated-examples.json",
            "downloads/ranking-pool.csv",
            "downloads/model-card.json",
            "downloads/evaluation-summary.json",
            "downloads/known-cases.json",
            "downloads/review-sample.json",
            "downloads/ranking-summary.json",
            "downloads/audit-findings.json",
            "downloads/RANKING-README.txt",
            "THIRD_PARTY_NOTICES.txt",
        ),
        False,
        {"purpose": "provisional build; supplies the deterministic curated-example list"},
    ),
    "previews": StageSpec(
        "previews",
        os.path.join(SITE_INPUTS_DIR, PREVIEWS_DIRNAME),
        ("site-seed",),
        ("manifest.json", "polygons.json"),
        False,
        {"max_previews": 40, "longest_side": 704, "outline_cap": 20, "overlay_mode": "plain"},
    ),
    "site": StageSpec(
        "site",
        "site",
        ("audit", "ranking", "evaluation", "site-seed"),
        (
            "index.html",
            "assets/review.css",
            "assets/review.js",
            "assets/guided-shared.png",
            "assets/guided-sparse.png",
            "data/site-data.js",
            "data/rankings.js",
            "data/curated-examples.json",
            "downloads/ranking-pool.csv",
            "downloads/model-card.json",
            "downloads/evaluation-summary.json",
            "downloads/known-cases.json",
            "downloads/review-sample.json",
            "downloads/ranking-summary.json",
            "downloads/audit-findings.json",
            "downloads/RANKING-README.txt",
            "THIRD_PARTY_NOTICES.txt",
        ),
        False,
        {"purpose": "final published site (includes rendered previews when enabled)"},
    ),
    "site-check": StageSpec(
        "site-check",
        None,
        ("site",),
        (),
        False,
        {"tool": "tools/check_review_site.py"},
    ),
}


def stage_command(spec: StageSpec, ctx) -> list:
    """The exact command line of a stage (recorded verbatim in its record)."""
    run = ctx.run_dir
    root = ctx.repo
    data = ctx.data_root
    python = ctx.python
    if spec.name == "audit":
        return [
            python, "-m", "livecell_audit", "audit",
            "--data-root", data,
            "--out-dir", os.path.join(run, "audit"),
        ]
    if spec.name == "warehouse":
        return [
            python, "-m", "livecell_audit", "warehouse",
            "--audit-dir", os.path.join(run, "audit"),
            "--out-dir", os.path.join(run, "warehouse"),
            "--data-root", data,
        ]
    if spec.name == "features":
        return [
            python, "-m", "livecell_audit", "features",
            "--warehouse-dir", os.path.join(run, "warehouse"),
            "--data-root", data,
            "--out-dir", os.path.join(run, "features"),
        ]
    if spec.name == "ranking":
        budgets = [str(value) for value in ctx.budgets]
        return [
            python, "-m", "livecell_audit", "rank",
            "--features-dir", os.path.join(run, "features"),
            "--warehouse-dir", os.path.join(run, "warehouse"),
            "--out-dir", os.path.join(run, "ranking"),
            "--seed", str(ctx.seed),
            "--budgets", *budgets,
        ]
    if spec.name == "evaluation":
        budgets = [str(value) for value in ctx.budgets]
        return [
            python, "-m", "livecell_audit", "evaluate",
            "--features-dir", os.path.join(run, "features"),
            "--warehouse-dir", os.path.join(run, "warehouse"),
            "--ranking-dir", os.path.join(run, "ranking"),
            "--out-dir", os.path.join(run, "evaluation"),
            "--budgets", *budgets,
        ]
    if spec.name in ("site-seed", "site"):
        return [
            python, os.path.join(root, "tools", "build_review_site.py"),
            "--audit-dir", os.path.join(run, "audit"),
            "--inputs", os.path.join(run, SITE_INPUTS_DIR),
            "--out", os.path.join(run, spec.out_rel),
        ]
    if spec.name == "previews":
        return [
            python, os.path.join(root, "tools", "make_previews.py"),
            "--curated", os.path.join(run, "site-seed", "data", "curated-examples.json"),
            "--data-root", data,
            "--out", os.path.join(run, SITE_INPUTS_DIR, PREVIEWS_DIRNAME),
        ]
    if spec.name == "site-check":
        return [
            python, os.path.join(root, "tools", "check_review_site.py"),
            "--site", os.path.join(run, "site"),
            "--inputs", os.path.join(run, SITE_INPUTS_DIR),
            "--audit-dir", os.path.join(run, "audit"),
        ]
    raise ReleaseError(f"no command defined for stage {spec.name!r}")


def active_stages(with_previews: bool) -> tuple:
    """The stages a run actually executes (the preview chain is optional)."""
    if with_previews:
        return tuple(STAGE_ORDER)
    return tuple(name for name in STAGE_ORDER if name not in ("site-seed", "previews"))


def stage_inputs(name: str, with_previews: bool) -> tuple:
    """Inputs of a stage, accounting for the optional preview chain."""
    if name == "site" and not with_previews:
        return tuple(entry for entry in SPEC[name].inputs if entry != "site-seed")
    return SPEC[name].inputs


# --------------------------------------------------------------------------- #
# stage input cross-checks
# --------------------------------------------------------------------------- #


def _import_package():
    """Import the project package lazily (PYTHONPATH is set by run-job)."""
    try:
        from livecell_audit import completion, evaluate, features, ranking
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ReleaseError(
            f"cannot import the livecell_audit package ({exc}); run this through "
            "run-job (or set PYTHONPATH=<repo>/src) with the project virtualenv"
        ) from exc
    return completion, evaluate, features, ranking


def check_audit_inputs(ctx, checker: Checker) -> None:
    fingerprint = ctx.fingerprint
    for split, entry in sorted(fingerprint["coco"].items()):
        path = os.path.join(ctx.data_root, entry["path"])
        checker.require(f"raw input exists: {entry['path']}", os.path.isfile(path), path)
        if not os.path.isfile(path):
            continue
        checker.require(
            f"raw input size unchanged since fingerprint: {entry['path']}",
            file_bytes(path) == entry["bytes"],
            f"fingerprint {entry['bytes']} vs file {file_bytes(path)}",
        )
        digest = sha256_file(path)
        checker.require(
            f"raw input sha256 matches fingerprint: {entry['path']}",
            digest == entry["sha256"],
            f"{digest} != {entry['sha256']}",
        )
    images_root = os.path.join(ctx.data_root, "images")
    checker.require("raw images root exists", os.path.isdir(images_root), images_root)
    expected = fingerprint["images"]["count"]
    if os.path.isdir(images_root):
        found = 0
        for _dirpath, _dirnames, filenames in os.walk(images_root):
            found += len(filenames)
        checker.require(
            "raw image file count matches fingerprint",
            found == expected,
            f"fingerprint {expected} vs {found} found",
        )


def check_audit_outputs(ctx, checker: Checker) -> None:
    audit = os.path.join(ctx.run_dir, "audit")
    marker = require_marker(audit, "audit")
    report = read_json(os.path.join(audit, "report.json"))
    sources = read_json(os.path.join(audit, "manifests", "sources.json"))
    marker_sha = marker.get("source_sha256") or {}
    source_sha = {split: entry.get("sha256") for split, entry in (sources.get("coco_files") or {}).items()}
    checker.require("audit marker source checksums match manifests/sources.json", marker_sha == source_sha)
    for field in ("total_image_records", "total_annotations", "unique_filenames"):
        checker.require(
            f"audit marker {field} matches report.json",
            marker.get(field) == report.get(field),
            f"marker {marker.get(field)} vs report {report.get(field)}",
        )
    checker.require("audit marker declares the three splits", sorted(marker.get("splits") or []) == ["test", "train", "val"])
    fingerprint_sha = ctx.fingerprint["coco"]
    checker.require(
        "audit source hashes match the run's input fingerprint",
        {split: entry.get("sha256") for split, entry in fingerprint_sha.items()} == marker_sha,
    )


def check_warehouse_inputs(ctx, checker: Checker) -> None:
    """The only input of the warehouse stage is a completed audit run."""
    check_audit_outputs(ctx, checker)


def check_warehouse_outputs(ctx, checker: Checker) -> None:
    completion, _evaluate, _features, _ranking = _import_package()
    warehouse = os.path.join(ctx.run_dir, "warehouse")
    marker = require_marker(warehouse, "warehouse")
    checker.require("warehouse marker is not partial", not marker.get("partial"))
    checker.require("warehouse schema version is 1", str(marker.get("schema_version")) == "1")
    checker.require("warehouse view version is 1", marker.get("view_version") == 1)
    audit_marker = load_marker(os.path.join(ctx.run_dir, "audit")) or {}
    checker.require(
        "warehouse source hashes equal the audit run's",
        marker.get("source_sha256") == audit_marker.get("source_sha256"),
    )
    result = completion.verify_manifest(
        warehouse,
        stage="warehouse",
        expected_sha256=(marker.get("manifest") or {}).get("sha256"),
        required_paths=("audit.duckdb", "integrity.json", "run.json"),
    )
    checker.require(
        "warehouse manifest and every listed file verify",
        result["ok"],
        "; ".join(result["problems"][:4]),
    )


def check_features_inputs(ctx, checker: Checker) -> None:
    check_warehouse_outputs(ctx, checker)


def check_features_outputs(ctx, checker: Checker) -> None:
    _completion, _evaluate, features, _ranking = _import_package()
    try:
        marker = features.assert_release_ready(os.path.join(ctx.run_dir, "features"))
    except Exception as exc:  # stage-specific error types
        raise ReleaseError(f"features output is not release-ready: {exc}") from exc
    checker.require("features run is not partial", not marker.get("partial"))
    checker.require("features mode is full", marker.get("mode") == "full", str(marker.get("mode")))
    warehouse_marker = load_marker(os.path.join(ctx.run_dir, "warehouse")) or {}
    checker.require(
        "features recorded the warehouse manifest hash of this run",
        marker.get("warehouse_manifest_sha256")
        == (warehouse_marker.get("manifest") or {}).get("sha256"),
    )


def check_ranking_inputs(ctx, checker: Checker) -> None:
    _completion, _evaluate, _features, ranking = _import_package()
    try:
        ranking.require_verified_inputs(
            os.path.join(ctx.run_dir, "features"), os.path.join(ctx.run_dir, "warehouse")
        )
    except Exception as exc:
        raise ReleaseError(f"ranking inputs are not verified: {exc}") from exc
    checker.require("ranking inputs verified (features + matching warehouse)", True)


def check_ranking_outputs(ctx, checker: Checker) -> None:
    _completion, _evaluate, _features, ranking = _import_package()
    try:
        marker = ranking.assert_ranking_ready(os.path.join(ctx.run_dir, "ranking"))
    except Exception as exc:
        raise ReleaseError(f"ranking output is not release-ready: {exc}") from exc
    checker.require("ranking seed is 42", marker.get("seed") == ctx.seed, str(marker.get("seed")))
    checker.require(
        "ranking budgets are the declared review budgets",
        list(marker.get("budgets") or []) == list(ctx.budgets),
        str(marker.get("budgets")),
    )
    evaluation = load_marker(os.path.join(ctx.run_dir, "evaluation")) or {}
    ranking_build = (evaluation.get("ranking_build") or {}) if evaluation else {}
    if ranking_build:
        checker.require(
            "evaluation recorded this ranking manifest hash",
            ranking_build.get("ranking_manifest_sha256")
            == (marker.get("manifest") or {}).get("sha256"),
        )


def check_evaluation_inputs(ctx, checker: Checker) -> None:
    check_ranking_outputs(ctx, checker)
    check_features_outputs(ctx, checker)


def check_evaluation_outputs(ctx, checker: Checker) -> None:
    _completion, evaluate, _features, ranking = _import_package()
    run = ctx.run_dir
    try:
        marker = evaluate.assert_evaluation_ready(os.path.join(run, "evaluation"))
    except Exception as exc:
        raise ReleaseError(f"evaluation output is not release-ready: {exc}") from exc
    checker.require("evaluation run is not partial", not marker.get("partial"))
    ranking_marker = load_marker(os.path.join(run, "ranking")) or {}
    ranking_manifest_sha = sha256_file(os.path.join(run, "ranking", "manifest.json"))
    checker.require(
        "ranking manifest hash on disk equals the ranking marker's",
        (ranking_marker.get("manifest") or {}).get("sha256") == ranking_manifest_sha,
    )
    checker.require(
        "evaluation consumed this exact ranking manifest hash",
        (marker.get("ranking_build") or {}).get("ranking_manifest_sha256") == ranking_manifest_sha,
        f"evaluation {(marker.get('ranking_build') or {}).get('ranking_manifest_sha256')} "
        f"vs ranking manifest {ranking_manifest_sha}",
    )
    try:
        ranking.assert_build_consistency(
            os.path.join(run, "features"), os.path.join(run, "ranking"), os.path.join(run, "evaluation")
        )
    except Exception as exc:
        raise ReleaseError(f"ranking/evaluation/feature builds do not match: {exc}") from exc
    checker.require("evaluation/ranking/feature build identity matches", True)
    sample = read_json(os.path.join(run, "evaluation", "review_sample.json"))
    checker.require(
        "review sample carries no fabricated judgment",
        all(record.get("judgment") is None for record in sample.get("records", [])),
    )
    summary = read_json(os.path.join(run, "evaluation", "evaluation_summary.json"))
    checker.require(
        "evaluation claims no measured real-world precision",
        summary.get("human_review", {}).get("real_world_precision") is None,
    )


def ensure_site_inputs(ctx) -> None:
    """Stage the fresh ranking/evaluation runs where the site tools expect them."""
    inputs_dir = os.path.join(ctx.run_dir, SITE_INPUTS_DIR)
    os.makedirs(inputs_dir, exist_ok=True)
    for dirname, target in (
        (SITE_RANK_DIRNAME, "ranking"),
        (SITE_EVAL_DIRNAME, "evaluation"),
    ):
        link = os.path.join(inputs_dir, dirname)
        wanted = os.path.join(os.pardir, target)
        if os.path.islink(link):
            if os.readlink(link) != wanted:
                os.remove(link)
                os.symlink(wanted, link)
        elif os.path.exists(link):
            raise ReleaseError(
                f"{link} exists and is not a link to {target}; refusing to overwrite it"
            )
        else:
            os.symlink(wanted, link)


def check_site_inputs(ctx, checker: Checker) -> None:
    _completion, _evaluate, _features, ranking = _import_package()
    run = ctx.run_dir
    if not os.path.exists(os.path.join(run, COMPLETION_MARKER)):
        ensure_site_inputs(ctx)
    for dirname, target in ((SITE_RANK_DIRNAME, "ranking"), (SITE_EVAL_DIRNAME, "evaluation")):
        link = os.path.join(run, SITE_INPUTS_DIR, dirname)
        checker.require(
            f"site input {dirname} resolves to this run's {target}",
            os.path.isdir(link)
            and os.path.realpath(link) == os.path.realpath(os.path.join(run, target)),
        )
    require_marker(os.path.join(run, "ranking"), "ranking")
    require_marker(os.path.join(run, "evaluation"), "evaluation")
    try:
        ranking.assert_build_consistency(
            os.path.join(run, "features"), os.path.join(run, "ranking"), os.path.join(run, "evaluation")
        )
    except Exception as exc:
        raise ReleaseError(f"site inputs do not share one build identity: {exc}") from exc
    evaluation_marker = load_marker(os.path.join(run, "evaluation")) or {}
    ranking_manifest_sha = sha256_file(os.path.join(run, "ranking", "manifest.json"))
    checker.require(
        "site inputs: evaluation recorded this run's ranking manifest hash",
        (evaluation_marker.get("ranking_build") or {}).get("ranking_manifest_sha256")
        == ranking_manifest_sha,
    )
    audit_report = read_json(os.path.join(run, "audit", "report.json"))
    ranking_summary = read_json(os.path.join(run, "ranking", "summary.json"))
    checker.require(
        "site inputs: audit unique_filenames equals ranking canonical_filenames",
        audit_report.get("unique_filenames") == ranking_summary.get("counts", {}).get("canonical_filenames"),
    )


def check_previews_inputs(ctx, checker: Checker) -> None:
    curated = os.path.join(ctx.run_dir, "site-seed", "data", "curated-examples.json")
    checker.require("curated example list from the provisional site build exists", os.path.isfile(curated))
    checker.require("raw images root is available for the preview render", os.path.isdir(os.path.join(ctx.data_root, "images")))
    require_record(ctx.run_dir, "site-seed")


def check_previews_outputs(ctx, checker: Checker) -> None:
    previews = os.path.join(ctx.run_dir, SITE_INPUTS_DIR, PREVIEWS_DIRNAME)
    manifest_path = os.path.join(previews, "manifest.json")
    polygons_path = os.path.join(previews, "polygons.json")
    checker.require("preview manifest exists", os.path.isfile(manifest_path))
    checker.require("preview polygons exist", os.path.isfile(polygons_path))
    if not os.path.isfile(manifest_path):
        return
    manifest = read_json(manifest_path)
    curated = read_json(os.path.join(ctx.run_dir, "site-seed", "data", "curated-examples.json"))
    entries = manifest.get("previews") or []
    checker.require(
        "preview count matches the curated example set",
        len(entries) == curated.get("count"),
        f"{len(entries)} previews vs {curated.get('count')} curated examples",
    )
    checker.require(
        "preview count respects the 40-image bound",
        len(entries) <= 40 and len(entries) >= 25,
        str(len(entries)),
    )
    checker.require(
        "preview render manifest was produced from this curated set",
        manifest.get("curated_sha256") == sha256_file(os.path.join(ctx.run_dir, "site-seed", "data", "curated-examples.json")),
    )
    render = manifest.get("render") or {}
    longest = render.get("longest_side") or (manifest.get("render_parameters") or {}).get("longest_side")
    if longest is not None:
        checker.require("preview longest side is within the 704 px bound", int(longest) <= 704, str(longest))
    problems = []
    layer_problems = []
    for entry in entries:
        path = os.path.join(previews, str(entry.get("file")))
        if not os.path.isfile(path):
            problems.append(f"missing {entry.get('file')}")
        elif entry.get("png_sha256") and sha256_file(path) != entry["png_sha256"]:
            problems.append(f"{entry.get('file')} sha256 mismatch")
        layer = entry.get("labels_layer")
        capped = (entry.get("polygons_drawn") or 0) < (entry.get("polygons_total") or 0)
        if capped and not layer:
            layer_problems.append(f"{entry.get('file')} is capped but has no label layer")
        if layer:
            layer_path = os.path.join(previews, str(layer.get("file")))
            if not os.path.isfile(layer_path):
                layer_problems.append(f"missing {layer.get('file')}")
            elif sha256_file(layer_path) != layer.get("png_sha256"):
                layer_problems.append(f"{layer.get('file')} sha256 mismatch")
            elif layer.get("outlines_drawn") != entry.get("polygons_total"):
                layer_problems.append(f"{layer.get('file')} does not draw every supplied outline")
    checker.require("every preview PNG exists and matches its manifest hash", not problems, "; ".join(problems[:3]))
    checker.require(
        "every capped preview has a full label layer that matches its manifest hash",
        not layer_problems,
        "; ".join(layer_problems[:3]),
    )
    guided_problems = []
    for entry in manifest.get("guided") or []:
        path = os.path.join(previews, str(entry.get("file")))
        if not os.path.isfile(path):
            guided_problems.append(f"missing {entry.get('file')}")
        elif sha256_file(path) != entry.get("png_sha256"):
            guided_problems.append(f"{entry.get('file')} sha256 mismatch")
        elif entry.get("outlines_drawn") != entry.get("polygons_total"):
            guided_problems.append(f"{entry.get('file')} does not draw every supplied outline")
    checker.require(
        "every guided label layer matches its manifest hash and draws every outline",
        not guided_problems,
        "; ".join(guided_problems[:3]),
    )


def check_site_seed_outputs(ctx, checker: Checker) -> None:
    """The provisional build: previews cannot exist yet, so status is 'pending'."""
    site = os.path.join(ctx.run_dir, "site-seed")
    data = load_js_global(os.path.join(site, "data", "site-data.js"), "LIVECELL_REVIEW")
    meta = data.get("meta") or {}
    checker.require(
        "provisional site build declares previews as pending",
        meta.get("preview_status") == "pending",
        str(meta.get("preview_status")),
    )
    curated = read_json(os.path.join(site, "data", "curated-examples.json"))
    checker.require(
        "provisional build produced a curated example list inside the 25-40 bound",
        25 <= int(curated.get("count") or 0) <= 40,
        str(curated.get("count")),
    )


def check_site_outputs(ctx, checker: Checker) -> None:
    site = os.path.join(ctx.run_dir, "site")
    data = load_js_global(os.path.join(site, "data", "site-data.js"), "LIVECELL_REVIEW")
    meta = data.get("meta") or {}
    expected = "delivered" if ctx.with_previews else "pending"
    checker.require(
        f"site declares preview status {expected!r}",
        meta.get("preview_status") == expected,
        str(meta.get("preview_status")),
    )
    checker.require("site carries no absolute-path href/src", not re.search(r'(?:href|src)="/', read_text(os.path.join(site, "index.html"))))
    provenance = data.get("provenance") or {}
    missing = [
        entry.get("path")
        for entry in provenance.get("files", [])
        if not os.path.isfile(os.path.join(ctx.repo, str(entry.get("path"))))
    ]
    checker.require("every site provenance input path resolves", not missing, ", ".join(missing[:3]))
    ranking_summary = read_json(os.path.join(ctx.run_dir, "ranking", "summary.json"))
    checker.require(
        "site feature manifest hash comes from this run's ranking summary",
        (provenance.get("feature_manifest_sha256") or None)
        == (ranking_summary.get("provenance") or {}).get("feature_manifest_sha256"),
    )


def check_site_check_outputs(ctx, checker: Checker) -> None:
    log_file = log_path(ctx.run_dir, "site-check")
    if not os.path.isfile(log_file):
        raise ReleaseError(f"site-check log is missing: {log_file}")
    text = read_text(log_file)
    failures = [line.strip() for line in text.splitlines() if line.strip().startswith("FAIL")]
    oks = [line for line in text.splitlines() if line.strip().startswith("ok ")]
    summary_match = re.search(r"(\d+) failed check\(s\)", text)
    checker.require("site check ran and reported a summary", summary_match is not None)
    checker.require("site check reported zero failures", summary_match is not None and int(summary_match.group(1)) == 0, "; ".join(failures[:3]))
    checker.require("site check performed a meaningful number of checks", len(oks) >= 100, str(len(oks)))


STAGE_INPUT_CHECK = {
    "audit": check_audit_inputs,
    "warehouse": check_warehouse_inputs,
    "features": check_features_inputs,
    "ranking": check_ranking_inputs,
    "evaluation": check_evaluation_inputs,
    "site-seed": check_site_inputs,
    "previews": check_previews_inputs,
    "site": check_site_inputs,
    "site-check": check_site_inputs,
}

STAGE_OUTPUT_CHECK = {
    "audit": check_audit_outputs,
    "warehouse": check_warehouse_outputs,
    "features": check_features_outputs,
    "ranking": check_ranking_outputs,
    "evaluation": check_evaluation_outputs,
    "site-seed": check_site_seed_outputs,
    "previews": check_previews_outputs,
    "site": check_site_outputs,
    "site-check": check_site_check_outputs,
}


def load_js_global(path: str, global_name: str):
    text = read_text(path)
    prefix = "window." + global_name + " = "
    if not text.startswith(prefix) or not text.rstrip().endswith(";"):
        raise ReleaseError(f"{path}: not a window.{global_name} script global")
    return json.loads(text[len(prefix):].rstrip()[:-1])


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #


class Context:
    def __init__(self, args):
        self.run_dir = os.path.abspath(args.run) if getattr(args, "run", None) else None
        self.repo = os.path.abspath(
            args.repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.python = args.python or sys.executable
        self.data_root = os.path.abspath(args.data_root or ".")
        out_root = getattr(args, "out_root", None)
        if out_root:
            self.out_root = os.path.abspath(out_root)
        elif self.run_dir:
            self.out_root = os.path.dirname(self.run_dir)
        else:
            self.out_root = None
        self.seed = int(getattr(args, "seed", 42))
        self.budgets = tuple(int(value) for value in (getattr(args, "budgets", None) or [10, 25, 50]))
        explicit_previews = getattr(args, "with_previews", None)
        self.with_previews = (
            self._infer_previews() if explicit_previews is None else bool(explicit_previews)
        )
        self.tiff_identity = bool(getattr(args, "tiff_identity", False))
        self._fingerprint = None

    def _infer_previews(self) -> bool:
        """Whether a run used the preview stage, read back from that run."""
        if not self.run_dir or not os.path.isdir(self.run_dir):
            return False
        marker = load_marker(self.run_dir)
        if marker is not None and "with_previews" in marker:
            return bool(marker.get("with_previews"))
        start = os.path.join(self.run_dir, RUN_RECORDS_DIR, "run-start.json")
        if os.path.isfile(start):
            try:
                config = (read_json(start) or {}).get("config") or {}
            except (OSError, ValueError):
                config = {}
            if "with_previews" in config:
                return bool(config.get("with_previews"))
        return os.path.isfile(
            os.path.join(self.run_dir, SITE_INPUTS_DIR, PREVIEWS_DIRNAME, "manifest.json")
        )

    def require_run(self) -> str:
        if not self.run_dir:
            raise ReleaseError("this command needs --run <release run directory>")
        return self.run_dir

    @property
    def fingerprint(self):
        if self._fingerprint is None:
            path = os.path.join(self.run_dir, RUN_PROVENANCE_DIR, "input-fingerprint.json")
            if not os.path.isfile(path):
                raise ReleaseError(f"input fingerprint is missing: {path}")
            self._fingerprint = read_json(path)
        return self._fingerprint

    @property
    def run_start(self):
        path = os.path.join(self.run_dir, RUN_RECORDS_DIR, "run-start.json")
        if not os.path.isfile(path):
            raise ReleaseError(f"run-start record is missing: {path}")
        return read_json(path)

    def identity_block(self) -> dict:
        start = self.run_start
        return {
            "run_id": os.path.basename(self.run_dir),
            "code": start.get("code"),
            "data": start.get("data"),
            "environment_digest": sha256_text(canonical_json(start.get("environment") or {})),
            "fingerprint_sha256": sha256_file(
                os.path.join(self.run_dir, RUN_PROVENANCE_DIR, "input-fingerprint.json")
            ),
            "run_start_sha256": sha256_file(os.path.join(self.run_dir, RUN_RECORDS_DIR, "run-start.json")),
        }


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #


def cmd_preflight(args) -> int:
    ctx = Context(args)
    checker = Checker("preflight", collect=True)
    checker.require("project repository exists", os.path.isdir(os.path.join(ctx.repo, "src", "livecell_audit")), ctx.repo)
    for relative in (
        "tools/build_review_site.py",
        "tools/check_review_site.py",
        "tools/make_previews.py",
        "tools/release_tools.py",
        "web/review/index.html",
        os.path.join("web", "review", "assets", "review.css"),
        os.path.join("web", "review", "assets", "review.js"),
        "requirements.txt",
        "README.md",
        "docs/12_release.md",
    ):
        checker.require(f"{relative} exists", os.path.isfile(os.path.join(ctx.repo, relative)))
    for split in ("train", "val", "test"):
        path = os.path.join(ctx.data_root, "annotations", f"livecell_coco_{split}.json")
        checker.require(f"raw annotation file exists: {split}", os.path.isfile(path), path)
    checker.require("raw images directory exists", os.path.isdir(os.path.join(ctx.data_root, "images")))
    checker.require("python interpreter exists", os.path.isfile(ctx.python), ctx.python)
    try:
        import duckdb  # noqa: F401
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import PIL  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:
        checker.require(f"runtime dependency importable ({exc})", False)
    else:
        checker.require("runtime dependencies import (duckdb, numpy, pandas, Pillow, scikit-learn)", True)
    node = shutil.which("node")
    if node:
        checker.require(
            "node is available for the site check's JavaScript syntax check", True, node
        )
    else:
        checker.warn(
            "node is not installed: the site check records its JavaScript syntax check as "
            "SKIPPED-missing-node and the release continues",
            "install node to restore node --check coverage; node is not required to release",
        )
    return emit_checks(checker, ctx, "preflight")


def cmd_fingerprint(args) -> int:
    ctx = Context(args)
    out_path = os.path.abspath(args.out)
    data_root = ctx.data_root
    coco = {}
    for split in ("train", "val", "test"):
        relative = os.path.join("annotations", f"livecell_coco_{split}.json")
        path = os.path.join(data_root, relative)
        if not os.path.isfile(path):
            raise ReleaseError(f"missing raw annotation file: {path}")
        digest = sha256_file(path)
        coco[split] = {"path": relative.replace(os.sep, "/"), "bytes": file_bytes(path), "sha256": digest}
        print(f"fingerprint: {relative} sha256={digest} bytes={file_bytes(path)}")
    if args.tiff_manifest is None:
        raise ReleaseError("--tiff-manifest is required with --tiff-identity")
    images_root = os.path.join(data_root, "images")
    if not os.path.isdir(images_root):
        raise ReleaseError(f"missing raw images directory: {images_root}")
    inventory = []
    full = []
    total_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(images_root):
        for name in sorted(filenames):
            full_path = os.path.join(dirpath, name)
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                continue
            relative = relpath_posix(images_root, full_path)
            size = file_bytes(full_path)
            total_bytes += size
            inventory.append(f"{relative}\t{size}")
            if ctx.tiff_identity:
                full.append((relative, size, sha256_file(full_path)))
    inventory.sort()
    identity = {
        "coco": {split: entry["sha256"] for split, entry in coco.items()},
        "images": {
            "count": len(inventory),
            "bytes_total": total_bytes,
            "inventory_sha256": sha256_text("\n".join(inventory)),
            "full_sha256": (
                sha256_text("\n".join(f"{rel}\t{size}\t{digest}" for rel, size, digest in full))
                if full
                else None
            ),
        },
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": utc_now(),
        "data_root_sha256": sha256_text(data_root),
        "data_root_basename": os.path.basename(os.path.normpath(data_root)),
        "coco": coco,
        "images": {
            "count": len(inventory),
            "bytes_total": total_bytes,
            "inventory_method": (
                "sha256 over sorted '<relative path>\\t<bytes>' lines of every file under "
                "the images root"
            ),
            "inventory_sha256": identity["images"]["inventory_sha256"],
            "tiff_content_hashed": bool(full),
            "tiff_content_method": (
                "sha256 of every image file under the data root (one pass, recorded in "
                "provenance/tiff-identity.json)"
                if full
                else "not computed in this run: COCO hashes and the image inventory cannot "
                "detect changed image pixels"
            ),
            "tiff_content_sha256": identity["images"]["full_sha256"],
        },
        "input_hash": {
            "sha256": sha256_text(canonical_json(identity)),
            "method": "sha256 over the canonical JSON of {coco hashes, image count/bytes/inventory hash}",
            "covers_tiff_pixels": bool(full),
        },
    }
    write_json(out_path, payload)
    print(f"fingerprint: {len(inventory)} image files, {total_bytes} bytes")
    print(f"fingerprint: input hash {payload['input_hash']['sha256']}")
    if full:
        identity_path = os.path.abspath(args.tiff_manifest)
        write_json(
            identity_path,
            {
                "schema_version": SCHEMA_VERSION,
                "created_utc": utc_now(),
                "method": "sha256 per file, read once, one file at a time",
                "file_count": len(full),
                "bytes_total": sum(size for _rel, size, _digest in full),
                "tree_sha256": payload["images"]["tiff_content_sha256"],
                "files": [
                    {"path": rel, "bytes": size, "sha256": digest}
                    for rel, size, digest in sorted(full)
                ],
            },
        )
        print(f"fingerprint: wrote {len(full)} per-image hashes to {identity_path}")
    return 0


def cmd_run_start(args) -> int:
    ctx = Context(args)
    ctx.require_run()
    start = {
        "schema_version": SCHEMA_VERSION,
        "run_id": os.path.basename(ctx.run_dir),
        "created_utc": utc_now(),
        "tool": {"name": "livecell-audit-release", "release_tools_schema": SCHEMA_VERSION},
        "code": code_identity(ctx.repo),
        "project": {
            "repo_dir_sha256": sha256_text(ctx.repo),
            "repo_basename": os.path.basename(ctx.repo),
            "python": ctx.python,
            "python_sha256": sha256_file(ctx.python) if os.path.isfile(ctx.python) else None,
        },
        "environment": environment_record(ctx.repo),
        "data": {
            "data_root_sha256": sha256_text(ctx.data_root),
            "data_root_basename": os.path.basename(os.path.normpath(ctx.data_root)),
            "fingerprint_sha256": sha256_file(os.path.join(ctx.run_dir, RUN_PROVENANCE_DIR, "input-fingerprint.json")),
            "tiff_identity_sha256": (
                sha256_file(os.path.join(ctx.run_dir, RUN_PROVENANCE_DIR, "tiff-identity.json"))
                if os.path.isfile(os.path.join(ctx.run_dir, RUN_PROVENANCE_DIR, "tiff-identity.json"))
                else None
            ),
        },
        "config": {
            "seed": ctx.seed,
            "budgets": list(ctx.budgets),
            "with_previews": ctx.with_previews,
            "tiff_identity": ctx.tiff_identity,
            "launcher": args.launcher,
        },
        "measurement": {
            "runtime_method": "time.perf_counter() around each stage command",
            "peak_memory_method": peak_memory_mb()["method"],
            "launcher_accounting": cgroup_info(),
        },
    }
    write_json(os.path.join(ctx.run_dir, RUN_RECORDS_DIR, "run-start.json"), start)
    print(json.dumps(
        {
            "run_id": start["run_id"],
            "git_head": (start["code"] or {}).get("git_head"),
            "git_dirty": (start["code"] or {}).get("git_dirty"),
            "python": (start["environment"] or {}).get("python_version"),
            "peak_memory_mb": start["measurement"]["launcher_accounting"].get("memory_peak_bytes"),
        },
        indent=2,
        sort_keys=True,
    ))
    return 0


def code_identity(repo: str) -> dict:
    def git(*args):
        try:
            result = subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    index = git("ls-files", "-s")
    fingerprint = None
    if index is not None:
        entries = sorted(line for line in index.splitlines() if line.strip())
        fingerprint = sha256_text("\n".join(entries))
    return {
        "git_head": head,
        "git_tree": git("rev-parse", "HEAD^{tree}"),
        "git_describe": git("describe", "--always", "--dirty"),
        "commit_subject": git("log", "-1", "--pretty=%s"),
        "git_dirty": bool(status),
        "git_status_sha256": sha256_text(status) if status is not None else None,
        "tracked_files": len(index.splitlines()) if index else None,
        "code_fingerprint_method": "sha256 over sorted 'git ls-files -s' lines (path + blob id)",
        "code_fingerprint_sha256": fingerprint,
    }


def environment_record(repo: str) -> dict:
    import platform

    def package_version(name):
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(name)
        except (PackageNotFoundError, ValueError):
            return None

    requirements = {}
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            requirements[name] = sha256_file(path)
    memory_total = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    memory_total = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor_count": os.cpu_count(),
        "mem_total_bytes": memory_total,
        "packages": {
            name: package_version(name)
            for name in ("duckdb", "numpy", "pandas", "Pillow", "scikit-learn", "scipy", "pytest")
        },
        "requirements_sha256": requirements,
        "launcher": {
            "name": "run-job",
            "declared_limits": (
                "1536 MiB RAM, 1024 MiB throttle, 256 MiB swap, 1 CPU, one job, "
                "64 tasks, 30-minute timeout"
            ),
            "observed_cgroup": cgroup_info(),
        },
    }


def cmd_stage(args) -> int:
    ctx = Context(args)
    ctx.require_run()
    if os.path.exists(os.path.join(ctx.run_dir, COMPLETION_MARKER)):
        raise ReleaseError("a completed run is immutable; start a new run instead")
    name = args.stage
    if name not in SPEC:
        raise ReleaseError(f"unknown stage {name!r}")
    spec = SPEC[name]
    if name == "previews" and not ctx.with_previews:
        raise ReleaseError("the previews stage is disabled; rerun with --with-previews")
    os.makedirs(os.path.join(ctx.run_dir, RUN_RECORDS_DIR), exist_ok=True)
    os.makedirs(os.path.join(ctx.run_dir, RUN_LOGS_DIR), exist_ok=True)
    if name in ("site-seed", "site", "site-check"):
        ensure_site_inputs(ctx)
    if name in ("site-seed", "site"):
        seed_site_shell(ctx, spec)

    started = utc_now()
    started_perf = time.perf_counter()

    checker = Checker(f"stage {name}")
    STAGE_INPUT_CHECK[name](ctx, checker)
    command = stage_command(spec, ctx)
    print(f"stage {name}: inputs verified ({len(checker.checks)} checks); running:")
    print("  " + " ".join(quote(part) for part in command))

    exit_code, log_file = run_logged(command, log_path(ctx.run_dir, name), cwd=ctx.repo)
    wall = time.perf_counter() - started_perf
    memory = peak_memory_mb()
    record = {
        "schema_version": SCHEMA_VERSION,
        "stage": name,
        "run_id": os.path.basename(ctx.run_dir),
        "status": "completed" if exit_code == 0 else "failed",
        "started_utc": started,
        "ended_utc": utc_now(),
        "wall_seconds": round(wall, 3),
        "peak_memory_mb": memory["peak_memory_mb"],
        "peak_memory_method": memory["method"],
        "cpu_seconds": memory["cpu_seconds"],
        "launcher_accounting": cgroup_info(),
        "exit_code": exit_code,
        "command": command,
        "cwd": ctx.repo,
        "config": spec.config,
        "identity": ctx.identity_block(),
        "checker_digest": canonical_json(checker.checks),
        "inputs": input_identity(ctx, spec),
        "cross_checks": checker.checks,
        "log": {"file": log_file, "sha256": sha256_file(log_file) if os.path.isfile(log_file) else None},
    }
    if exit_code != 0:
        record["errors"] = find_error_lines(log_file)
        write_json(record_path(ctx.run_dir, name), record)
        raise ReleaseError(
            f"stage {name!r} failed with exit code {exit_code}; log: {log_file}\n  "
            + "\n  ".join(record["errors"] or ["(no error line captured)"])
        )

    output_checker = Checker(f"stage {name} outputs")
    STAGE_OUTPUT_CHECK[name](ctx, output_checker)
    out_dir = stage_dir(ctx.run_dir, name)
    record["cross_checks"].extend(output_checker.checks)
    if spec.out_rel:
        tree = hash_tree(out_dir)
        record["outputs"] = {
            "dir": out_dir,
            "file_count": tree["file_count"],
            "bytes_total": tree["bytes_total"],
            "tree_sha256": tree["tree_sha256"],
            "hash_method": tree["hash_method"],
            "files": tree["files"],
        }
    if spec.marker:
        marker_path = os.path.join(out_dir, COMPLETION_MARKER)
        record["marker"] = {
            "file": COMPLETION_MARKER,
            "sha256": sha256_file(marker_path),
            "stage": (load_marker(out_dir) or {}).get("stage"),
        }
    write_json(record_path(ctx.run_dir, name), record)
    print(json.dumps(
        {
            "stage": name,
            "status": record["status"],
            "wall_seconds": record["wall_seconds"],
            "peak_memory_mb": record["peak_memory_mb"],
            "output_file_count": (record.get("outputs") or {}).get("file_count"),
            "output_bytes": (record.get("outputs") or {}).get("bytes_total"),
            "output_tree_sha256": (record.get("outputs") or {}).get("tree_sha256"),
            "log": log_file,
        },
        indent=2,
        sort_keys=True,
    ))
    return 0


def input_identity(ctx, spec) -> list:
    entries = []
    for name in stage_inputs(spec.name, ctx.with_previews):
        record = require_record(ctx.run_dir, name)
        entry = {
            "stage": name,
            "status": record.get("status"),
            "record_sha256": record_sha256(ctx.run_dir, name),
            "output_tree_sha256": (record.get("outputs") or {}).get("tree_sha256"),
            "marker_sha256": (record.get("marker") or {}).get("sha256"),
        }
        if name == "audit":
            entry["dir"] = os.path.join(ctx.run_dir, "audit")
        elif SPEC[name].out_rel:
            entry["dir"] = stage_dir(ctx.run_dir, name)
        entries.append(entry)
    return entries


def seed_site_shell(ctx, spec) -> None:
    """Copy the hand-written site shell into a fresh site build directory."""
    target = stage_dir(ctx.run_dir, spec.name)
    for relative in SITE_SHELL_FILES:
        source = os.path.join(ctx.repo, "web", "review", relative)
        if not os.path.isfile(source):
            raise ReleaseError(f"site shell file is missing from the repository: {source}")
        destination = os.path.join(target, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(source, destination)


def run_logged(command: list, log_file: str, cwd: str):
    """Run a stage command, streaming its output to stdout and the stage log."""
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    with open(log_file, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
        exit_code = process.wait()
    return exit_code, log_file


def quote(part: str) -> str:
    return part if re.fullmatch(r"[A-Za-z0-9_./:+,=-]+", part) else f"'{part}'"


def cmd_verify_chain(args) -> int:
    ctx = Context(args)
    ctx.require_run()
    completed = os.path.exists(os.path.join(ctx.run_dir, COMPLETION_MARKER))
    results = []
    failures = 0
    for name in active_stages(ctx.with_previews):
        spec = SPEC[name]
        for kind, func in (("input", STAGE_INPUT_CHECK[name]), ("output", STAGE_OUTPUT_CHECK[name])):
            checker = Checker(f"{name} {kind} checks", collect=True)
            try:
                func(ctx, checker)
            except ReleaseError as exc:
                checker.add(f"{name} {kind} checks raised", False, str(exc))
            for entry in checker.checks:
                results.append({"stage": name, "kind": kind, **entry})
                if not entry["ok"]:
                    failures += 1
        if spec.out_rel:
            out_dir = stage_dir(ctx.run_dir, name)
            tree = hash_tree(out_dir)
            record = require_record(ctx.run_dir, name)
            recorded = (record.get("outputs") or {}).get("tree_sha256")
            ok = recorded == tree["tree_sha256"]
            results.append(
                {
                    "stage": name,
                    "kind": "recorded-output-hash",
                    "check": f"{name}: recorded output tree hash still matches on disk",
                    "ok": ok,
                    "detail": f"recorded {recorded} vs now {tree['tree_sha256']}",
                }
            )
            if not ok:
                failures += 1
            if not completed:
                record["outputs"]["reverified_tree_sha256"] = tree["tree_sha256"]
                record["outputs"]["reverified_utc"] = utc_now()
                write_json(record_path(ctx.run_dir, name), record)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": os.path.basename(ctx.run_dir),
        "verified_utc": utc_now(),
        "checks": results,
        "check_count": len(results),
        "failures": failures,
        "ok": failures == 0,
        "with_previews": ctx.with_previews,
    }
    if not completed:
        write_json(os.path.join(ctx.run_dir, RUN_RECORDS_DIR, "chain-verification.json"), payload)
    print(json.dumps({"checks": len(results), "failures": failures}, indent=2, sort_keys=True))
    if failures:
        for entry in results:
            if not entry["ok"]:
                print(f"  FAIL {entry['stage']}/{entry['kind']}: {entry['check']} {entry.get('detail') or ''}")
        raise ReleaseError(f"{failures} cross-stage check(s) failed; see records/chain-verification.json")
    return 0


def cmd_run_complete(args) -> int:
    ctx = Context(args)
    ctx.require_run()
    if os.path.isfile(os.path.join(ctx.run_dir, COMPLETION_MARKER)):
        raise ReleaseError(
            f"{ctx.run_dir} is already marked complete; a completed run is immutable "
            "(start a new run instead)"
        )
    verification_file = os.path.join(ctx.run_dir, RUN_RECORDS_DIR, "chain-verification.json")
    if not os.path.isfile(verification_file):
        raise ReleaseError(
            f"no cross-stage verification found ({verification_file}); run "
            "'verify-chain' before marking the run complete"
        )
    verification = read_json(verification_file)
    if not verification.get("ok"):
        raise ReleaseError("the cross-stage verification did not pass; refusing to mark the run complete")
    stages = []
    for name in active_stages(ctx.with_previews):
        record = require_record(ctx.run_dir, name)
        outputs = record.get("outputs") or {}
        stages.append(
            {
                "stage": name,
                "status": record["status"],
                "started_utc": record["started_utc"],
                "ended_utc": record["ended_utc"],
                "wall_seconds": record["wall_seconds"],
                "peak_memory_mb": record["peak_memory_mb"],
                "peak_memory_method": record["peak_memory_method"],
                "exit_code": record["exit_code"],
                "command": record["command"],
                "config": record["config"],
                "record_sha256": record_sha256(ctx.run_dir, name),
                "marker_sha256": (record.get("marker") or {}).get("sha256"),
                "launcher_accounting": record.get("launcher_accounting"),
                "output_tree_sha256": outputs.get("tree_sha256"),
                "output_file_count": outputs.get("file_count"),
                "output_bytes": outputs.get("bytes_total"),
            }
        )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "release": {
            "name": "livecell-audit-release",
            "run_id": os.path.basename(ctx.run_dir),
            "runner": "tools/run_release.sh",
            "driver": "tools/release_tools.py",
            "completed_utc": utc_now(),
            "with_previews": ctx.with_previews,
            "launcher": "run-job (no container; docker/podman/buildah unavailable on the data server)",
        },
        "identity": ctx.identity_block(),
        "code": code_identity(ctx.repo),
        "environment": environment_record(ctx.repo),
        "data": {
            "fingerprint": ctx.fingerprint,
            "tiff_identity_sha256": ctx.run_start.get("data", {}).get("tiff_identity_sha256"),
        },
        "stages": stages,
        "verification": {
            "chain": {
                "check_count": verification["check_count"],
                "failures": verification["failures"],
                "verified_utc": verification["verified_utc"],
                "record_sha256": sha256_file(os.path.join(ctx.run_dir, RUN_RECORDS_DIR, "chain-verification.json")),
            },
            "site_check": {
                "log_sha256": (load_record(ctx.run_dir, "site-check") or {}).get("log", {}).get("sha256"),
                "log_file": log_path(ctx.run_dir, "site-check"),
            },
        },
        "limitations": list(LIMITATIONS),
        "deferred": list(DEFERRED),
        "reference_run": copy.deepcopy(REFERENCE_RUN),
        "attribution": list(ATTRIBUTION),
        "licenses": {
            "dataset_images_and_annotations": "CC BY-NC 4.0 (Sartorius Corporate Research / Edlund et al.)",
            "upstream_software": "MIT (sartorius-research/LIVECell)",
            "this_repository": "non-commercial research, portfolio and educational use",
        },
        "exclusions": {
            "raw_images_and_coco": True,
            "duckdb_database": True,
            "model_binaries": True,
            "parquet": True,
            "absolute_or_private_paths": True,
            "secrets": True,
        },
    }
    write_json(os.path.join(ctx.run_dir, RUN_RECORDS_DIR, "release-receipt.json"), receipt)
    marker = {
        "status": "completed",
        "stage": "release-run",
        "run_id": receipt["release"]["run_id"],
        "tool": {"name": "livecell-audit-release", "release_tools_schema": SCHEMA_VERSION},
        "completed_utc": receipt["release"]["completed_utc"],
        "stages": [entry["stage"] for entry in stages],
        "with_previews": ctx.with_previews,
        "input_hash": ctx.fingerprint["input_hash"]["sha256"],
        "coco_sha256": {split: entry["sha256"] for split, entry in ctx.fingerprint["coco"].items()},
        "chain_verification_sha256": receipt["verification"]["chain"]["record_sha256"],
        "release_receipt": {
            "file": os.path.join(RUN_RECORDS_DIR, "release-receipt.json"),
            "sha256": sha256_file(os.path.join(ctx.run_dir, RUN_RECORDS_DIR, "release-receipt.json")),
        },
    }
    tmp = os.path.join(ctx.run_dir, COMPLETION_MARKER + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(marker, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, os.path.join(ctx.run_dir, COMPLETION_MARKER))
    print(json.dumps(marker, indent=2, sort_keys=True))
    return 0


def cmd_list(args) -> int:
    out_root = os.path.abspath(args.out_root)
    rows = []
    seen = set()
    # A run root is either the output root itself (`<out>/<run id>`) or its `runs/`
    # subdirectory (older layout); both are accepted so listing never hides a run.
    for base in (out_root, os.path.join(out_root, "runs")):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            run_dir = os.path.join(base, name)
            records_dir = os.path.join(run_dir, RUN_RECORDS_DIR)
            if not os.path.isdir(run_dir) or name in seen:
                continue
            if not (os.path.isfile(os.path.join(run_dir, COMPLETION_MARKER)) or os.path.isdir(records_dir)):
                continue
            seen.add(name)
            marker = load_marker(run_dir)
            stages = sorted(
                entry[len("stage-"):-len(".json")]
                for entry in os.listdir(records_dir)
                if entry.startswith("stage-") and entry.endswith(".json")
            ) if os.path.isdir(records_dir) else []
            failed = os.path.isfile(os.path.join(records_dir, "run-failed.json"))
            rows.append(
                {
                    "run_id": name,
                    "path": run_dir,
                    "status": "completed" if marker else ("failed" if failed else "incomplete"),
                    "stages": stages,
                    "with_previews": bool(marker and marker.get("with_previews")),
                }
            )
    print(json.dumps({"out_root": out_root, "runs": rows}, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# packaging
# --------------------------------------------------------------------------- #

#: Files copied into the release package from each finished run.
PACKAGE_TABLES = (
    ("ranking", "summary.json"),
    ("ranking", "model_card.json"),
    ("ranking", "partition_manifest.json"),
    ("ranking", "fit_reference.json"),
    ("ranking", "candidates.csv"),
    ("ranking", "version_provenance.jsonl"),
    ("evaluation", "evaluation_summary.json"),
    ("evaluation", "known_cases.json"),
    ("evaluation", "known_cases.csv"),
    ("evaluation", "synthetic_annotation_loss.json"),
    ("evaluation", "synthetic_paired_deltas.csv"),
    ("evaluation", "review_sample.json"),
    ("evaluation", "review_sample.csv"),
)

PACKAGE_DOCS = (
    "README.md",
    "docs/09_data_contract.md",
    "docs/09_model_card.md",
    "docs/10_ranking_evaluation.md",
    "docs/11_review_interface.md",
    "docs/12_release.md",
)


def sanitize_text(text: str, ctx) -> str:
    """Replace absolute paths with placeholders (never ship a machine path)."""
    replacements = [
        (ctx.run_dir, "<RUN_DIR>"),
        (ctx.out_root, "<OUT_ROOT>"),
        (ctx.data_root, "<DATA_ROOT>"),
        (os.path.dirname(ctx.python), "<VENV>"),
        (ctx.repo, "<REPO>"),
    ]
    for needle, replacement in sorted(replacements, key=lambda pair: -len(pair[0])):
        if needle:
            text = text.replace(needle, replacement)
    return GENERIC_ABS_PATH_RE.sub("<ABSOLUTE_PATH>", text)


def sanitize_document_text(text: str, ctx) -> str:
    """Sanitize a packaged document: machine paths, hosts and private names."""
    text = sanitize_text(text, ctx)
    text = SSH_TARGET_RE.sub("<ssh-target>", text)
    for needle, replacement in DOC_REDACTIONS:
        text = text.replace(needle, replacement)
    return text


def sanitize_command(command: list, ctx) -> list:
    return [sanitize_text(part, ctx) for part in command]


#: Placeholder for an absolute path that no specific replacement rule matched.
GENERIC_PATH_PLACEHOLDER = "<ABSOLUTE_PATH>"


def current_account() -> str:
    """Best-effort account name of the machine writing the receipt."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))


def receipt_redaction_rules(ctx) -> list:
    """Ordered ``(needle, placeholder, kind)`` rules for a portable receipt.

    Longest needle first, so a nested path takes its most specific placeholder
    (the run directory before the repository root). Every rule that fires is
    recorded in the receipt's ``redaction_map`` with the SHA-256 of the withheld
    value, so the redaction is auditable without publishing the machine value.
    """
    account = current_account()
    rules = [
        (ctx.run_dir, "<run>", "release run directory"),
        (ctx.out_root, "<out-root>", "output root"),
        (ctx.data_root, "<data-root>", "raw data root"),
        (os.path.dirname(ctx.python), "<venv>", "project virtualenv directory"),
        (ctx.repo, "<repo>", "repository root"),
        (os.path.expanduser("~"), "<home>", "account home directory"),
        (account, "<user>", "account name"),
    ]
    rules.extend(
        (needle, replacement, "private environment name")
        for needle, replacement in DOC_REDACTIONS
    )
    # A needle shorter than a few characters, or a bare root, would corrupt
    # unrelated text; the account rule additionally needs to be distinctive.
    usable = [
        rule
        for rule in rules
        if rule[0] and rule[0].strip("/") and len(rule[0]) >= 3
    ]
    return sorted(usable, key=lambda rule: -len(rule[0]))


def _redact_string(text: str, rules: list, applied: dict) -> str:
    # ``user@host`` first: a rule like the account name must not mangle the
    # local part into a shape the SSH-target pattern can no longer match.
    targets = sorted(set(SSH_TARGET_RE.findall(text)))
    if targets:
        text = SSH_TARGET_RE.sub("<ssh-target>", text)
        applied.setdefault(
            "<ssh-target>",
            {"kind": "SSH target", "sha256": sha256_text("\n".join(targets))},
        )
    for needle, placeholder, kind in rules:
        if needle in text:
            text = text.replace(needle, placeholder)
            applied.setdefault(placeholder, {"kind": kind, "sha256": sha256_text(needle)})
    if GENERIC_ABS_PATH_RE.search(text):
        text = GENERIC_ABS_PATH_RE.sub(GENERIC_PATH_PLACEHOLDER, text)
        applied.setdefault(
            GENERIC_PATH_PLACEHOLDER,
            {"kind": "absolute path matched by the generic rule", "sha256": None},
        )
    return text


def _redact_payload(value, rules: list, applied: dict):
    if isinstance(value, str):
        return _redact_string(value, rules, applied)
    if isinstance(value, list):
        return [_redact_payload(item, rules, applied) for item in value]
    if isinstance(value, dict):
        return {
            (_redact_string(key, rules, applied) if isinstance(key, str) else key):
            _redact_payload(item, rules, applied)
            for key, item in value.items()
        }
    return value


def redact_machine_values(payload: dict, ctx) -> dict:
    """Return a portable copy of a public payload, with a redaction map.

    Machine-specific absolute paths, the account name and any ``user@host``
    target are replaced by portable placeholders (``<repo>``, ``<run>``,
    ``<home>``, ``<user>``, ...). ``redaction_map`` records every placeholder
    that was used, what it stands for and the SHA-256 of the withheld value, so
    the receipt stays auditable without embedding the machine value. All hashes,
    versions and timings in the payload are left untouched.
    """
    rules = receipt_redaction_rules(ctx)
    applied: dict = {}
    redacted = _redact_payload(payload, rules, applied)
    redacted["redaction_map"] = {name: applied[name] for name in sorted(applied)}
    return redacted


def verify_completed_run(ctx) -> dict:
    """Recheck the frozen receipt and artifacts before copying a release.

    A completion marker describes the files at completion time. It is not
    sufficient evidence that those files have remained unchanged since then.
    This check is read-only so repackaging preserves that original evidence.
    """
    marker = require_marker(ctx.run_dir, "release-run")
    checker = Checker("completed release")
    checker.require("run id matches", marker.get("run_id") == os.path.basename(ctx.run_dir))

    def require_hash(relative, expected):
        path = os.path.join(ctx.run_dir, relative)
        checker.require(f"{relative} exists", os.path.isfile(path))
        checker.require(f"{relative} hash matches completion evidence", sha256_file(path) == expected)

    receipt_relative = os.path.join(RUN_RECORDS_DIR, "release-receipt.json")
    require_hash(receipt_relative, (marker.get("release_receipt") or {}).get("sha256"))
    chain_relative = os.path.join(RUN_RECORDS_DIR, "chain-verification.json")
    require_hash(chain_relative, marker.get("chain_verification_sha256"))
    try:
        receipt = read_json(os.path.join(ctx.run_dir, receipt_relative))
        chain = read_json(os.path.join(ctx.run_dir, chain_relative))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"unreadable completion evidence: {exc}") from exc
    checker.require("receipt is an object", isinstance(receipt, dict))
    checker.require("chain verification passed", isinstance(chain, dict) and chain.get("ok") is True)
    checker.require("input identity still matches", receipt.get("identity") == ctx.identity_block())
    names = list(active_stages(ctx.with_previews))
    checker.require("marker lists the complete chain", marker.get("stages") == names)
    stages = receipt.get("stages")
    checker.require("receipt lists the complete chain", isinstance(stages, list) and
                    all(isinstance(entry, dict) for entry in stages) and
                    [entry.get("stage") for entry in stages] == names)
    for entry in stages:
        name = entry["stage"]
        record = require_record(ctx.run_dir, name)
        require_hash(relpath_posix(ctx.run_dir, record_path(ctx.run_dir, name)), entry.get("record_sha256"))
        require_hash(relpath_posix(ctx.run_dir, log_path(ctx.run_dir, name)),
                     (record.get("log") or {}).get("sha256"))
        spec = SPEC[name]
        if spec.out_rel:
            output_dir = stage_dir(ctx.run_dir, name)
            for parent, dirs, files in os.walk(output_dir):
                for child in dirs + files:
                    checker.require(f"{name} output has no symlinks",
                                    not os.path.islink(os.path.join(parent, child)))
            tree = hash_tree(output_dir)
            checker.require(f"{name} outputs still match completion evidence",
                            tree["tree_sha256"] == entry.get("output_tree_sha256"))
        if spec.marker:
            require_hash(os.path.join(spec.out_rel, COMPLETION_MARKER), entry.get("marker_sha256"))
    return receipt


def cmd_package(args) -> int:
    ctx = Context(args)
    ctx.require_run()
    run_dir = ctx.run_dir
    if not os.path.isfile(os.path.join(run_dir, COMPLETION_MARKER)):
        raise ReleaseError(
            f"{run_dir} has no pipeline {COMPLETION_MARKER} marker: finish (and verify) the run "
            "before packaging it"
        )
    receipt = verify_completed_run(ctx)
    name = f"livecell-audit-release-{os.path.basename(run_dir)}"
    release_dir = os.path.join(run_dir, "release")
    staging = os.path.join(release_dir, "staging", name)
    zip_path = os.path.join(release_dir, f"{name}.zip")
    if os.path.exists(zip_path) and not args.force:
        raise ReleaseError(f"{zip_path} already exists; packaging never overwrites a release")
    if os.path.isdir(os.path.join(release_dir, "staging")):
        shutil.rmtree(os.path.join(release_dir, "staging"))
    os.makedirs(staging)

    site_src = os.path.join(run_dir, "site")
    site_dst = os.path.join(staging, "site")
    shutil.copytree(site_src, site_dst)
    for relative in SITE_SHELL_FILES:
        if not os.path.isfile(os.path.join(site_dst, relative)):
            raise ReleaseError(f"site shell file missing from the built site: {relative}")
    shutil.copyfile(
        os.path.join(site_src, "THIRD_PARTY_NOTICES.txt"),
        os.path.join(staging, "THIRD_PARTY_NOTICES.txt"),
    )

    for stage, relative in PACKAGE_TABLES:
        source = os.path.join(run_dir, stage, relative)
        if not os.path.isfile(source):
            raise ReleaseError(f"package table is missing: {source}")
        destination = os.path.join(staging, "tables", stage, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(source, destination)

    for relative in PACKAGE_DOCS:
        source = os.path.join(ctx.repo, relative)
        if not os.path.isfile(source):
            raise ReleaseError(
                f"packaged document is missing from the repository: {source} "
                "(restore it or update PACKAGE_DOCS)"
            )
        destination = os.path.join(staging, "docs", relative.removeprefix("docs/"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        write_text(destination, sanitize_document_text(read_text(source), ctx))

    provenance = os.path.join(staging, "provenance")
    os.makedirs(provenance, exist_ok=True)
    shutil.copyfile(
        os.path.join(run_dir, RUN_PROVENANCE_DIR, "input-fingerprint.json"),
        os.path.join(provenance, "input-fingerprint.json"),
    )
    tiff_identity = os.path.join(run_dir, RUN_PROVENANCE_DIR, "tiff-identity.json")
    if os.path.isfile(tiff_identity):
        shutil.copyfile(tiff_identity, os.path.join(provenance, "tiff-identity.json"))
    write_json(os.path.join(provenance, "environment.json"), public_environment(ctx))
    write_json(os.path.join(provenance, "code.json"), public_code(ctx))
    write_json(os.path.join(provenance, "run-receipt.json"), public_receipt(ctx, receipt))

    write_text(os.path.join(staging, "README.md"), release_readme(ctx, receipt))
    write_text(os.path.join(staging, "RELEASE_NOTES.md"), release_notes(ctx, receipt))
    write_text(os.path.join(staging, "DEPLOY.md"), DEPLOY_MD)
    write_text(os.path.join(staging, "DEMO.md"), DEMO_MD)

    public = public_receipt(
        ctx,
        receipt,
        extra={
            "package": {
                "name": f"{name}.zip",
                "checksums_file": "checksums.sha256",
                "note": (
                    "checksums.sha256 lists the SHA-256 of every packaged file; the archive's own "
                    f"SHA-256 is published beside it as {name}.zip.sha256 and recorded with the exact "
                    "file count and byte total in records/package.json of the run directory"
                ),
            }
        },
    )
    write_json(os.path.join(staging, "RELEASE_RECEIPT.json"), public)

    write_checksums(staging)
    scan_public_tree(staging, ctx)
    zip_deterministic(staging, zip_path, name)
    zip_digest = sha256_file(zip_path)
    write_text(zip_path + ".sha256", f"{zip_digest}  {os.path.basename(zip_path)}\n")

    problems = verify_zip(zip_path, ctx)
    staged_files = list_tree(staging)
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": os.path.basename(run_dir),
        "created_utc": utc_now(),
        "zip": {
            "file": zip_path,
            "bytes": file_bytes(zip_path),
            "sha256": zip_digest,
            "sha256_file": os.path.basename(zip_path) + ".sha256",
        },
        "contents": {
            "file_count": len(staged_files),
            "bytes_total": sum(file_bytes(os.path.join(staging, rel)) for rel in staged_files),
        },
        "verification": {"ok": not problems, "problems": problems},
        "excluded": sorted(EXCLUDED_SUFFIXES),
    }
    write_json(os.path.join(run_dir, RUN_RECORDS_DIR, "package.json"), record)
    print(json.dumps(
        {"zip": zip_path, "bytes": record["zip"]["bytes"], "sha256": zip_digest, "verified": not problems},
        indent=2,
        sort_keys=True,
    ))
    if problems:
        raise ReleaseError("packaged release failed verification: " + "; ".join(problems[:5]))
    return 0


def public_environment(ctx) -> dict:
    start = ctx.run_start
    environment = dict(start.get("environment") or {})
    environment["note"] = (
        "Absolute machine paths are withheld from the release; hashes of the withheld "
        "paths are recorded in the run record instead."
    )
    return environment


def public_code(ctx) -> dict:
    return ctx.run_start.get("code") or {}


def public_receipt(ctx, receipt, extra: dict | None = None) -> dict:
    stages = []
    for entry in receipt["stages"]:
        stages.append(
            {
                "stage": entry["stage"],
                "status": entry["status"],
                "started_utc": entry["started_utc"],
                "ended_utc": entry["ended_utc"],
                "wall_seconds": entry["wall_seconds"],
                "peak_memory_mb": entry["peak_memory_mb"],
                "peak_memory_method": entry["peak_memory_method"],
                "launcher_accounting": entry.get("launcher_accounting"),
                "command": sanitize_command(entry["command"], ctx),
                "config": entry["config"],
                "record_sha256": entry["record_sha256"],
                "marker_sha256": entry["marker_sha256"],
                "output_tree_sha256": entry["output_tree_sha256"],
                "output_file_count": entry["output_file_count"],
                "output_bytes": entry["output_bytes"],
            }
        )
    fingerprint = receipt["data"]["fingerprint"]
    public = {
        "schema_version": SCHEMA_VERSION,
        "release": receipt["release"],
        "identity": {
            "run_id": receipt["identity"]["run_id"],
            "fingerprint_sha256": receipt["identity"]["fingerprint_sha256"],
            "run_start_sha256": receipt["identity"]["run_start_sha256"],
            "environment_digest": receipt["identity"]["environment_digest"],
            "code_at_run_start": receipt["identity"].get("code"),
            "code_fingerprint_sha256": (receipt["code"] or {}).get("code_fingerprint_sha256"),
            "git_head": (receipt["code"] or {}).get("git_head"),
            "git_dirty": (receipt["code"] or {}).get("git_dirty"),
            "commit_subject": (receipt["code"] or {}).get("commit_subject"),
        },
        "inputs": {
            "coco": {
                split: {"path": entry["path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
                for split, entry in sorted(fingerprint["coco"].items())
            },
            "images": fingerprint["images"],
            "input_hash": fingerprint["input_hash"],
        },
        "environment": public_environment(ctx),
        "stages": stages,
        "verification": receipt["verification"],
        "limitations": receipt["limitations"],
        "deferred": receipt["deferred"],
        "attribution": receipt["attribution"],
        "licenses": receipt["licenses"],
        "exclusions": receipt["exclusions"],
        "redactions": {
            "absolute_paths": "replaced by <ABSOLUTE_PATH> placeholders in packaged documents",
            "ssh_targets": "replaced by <ssh-target> in packaged documents",
            "path_hashes": "SHA-256 of the withheld machine paths are recorded in the run records",
            "receipt_fields": (
                "machine-specific fields of this receipt (the repository, data and output roots, "
                "the run directory, the account home/name and any SSH target) are replaced by "
                "portable <placeholder> tokens; redaction_map below names each placeholder and "
                "records the SHA-256 of the withheld value"
            ),
        },
    }
    if extra:
        public.update(extra)
    # Always ship the verified reference run: a run record written before this field
    # existed still produces a receipt that names the run which proved the tooling.
    public["reference_run"] = copy.deepcopy(receipt.get("reference_run") or REFERENCE_RUN)
    return redact_machine_values(public, ctx)


def release_readme(ctx, receipt) -> str:
    return f"""# LIVECell audit — portable local release

This ZIP is a self-contained, static artifact built from one verified release run of the
LIVECell audit pipeline (audit → warehouse → features → rank → evaluate → site).
It needs no server, no backend, no container and no network.

**Run ID:** `{receipt['release']['run_id']}`
**Built:** {receipt['release']['completed_utc']}
**Source revision:** `{(receipt.get('code') or {}).get('git_head')}` (dirty: {bool((receipt.get('code') or {}).get('git_dirty'))})

## Open the site

    python3 -m http.server --directory site 8321     # → http://localhost:8321/
    # or open site/index.html directly (file:// works; relative assets only)

## What is in here

| Path | Contents |
| --- | --- |
| `site/` | the static review website (overview, illustrated examples, full rankings, method comparison, downloads) |
| `tables/ranking/` | ranking summary, model card, partition manifest, fit reference, candidates CSV, version provenance |
| `tables/evaluation/` | evaluation summary, known cases, synthetic annotation-loss results, review sample (blank judgments) |
| `docs/` | the repository README and the methodology/model-card/release documents |
| `provenance/` | input fingerprint (COCO hashes + image inventory), optional per-image TIFF hashes, environment, code identity, run receipt |
| `RELEASE_RECEIPT.json` | machine-readable receipt: run ID, per-stage hashes, timings, peak memory, exclusions |
| `checksums.sha256` | SHA-256 of every file in this package |
| `THIRD_PARTY_NOTICES.txt` | attribution, licences and the change notice for the rendered previews |

## Verified end to end

One completed run of this tooling was verified and is recorded here as the reference
run: its run ID, chain verification, input fingerprint, audit/warehouse counts, the
evaluation↔ranking manifest match, the review-site check result, the hash-verified
previews, per-stage peak memory with the launcher's cgroup and swap behaviour, and the
packaged archive's size and SHA-256. It appears under `reference_run` in `RELEASE_RECEIPT.json` and in
`docs/12_release.md` §5.

## What this release does not claim

No real-world precision or recall, no expert scientific review, no model superiority, no
biological inference, and no affiliation with Sartorius. See `RELEASE_NOTES.md` and the
model card for the honest limits. Raw images, the COCO annotations, the DuckDB database,
Parquet exports and model binaries are deliberately **not** included.
"""


def release_notes(ctx, receipt) -> str:
    lines = [
        "# Release notes",
        "",
        f"Run ID `{receipt['release']['run_id']}`, built {receipt['release']['completed_utc']}.",
        "",
        "## Verified in this release",
        "",
        f"- Cross-stage checks: {receipt['verification']['chain']['check_count']} checks, "
        f"{receipt['verification']['chain']['failures']} failures.",
        "- Every stage wrote a content manifest and a `COMPLETED` marker; the wrapper "
        "re-verified each stage's recorded output tree hash after the run.",
        "- The evaluation stage's recorded ranking-manifest hash equals the ranking "
        "manifest hash of the run it was packaged with.",
        "- The review website's own static checker (`tools/check_review_site.py`) reported "
        "zero failures over the built site.",
        "- Per-stage wall time and peak memory were measured inside the capped launcher job "
        "(child peak RSS plus the job's cgroup memory values), with the measurement method "
        "recorded per stage.",
        "- Every file in the ZIP was hashed, and the ZIP was unzipped to a temporary "
        "directory where every relative link, asset and download was re-resolved.",
        "",
        "## Reference verified run",
        "",
    ]
    reference = receipt.get("reference_run") or REFERENCE_RUN
    chain = reference.get("chain_verification") or {}
    inputs = reference.get("inputs") or {}
    audit = reference.get("audit") or {}
    warehouse = reference.get("warehouse") or {}
    evaluation = reference.get("evaluation") or {}
    site = reference.get("site_check") or {}
    previews = reference.get("previews") or {}
    peaks = reference.get("stage_peak_memory_mb") or {}
    package = reference.get("package") or {}
    lines.append(
        f"The one completed end-to-end run this tooling ships against, independently "
        f"verified, is `{reference.get('run_id')}`; the same facts are recorded in "
        "`docs/12_release.md` §5."
    )
    lines.append("")
    lines.append(
        f"- Chain verification: {chain.get('checks')} checks, {chain.get('failures')} failures."
    )
    lines.append(
        f"- Input fingerprint: the raw COCO files' SHA-256, plus "
        f"{inputs.get('images_fingerprinted'):,} raw images fingerprinted."
    )
    lines.append(
        f"- Audit: {audit.get('image_records'):,} image records, "
        f"{audit.get('annotations'):,} annotations, "
        f"{audit.get('distinct_filenames'):,} distinct filenames."
    )
    lines.append(
        f"- Warehouse: schema v{warehouse.get('schema_version')}, "
        f"views v{warehouse.get('view_version')}."
    )
    lines.append(
        f"- Evaluation consumed ranking manifest `{evaluation.get('ranking_manifest_sha256')}` "
        f"({evaluation.get('match')})."
    )
    lines.append(
        f"- Review-site check: {site.get('checks')} checks, {site.get('failures')} failures"
        + (f", {site.get('skipped')} skipped ({site.get('skipped_reason')})"
           if site.get("skipped") else "")
        + "."
    )
    lines.append(
        f"- Previews: {previews.get('hash_verified')}/{previews.get('rendered')} rendered "
        "previews hash-verified"
        + (f", plus {previews.get('label_layers')} full label layers for dense images"
           if previews.get("label_layers") else "")
        + "."
    )
    if peaks:
        approximate = set(reference.get("peak_memory_approximate_stages") or ())
        rendered = ", ".join(
            f"{name} {'~' if name in approximate else ''}{value} MB"
            for name, value in peaks.items()
        )
        lines.append(
            f"- Peak memory per stage ({reference.get('peak_memory_method')}): {rendered}."
        )
    if reference.get("launcher_cgroup"):
        lines.append(f"- Launcher cgroup: {reference.get('launcher_cgroup')}.")
    lines.append(
        f"- Packaged archive: `{package.get('file')}`, {package.get('bytes'):,} bytes, "
        f"SHA-256 `{package.get('sha256')}`, verified: {package.get('verified')}."
    )
    lines.extend(
        [
            "",
            "## Verified by the project, not by outside review",
            "",
        ]
    )
    lines.extend(f"- Not verified: {item}" for item in receipt["limitations"])
    lines.extend(["", "## Deferred (not part of this local release)", ""])
    lines.extend(f"- {item}" for item in receipt["deferred"])
    lines.extend(
        [
            "",
            "## Redactions in this package",
            "",
            "- Absolute machine paths in the packaged documentation are replaced by "
            "`<ABSOLUTE_PATH>`-style placeholders, and the private SSH target of the data "
            "server is replaced by `<ssh-target>`. The repository copies of those documents "
            "are unmodified; only the packaged copies are redacted.",
            (
                "- `RELEASE_RECEIPT.json` and `provenance/run-receipt.json` replace their "
                "machine-specific fields (repository, data and output roots, the run directory, "
                "the account home/name and any SSH target) with portable `<repo>` / `<run>` / "
                "`<home>` / `<user>`-style placeholders; every placeholder used is listed in the "
                "receipt's `redaction_map`, so the redaction is auditable."
            ),
            "- The run receipt records SHA-256 hashes of the withheld paths, so a reader can "
            "verify which paths were used without publishing them.",
        ]
    )
    lines.extend(["", "## Attribution", ""])
    lines.extend(f"- {item}" for item in receipt["attribution"])
    lines.extend(["", "## Licences", ""])
    for key, value in sorted(receipt["licenses"].items()):
        lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


DEPLOY_MD = """# Deploy / rollback (generic)

This release is a static folder: no runtime, no database, no API. The user's own website
integration is deferred, so these steps are intentionally generic.

## Local preview
```
python3 -m http.server --directory site 8321     # → http://localhost:8321/
```
`file://` also works; all assets are relative.

## Static hosting
1. Copy `site/` to the web root (or any nested subpath, e.g. `/projects/livecell-audit/`).
2. Keep the directory structure intact: `data/`, `downloads/`, `previews/` and `assets/`
   are referenced relatively.
3. No server configuration is required beyond serving the files; there is no redirect,
   no rewrite rule and no CORS requirement.

## Deployment checklist
- [ ] The site is served over HTTPS (or viewed locally) only.
- [ ] `THIRD_PARTY_NOTICES.txt` and `RELEASE_RECEIPT.json` are published next to `site/`.
- [ ] The dataset's CC BY-NC 4.0 terms are respected: non-commercial use only, attribution kept.

## Rollback
Hosts differ, but the safe pattern is to keep the previous deployment as a directory or
archive and restore it:
1. Rename the currently served folder (for example `site/` → `site-previous/`).
2. Copy the older release's `site/` back into place.
3. Verify one page loads and `THIRD_PARTY_NOTICES.txt` is reachable.

Nothing in this release writes to the host, so rollback is a file operation.
"""

DEMO_MD = """# Two-minute demo script

1. **Open `site/index.html`.** The overview states what the audit is and what it does not
   claim (no accuracy or health score).
2. **Show the audit numbers** (image records, distinct photos, cell markings) and the
   cross-split overlap count, and say plainly: a sparser label is a review candidate, not
   proven error.
3. **Open one illustrated example.** Toggle the annotation outlines; point at the PNG and
   source-TIFF SHA-256 in the detail panel — the picture is a bounded server-side render,
   not a claim about cell boundaries.
4. **Open the method comparison.** Low-count baseline vs robust z-score vs Isolation Forest
   at the same review budget: the simple rule recovered 9/9 previously reported sparse-label
   cases; the Isolation Forest did not, and the negative result is kept.
5. **Open the full ranking table.** Filter to the review pool; show that every row traces back
   to a ranked candidate with its ranks and features, and that `downloads/` carries the same
   numbers as CSV/JSON.
6. **Close on the limits.** The review sample ships with blank judgments: no scientific
   reviewer was supplied, so real-world precision is unmeasured. Then show
   `RELEASE_RECEIPT.json` for the run ID, per-stage hashes and timings.
"""


def write_checksums(root: str) -> None:
    lines = []
    for relative in list_tree(root):
        lines.append(f"{sha256_file(os.path.join(root, relative))}  {relative}")
    write_text(os.path.join(root, "checksums.sha256"), "\n".join(lines) + "\n")


def scan_public_tree(root: str, ctx) -> None:
    """Refuse to ship an excluded artifact, a private path or an absolute path."""
    problems = []
    literals = [ctx.repo, ctx.run_dir, ctx.out_root, ctx.data_root]
    for relative in list_tree(root):
        full = os.path.join(root, relative)
        lowered = relative.lower()
        for suffix in EXCLUDED_SUFFIXES:
            if lowered.endswith(suffix) and relative != "checksums.sha256":
                problems.append(f"excluded artifact type in the package: {relative}")
        if relative.endswith((".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2")):
            continue
        text = read_text(full)
        for literal in literals:
            if literal and literal in text:
                problems.append(f"{relative}: contains the machine path {literal!r}")
        for marker in PRIVATE_MARKERS:
            if marker in text:
                problems.append(f"{relative}: contains a private marker {marker!r}")
        for match in ABSOLUTE_PATH_RE.finditer(text):
            problems.append(f"{relative}: contains an absolute path ({match.group(0).strip()!r})")
    if problems:
        raise ReleaseError("release packaging refused: " + "; ".join(sorted(set(problems))[:5]))


def zip_deterministic(source_dir: str, zip_path: str, prefix: str) -> None:
    """Write a reproducible ZIP: sorted entries, fixed timestamps, deflated."""
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    timestamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for dirpath, dirnames, filenames in os.walk(source_dir):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                relative = relpath_posix(source_dir, full)
                info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(full, "rb") as handle:
                    archive.writestr(info, handle.read())


def portable_relative_path(name: str) -> bool:
    """Accept only unambiguous relative paths inside a portable package."""
    return bool(name) and not (
        name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name)
        or any(part in ("", ".", "..") for part in name.split("/"))
    )


def verify_zip(zip_path: str, ctx) -> list:
    """Verify the complete file inventory and resolve links after safe extraction."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="livecell-release-verify-") as tmp:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                seen = set()
                for info in archive.infolist():
                    name = info.filename.rstrip("/") if info.is_dir() else info.filename
                    if not portable_relative_path(name):
                        problems.append(f"unsafe archive path: {info.filename}")
                    if name in seen:
                        problems.append(f"duplicate archive path: {name}")
                    seen.add(name)
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        problems.append(f"archive entry is a symlink: {name}")
                    elif mode not in (0, 0o100000, 0o040000):
                        problems.append(f"archive entry is not a regular file or directory: {name}")
                if problems:
                    return problems
                archive.extractall(tmp)
        except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            return [f"cannot read release archive: {exc}"]
        root = tmp
        root_dirs = [name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))]
        if len(root_dirs) != 1 or len(os.listdir(root)) != 1:
            problems.append(f"archive should contain exactly one top-level directory, found {os.listdir(root)}")
            return problems
        package_root = os.path.join(root, root_dirs[0])
        try:
            scan_public_tree(package_root, ctx)
        except ReleaseError as exc:
            problems.append(str(exc))
        checksums = os.path.join(package_root, "checksums.sha256")
        if not os.path.isfile(checksums):
            problems.append("checksums.sha256 is missing from the package")
        else:
            listed = set()
            for line in read_text(checksums).splitlines():
                if not line.strip():
                    continue
                digest, _, relative = line.partition("  ")
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or not portable_relative_path(relative):
                    problems.append(f"malformed checksum entry: {line!r}")
                    continue
                if relative in listed:
                    problems.append(f"duplicate checksum entry: {relative}")
                listed.add(relative)
                full = os.path.join(package_root, relative)
                if not os.path.isfile(full):
                    problems.append(f"checksums list a missing file: {relative}")
                elif sha256_file(full) != digest:
                    problems.append(f"checksum mismatch inside the package: {relative}")
            actual = set(list_tree(package_root)) - {"checksums.sha256"}
            for relative in sorted(actual - listed):
                problems.append(f"package file is missing from checksums: {relative}")
            if not listed:
                problems.append("checksums.sha256 has no file entries")
        problems.extend(resolve_package_links(package_root))
        site = os.path.join(package_root, "site")
        for relative, global_name in (
            ("data/site-data.js", "LIVECELL_REVIEW"),
            ("data/rankings.js", "LIVECELL_RANKINGS"),
        ):
            try:
                load_js_global(os.path.join(site, relative), global_name)
            except (OSError, ReleaseError, ValueError) as exc:
                problems.append(f"{relative} does not parse: {exc}")
    return problems


LINK_ATTR_RE = re.compile(r'(?:href|src)="([^"]+)"')
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)")
#: JavaScript references, extracted exactly as tools/check_review_site.py does:
#: download names and preview paths. Provenance records inside the generated data
#: (which name inputs, not links) are deliberately not treated as links.
JS_DOWNLOAD_RE = re.compile(r"downloads/([A-Za-z0-9._-]+)")
JS_PREVIEW_RE = re.compile(r"['\"](previews/[A-Za-z0-9._-]+)['\"]")


def resolve_package_links(package_root: str) -> list:
    """Every relative reference in the packaged site must resolve inside it."""
    problems = []
    site = os.path.join(package_root, "site")
    for dirpath, dirnames, filenames in os.walk(package_root):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            lowered = name.lower()
            references = []
            if lowered.endswith(".html"):
                references = LINK_ATTR_RE.findall(read_text(full))
            elif lowered.endswith(".css"):
                references = CSS_URL_RE.findall(read_text(full))
            elif lowered.endswith(".js"):
                text = read_text(full)
                references = [
                    "downloads/" + name for name in JS_DOWNLOAD_RE.findall(text)
                ] + JS_PREVIEW_RE.findall(text)
            for reference in references:
                if not reference or reference.startswith(("#", "http:", "https:", "mailto:", "data:")):
                    continue
                if reference.startswith("/"):
                    problems.append(f"{relpath_posix(package_root, full)}: absolute link {reference!r}")
                    continue
                target = reference.split("#", 1)[0].split("?", 1)[0]
                candidates = [
                    os.path.realpath(os.path.join(os.path.dirname(full), target)),
                    os.path.realpath(os.path.join(site, target)),
                ]
                package_real = os.path.realpath(package_root)
                if not any(os.path.commonpath([package_real, candidate]) == package_real
                           and os.path.isfile(candidate) for candidate in candidates):
                    problems.append(
                        f"{relpath_posix(package_root, full)}: unresolved relative link {reference!r}"
                    )
    return problems


def cmd_verify_package(args) -> int:
    ctx = Context(args)
    zip_path = os.path.abspath(args.zip)
    if not os.path.isfile(zip_path):
        raise ReleaseError(f"release archive not found: {zip_path}")
    problems = verify_zip(zip_path, ctx)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "zip": zip_path,
        "bytes": file_bytes(zip_path),
        "sha256": sha256_file(zip_path),
        "verified_utc": utc_now(),
        "ok": not problems,
        "problems": problems,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if problems:
        raise ReleaseError("archive verification failed: " + "; ".join(problems[:5]))
    return 0


def emit_checks(checker: Checker, ctx, label: str, fail: bool = True) -> int:
    failed = [entry for entry in checker.checks if not entry["ok"]]
    warnings = [entry for entry in checker.checks if entry.get("warning")]
    payload = {
        "checker": label,
        "check_count": len(checker.checks),
        "failures": len(failed),
        "warnings": len(warnings),
        "checks": checker.checks,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if fail and failed:
        raise ReleaseError(
            f"{label} failed {len(failed)} check(s): "
            + "; ".join(f"{entry['check']}{(': ' + entry['detail']) if entry['detail'] else ''}" for entry in failed[:5])
        )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_common(parser, with_out_root=False):
    parser.add_argument("--run", help="release run directory")
    parser.add_argument("--data-root", default="data/raw", help="raw data root")
    parser.add_argument("--repo", default=None, help="repository root (default: this file's parent)")
    parser.add_argument("--python", default=sys.executable, help="project virtualenv python")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--budgets", type=int, nargs="+", default=[10, 25, 50])
    parser.add_argument("--with-previews", dest="with_previews", action="store_true", default=None)
    parser.add_argument("--no-previews", dest="with_previews", action="store_false")
    parser.add_argument("--tiff-identity", action="store_true")
    parser.add_argument("--launcher", default="run-job")
    if with_out_root:
        parser.add_argument("--out-root", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_tools.py",
        description="Release driver for the LIVECell pipeline (called by tools/run_release.sh).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCHEMA_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="check the environment and inputs")
    add_common(preflight, with_out_root=True)

    fingerprint = sub.add_parser("fingerprint", help="hash the raw inputs for the run ID")
    add_common(fingerprint, with_out_root=True)
    fingerprint.add_argument("--out", required=True, help="input fingerprint output file")
    fingerprint.add_argument("--tiff-manifest", default=None, help="per-image hash manifest output file")

    run_start = sub.add_parser("run-start", help="record environment, code and data identity")
    add_common(run_start, with_out_root=True)

    stage = sub.add_parser("stage", help="run one stage with record/cross-check discipline")
    add_common(stage, with_out_root=True)
    stage.add_argument("--stage", required=True, choices=sorted(SPEC))

    verify = sub.add_parser("verify-chain", help="re-verify every stage of a finished run")
    add_common(verify, with_out_root=True)

    complete = sub.add_parser("run-complete", help="write the pipeline marker and receipt")
    add_common(complete, with_out_root=True)

    package = sub.add_parser("package", help="assemble and verify the portable release ZIP")
    add_common(package, with_out_root=True)
    package.add_argument("--force", action="store_true", help="overwrite an existing archive")

    verify_package = sub.add_parser("verify-package", help="verify a release ZIP")
    add_common(verify_package)
    verify_package.add_argument("--zip", required=True)

    listing = sub.add_parser("list", help="list runs under an output root")
    add_common(listing, with_out_root=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "preflight": cmd_preflight,
        "fingerprint": cmd_fingerprint,
        "run-start": cmd_run_start,
        "stage": cmd_stage,
        "verify-chain": cmd_verify_chain,
        "run-complete": cmd_run_complete,
        "package": cmd_package,
        "verify-package": cmd_verify_package,
        "list": cmd_list,
    }
    try:
        return handlers[args.command](args)
    except ReleaseError as exc:
        print(f"release_tools: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("release_tools: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
