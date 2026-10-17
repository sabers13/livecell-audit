#!/usr/bin/env python3
"""Build the connected static report from audit outputs.

Reads the deterministic audit outputs (report.json, findings.json, manifests)
and the source TIFFs read-only, then writes the self-contained static page:

- web/report/data/report.js + findings.js (generated data as script globals,
  so the page works from file:// with no fetch and no build step);
- web/report/assets/example-shared.png + example-sparse.png (display copies
  of the exact example TIFFs named in the audit report);
- verifies the sparse-example polygon lies within the image bounds.

Example:
    PYTHONPATH=src python3 scripts/build_report.py \\
        --audit-dir /home/saber/Projects/firstmate/data/livecell-mvp/work/outputs \\
        --data-root /home/saber/projects/livecell-audit/data/raw \\
        --report-dir web/report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

try:  # single source of truth for the marker name when run with PYTHONPATH=src
    from livecell_audit.audit import COMPLETION_MARKER
except ImportError:  # pragma: no cover - fallback for a bare script run
    COMPLETION_MARKER = "COMPLETED"

#: Summary fields the completion marker must agree with ``report.json`` on.
_MARKER_SUMMARY_FIELDS = (
    "total_image_records",
    "total_annotations",
    "unique_filenames",
)


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _require_completed_run(audit_dir, report, sources):
    """Reject an audit directory that is not a validated completed run.

    Completion is not file existence alone: the ``COMPLETED`` marker must be
    present, parse as JSON, declare ``status == "completed"``, and link to the
    other outputs (source checksums and summary counts). Returns the parsed
    marker. Raises ``ValueError`` with an actionable message otherwise.
    """
    marker_path = os.path.join(audit_dir, COMPLETION_MARKER)
    if not os.path.isfile(marker_path):
        raise ValueError(
            f"no completion marker ({COMPLETION_MARKER}) in {audit_dir}: this is "
            "not a completed audit run. Rerun the audit to regenerate it "
            "rather than fabricating a marker."
        )
    try:
        marker = _load(marker_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable or truncated completion marker {marker_path}: {exc}")
    if not isinstance(marker, dict) or marker.get("status") != "completed":
        raise ValueError(f"completion marker {marker_path} does not declare a completed run")

    marker_sha = marker.get("source_sha256") or {}
    source_sha = {
        split: entry.get("sha256")
        for split, entry in (sources.get("coco_files") or {}).items()
    }
    if marker_sha != source_sha:
        raise ValueError(
            "completion marker source checksums do not match "
            "manifests/sources.json (mixed or stale outputs)"
        )
    for field in _MARKER_SUMMARY_FIELDS:
        if marker.get(field) != report.get(field):
            raise ValueError(
                f"completion marker {field!r} does not match report.json "
                "(mixed or stale outputs)"
            )
    return marker


def _write_js(path, global_name, payload):
    text = (
        "window."
        + global_name
        + " = "
        + json.dumps(payload, sort_keys=True)
        + ";\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _find_tiff(images_root, file_name):
    for dirpath, _dirnames, filenames in os.walk(images_root):
        if file_name in filenames:
            return os.path.join(dirpath, file_name)
    return None


def _tiff_to_png(tiff_path, png_path):
    from PIL import Image

    with Image.open(tiff_path) as img:
        size = (int(img.width), int(img.height))
        img.save(png_path, format="PNG")
    return size


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the connected static report.")
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args(argv)

    audit_dir = args.audit_dir
    try:
        report = _load(os.path.join(audit_dir, "report.json"))
        findings = _load(os.path.join(audit_dir, "findings.json"))
        sources = _load(os.path.join(audit_dir, "manifests", "sources.json"))
        _require_completed_run(audit_dir, report, sources)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # Reject incomplete/invalid evidence before touching any report output.
        print(f"build_report: error: {exc}", file=sys.stderr)
        return 2

    data_dir = os.path.join(args.report_dir, "data")
    assets_dir = os.path.join(args.report_dir, "assets")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(assets_dir, exist_ok=True)

    enriched = dict(report)
    enriched["source_checksums"] = sources.get("coco_files", {})
    _write_js(os.path.join(data_dir, "report.js"), "LIVECELL_REPORT", enriched)
    _write_js(os.path.join(data_dir, "findings.js"), "LIVECELL_FINDINGS", findings)

    images_root = os.path.join(args.data_root, "images")
    jobs = []
    if report["examples"].get("shared_image"):
        jobs.append((report["examples"]["shared_image"]["file_name"], "example-shared.png"))
    if report["examples"].get("sparse_labels"):
        jobs.append((report["examples"]["sparse_labels"]["file_name"], "example-sparse.png"))
    if not jobs:
        print("build_report: error: audit report names no examples", file=sys.stderr)
        return 2

    for file_name, png_name in jobs:
        tiff = _find_tiff(images_root, file_name)
        if tiff is None:
            print(f"build_report: error: missing local TIFF for {file_name}", file=sys.stderr)
            return 2
        png_path = os.path.join(assets_dir, png_name)
        width, height = _tiff_to_png(tiff, png_path)
        print(f"rendered {png_name} from {os.path.relpath(tiff, args.data_root)} "
              f"({width}x{height}) sha256={_sha256(png_path)[:16]}...")

    sparse = report["examples"].get("sparse_labels") or {}
    sample = sparse.get("sample_annotation") or {}
    width, height = 704, 520
    for poly in sample.get("segmentation") or []:
        xs = poly[0::2]
        ys = poly[1::2]
        if not xs or min(xs) < 0 or min(ys) < 0 or max(xs) > width or max(ys) > height:
            print("build_report: error: sparse polygon outside image bounds", file=sys.stderr)
            return 2
    print(f"polygon check ok: {len(sample.get('segmentation') or [])} polygon(s) "
          f"inside {width}x{height}")

    for name in ("report.js", "findings.js"):
        print(f"wrote data/{name} sha256={_sha256(os.path.join(data_dir, name))[:16]}...")
    print(f"findings embedded: {len(findings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
