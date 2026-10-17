"""Command-line interface for the LIVECell audit.

Example:
    PYTHONPATH=src python3 -m livecell_audit audit \\
        --data-root /home/saber/projects/livecell-audit/data/raw \\
        --out-dir /tmp/livecell-out
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .audit import SPLITS, AuditConfig, run_audit
from .errors import AuditError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livecell-audit",
        description=(
            "Stream LIVECell COCO splits, reconcile image files, report repeats "
            "and overlap, and write deterministic report data."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="run the audit over a data root")
    audit.add_argument(
        "--data-root",
        required=True,
        help="directory with annotations/livecell_coco_{train,val,test}.json and images/",
    )
    audit.add_argument(
        "--out-dir",
        required=True,
        help="directory receiving report.json, findings.json, tables/ and manifests/",
    )
    audit.add_argument(
        "--splits",
        nargs="+",
        default=list(SPLITS),
        choices=list(SPLITS),
        help="which splits to read (default: train val test)",
    )
    audit.add_argument(
        "--low-count-threshold",
        type=int,
        default=10,
        help="flag image records with at most this many annotations (default: 10)",
    )
    audit.add_argument(
        "--no-check-dimensions",
        action="store_true",
        help="skip opening local TIFFs to compare dimensions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_audit(
            AuditConfig(
                data_root=args.data_root,
                out_dir=args.out_dir,
                splits=tuple(args.splits),
                low_count_threshold=args.low_count_threshold,
                check_dimensions=not args.no_check_dimensions,
            )
        )
    except AuditError as exc:
        print(f"livecell-audit: error: {exc}", file=sys.stderr)
        return 2
    run = report.get("run", {})
    print(
        json.dumps(
            {
                "image_records": report["total_image_records"],
                "unique_filenames": report["unique_filenames"],
                "annotations": report["total_annotations"],
                "missing_files": len(report["missing_image_filenames"]),
                "train_val_overlap": report["cross_split_filename_overlap"]["train_val"][
                    "filenames"
                ],
                "low_count_records": len(report["low_annotation_records"]),
                "known_cases_recovered": (
                    f"{report['known_sparse_cases_recovered']}/"
                    f"{report['known_sparse_cases_total']}"
                ),
                "runtime_seconds": run.get("runtime_seconds"),
                "peak_memory_mb": run.get("peak_memory_mb"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
