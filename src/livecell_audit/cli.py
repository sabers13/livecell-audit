"""Command-line interface for the LIVECell audit, warehouse and feature stages.

Examples:
    PYTHONPATH=src python3 -m livecell_audit audit \\
        --data-root /home/saber/projects/livecell-audit/data/raw \\
        --out-dir /tmp/livecell-out

    PYTHONPATH=src python3 -m livecell_audit warehouse \\
        --audit-dir /tmp/livecell-out \\
        --out-dir data/workbench/warehouse

    PYTHONPATH=src python3 -m livecell_audit features \\
        --warehouse-dir data/workbench/warehouse \\
        --data-root /home/saber/projects/livecell-audit/data/raw \\
        --out-dir data/workbench/features
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .audit import SPLITS, AuditConfig, run_audit
from .errors import AuditError, FeatureError, WarehouseError
from .features import FeatureConfig, run_features
from .warehouse import WarehouseConfig, run_warehouse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livecell-audit",
        description=(
            "Stream LIVECell COCO splits, reconcile image files, report repeats "
            "and overlap, load a DuckDB warehouse, and build pandas features."
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

    warehouse = sub.add_parser(
        "warehouse",
        help="load a completed audit run into DuckDB and export Parquet",
    )
    warehouse.add_argument(
        "--audit-dir",
        required=True,
        help="a completed audit output directory (must contain COMPLETED)",
    )
    warehouse.add_argument(
        "--out-dir",
        required=True,
        help="directory receiving audit.duckdb, parquet/, integrity.json, run.json, COMPLETED",
    )
    warehouse.add_argument(
        "--data-root",
        default=None,
        help="raw data root; when given the COCO source files are re-hashed",
    )
    warehouse.add_argument(
        "--no-verify-source-hashes",
        action="store_true",
        help="skip re-hashing the raw COCO files (marker/sources cross-check still runs)",
    )
    warehouse.add_argument(
        "--threads",
        type=int,
        default=None,
        help="DuckDB thread count (default: DuckDB's choice)",
    )
    warehouse.add_argument(
        "--parquet-compression",
        default="zstd",
        choices=["zstd", "snappy", "gzip", "uncompressed"],
        help="Parquet compression codec (default: zstd)",
    )

    features = sub.add_parser(
        "features",
        help="build the pandas feature table from a completed warehouse",
    )
    features.add_argument(
        "--warehouse-dir",
        required=True,
        help="a completed warehouse output directory (must contain COMPLETED)",
    )
    features.add_argument(
        "--out-dir",
        required=True,
        help="directory receiving features.parquet, features.jsonl, summaries, COMPLETED",
    )
    features.add_argument(
        "--data-root",
        default=None,
        help="raw data root used to resolve TIFF paths (derived from the warehouse if omitted)",
    )
    features.add_argument(
        "--max-filenames",
        type=int,
        default=None,
        help="development only: read image statistics for at most N distinct filenames",
    )
    return parser


def _run_audit_command(args: argparse.Namespace) -> int:
    report = run_audit(
        AuditConfig(
            data_root=args.data_root,
            out_dir=args.out_dir,
            splits=tuple(args.splits),
            low_count_threshold=args.low_count_threshold,
            check_dimensions=not args.no_check_dimensions,
        )
    )
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


def _run_warehouse_command(args: argparse.Namespace) -> int:
    result = run_warehouse(
        WarehouseConfig(
            audit_dir=args.audit_dir,
            out_dir=args.out_dir,
            data_root=args.data_root,
            verify_source_hashes=not args.no_verify_source_hashes,
            threads=args.threads,
            parquet_compression=args.parquet_compression,
        )
    )
    integrity = result["integrity"]
    print(
        json.dumps(
            {
                "out_dir": result["out_dir"],
                "images": integrity["reconciliation"]["images"]["loaded"],
                "annotations": integrity["reconciliation"]["annotations"]["loaded"],
                "unique_filenames": integrity["reconciliation"]["unique_filenames"]["loaded"],
                "findings": integrity["reconciliation"]["findings"]["loaded"],
                "ambiguous_image_id_groups": len(
                    integrity["join_integrity"]["ambiguous_image_id_groups"]
                ),
                "orphan_annotation_records": integrity["join_integrity"][
                    "orphan_annotation_records"
                ],
                "duckdb_bytes": result["run"]["database_bytes"],
                "runtime_seconds": result["run"]["runtime_seconds"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run_features_command(args: argparse.Namespace) -> int:
    result = run_features(
        FeatureConfig(
            warehouse_dir=args.warehouse_dir,
            out_dir=args.out_dir,
            data_root=args.data_root,
            max_filenames=args.max_filenames,
        )
    )
    diagnostics = result["diagnostics"]
    print(
        json.dumps(
            {
                "out_dir": result["out_dir"],
                "feature_records": diagnostics["records"],
                "distinct_filenames": diagnostics["distinct_filenames_total"],
                "filenames_read": diagnostics["filenames_read"],
                "annotations_attached": diagnostics["annotation_total_attached"],
                "ambiguous_records": diagnostics["ambiguous_records"],
                "orphan_annotation_records": diagnostics["orphan_annotation_records"],
                "image_features_ok": diagnostics["image_features_ok"],
                "image_features_missing_file": diagnostics["image_features_missing_file"],
                "image_features_unreadable": diagnostics["image_features_unreadable"],
                "runtime_seconds": result["run"]["runtime_seconds"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            return _run_audit_command(args)
        if args.command == "warehouse":
            return _run_warehouse_command(args)
        if args.command == "features":
            return _run_features_command(args)
    except (AuditError, WarehouseError, FeatureError) as exc:
        print(f"livecell-audit: error: {exc}", file=sys.stderr)
        return 2
    print(f"livecell-audit: error: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
