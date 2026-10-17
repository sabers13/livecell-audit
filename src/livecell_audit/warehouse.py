"""Milestone 2: load completed audit exports into DuckDB and Parquet.

The warehouse consumes a *completed* audit output directory (the one produced
by ``livecell-audit audit``) and turns its JSONL tables and manifests into an
explicitly typed DuckDB database plus typed Parquet exports. It never touches
the raw dataset or the original audit outputs.

Contract
--------
An audit output directory is only consumed when its ``COMPLETED`` marker is
present, parses, and declares ``status == "completed"``. Before any table is
written the builder also reconciles the marker against ``manifests/sources.json``
(source SHA-256 per split) and against the actual record counts in
``tables/images.jsonl`` and ``tables/annotations.jsonl``. Any mismatch aborts
the build with :class:`~livecell_audit.errors.WarehouseError`.

Identity model
--------------
- *image record key* = ``(split, record_position)`` where ``record_position``
  is the record's position in that split's ``images`` array. This is the only
  key treated as unique.
- ``id`` (the original COCO image id) is a **field**, not a global key; it is
  scoped to a split and may be ambiguous (see below).
- *annotation version* = one image record plus the annotations pointing at it;
  several records may share one filename (alternate annotation versions).
- ``file_name`` is the *logical* image identity (one acquisition), never a
  claim of pixel identity.

Join safety
-----------
Annotations carry only ``(split, image_id)``. When two image records in the
same split share an ``id`` the reference is ambiguous: the warehouse does not
guess and does not multiply counts. Ambiguous groups and orphan annotations
(``image_id`` with no image record) are exposed by dedicated views and recorded
in ``integrity.json``; the feature stage quarantines ambiguous rows explicitly.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import duckdb

from . import __version__
from .completion import (
    COMPLETION_MARKER,
    STAGING_DIRNAME,
    clear_stale,
    count_lines,
    dump_json,
    load_completion_marker,
    promote_staged,
    read_json,
    sha256_file,
    write_completion_marker,
)
from .errors import WarehouseError

SCHEMA_NAME = "lc"
SCHEMA_VERSION = "1"
VIEW_VERSION = 1

#: Files the audit writes that the warehouse consumes.
IMAGES_REL = os.path.join("tables", "images.jsonl")
ANNOTATIONS_REL = os.path.join("tables", "annotations.jsonl")
FINDINGS_REL = "findings.json"
SOURCES_REL = os.path.join("manifests", "sources.json")
MEMBERSHIP_REL = os.path.join("manifests", "val_membership.json")
REPORT_REL = "report.json"
RUN_REL = "run.json"

#: Explicit JSON schemas -- the audit's JSONL is read with fixed column types
#: rather than inferred ones, so a schema drift becomes a load error.
IMAGES_COLUMNS: Dict[str, str] = {
    "split": "VARCHAR",
    "position": "BIGINT",
    "id": "BIGINT",
    "file_name": "VARCHAR",
    "original_filename": "VARCHAR",
    "cell_line": "VARCHAR",
    "well": "VARCHAR",
    "well_row": "VARCHAR",
    "well_col": "BIGINT",
    "field": "BIGINT",
    "timestamp": "VARCHAR",
    "time_hours": "DOUBLE",
    "crop": "BIGINT",
    "filename_parsed": "BOOLEAN",
    "width": "BIGINT",
    "height": "BIGINT",
    "local_path": "VARCHAR",
    "local_exists": "BOOLEAN",
    "local_width": "BIGINT",
    "local_height": "BIGINT",
    "dimensions_status": "VARCHAR",
    "dimensions_match": "BOOLEAN",
    "annotation_count": "BIGINT",
}

ANNOTATIONS_COLUMNS: Dict[str, str] = {
    "split": "VARCHAR",
    "id": "BIGINT",
    "image_id": "BIGINT",
    "area": "DOUBLE",
    "bbox": "DOUBLE[]",
    "n_points": "BIGINT",
}

FINDINGS_COLUMNS: Dict[str, str] = {
    "finding_id": "VARCHAR",
    "type": "VARCHAR",
    "status": "VARCHAR",
    "title": "VARCHAR",
    "file_name": "VARCHAR",
    "splits": "VARCHAR[]",
    "records": "JSON",
    "recommendation": "VARCHAR",
    "evidence": "JSON",
}

#: Base tables and the SQL views the warehouse publishes. View names carry the
#: :data:`VIEW_VERSION` prefix so a future revision can be added side by side.
VIEWS: Dict[str, str] = {
    "v1_image_annotation_stats": """
        -- Large-annotation aggregation in SQL: one row per (split, image_id).
        SELECT
            split,
            image_id,
            COUNT(*)                       AS n_annotations,
            SUM(area)                      AS area_sum,
            AVG(area)                      AS area_mean,
            median(area)                   AS area_median,
            MIN(area)                      AS area_min,
            MAX(area)                      AS area_max,
            stddev_samp(area)              AS area_std,
            AVG(bbox[3])                   AS bbox_width_mean,
            median(bbox[3])                AS bbox_width_median,
            AVG(bbox[4])                   AS bbox_height_mean,
            median(bbox[4])                AS bbox_height_median,
            AVG(n_points)                  AS n_points_mean,
            median(n_points)               AS n_points_median,
            MIN(n_points)                  AS n_points_min,
            MAX(n_points)                  AS n_points_max,
            SUM(n_points)                  AS n_points_sum
        FROM lc.annotations
        GROUP BY split, image_id
    """,
    "v1_split_membership": """
        -- Every image record's split membership with its record key.
        SELECT split, record_position, record_key, id AS image_id, file_name
        FROM lc.images
    """,
    "v1_split_overlap": """
        -- Filenames referenced by more than one split (per-filename view).
        WITH per_file AS (
            SELECT file_name, split, COUNT(*) AS record_count,
                   SUM(annotation_count) AS annotation_count
            FROM lc.images
            GROUP BY file_name, split
        )
        SELECT
            file_name,
            list_sort(list(DISTINCT split))                        AS splits,
            COUNT(DISTINCT split)                                  AS n_splits,
            SUM(record_count)                                      AS record_count,
            SUM(annotation_count)                                  AS annotation_count,
            SUM(CASE WHEN split = 'train' THEN record_count ELSE 0 END) AS train_records,
            SUM(CASE WHEN split = 'val'   THEN record_count ELSE 0 END) AS val_records,
            SUM(CASE WHEN split = 'test'  THEN record_count ELSE 0 END) AS test_records
        FROM per_file
        GROUP BY file_name
    """,
    "v1_annotation_versions": """
        -- One row per annotation-version image record, with version context.
        SELECT
            split,
            record_position,
            record_key,
            id AS image_id,
            file_name,
            annotation_count,
            COUNT(*) OVER (PARTITION BY split, file_name)                       AS versions_in_split,
            COUNT(*) OVER (PARTITION BY file_name)                              AS records_for_filename,
            COUNT(DISTINCT split) OVER (PARTITION BY file_name)                 AS splits_for_filename
        FROM lc.images
    """,
    "v1_alternate_versions": """
        -- Filenames carrying more than one annotation version within a split.
        SELECT
            split,
            file_name,
            COUNT(*)                                  AS version_count,
            list_sort(list(id ORDER BY id))            AS image_ids,
            list(record_position ORDER BY record_position) AS record_positions,
            list(annotation_count ORDER BY record_position) AS annotation_counts
        FROM lc.images
        GROUP BY split, file_name
        HAVING COUNT(*) > 1
    """,
    "v1_cell_line_summary": """
        -- Cell-line aggregates (metadata context, not biological growth).
        SELECT
            cell_line,
            COUNT(*)                                    AS image_records,
            COUNT(DISTINCT file_name)                   AS unique_filenames,
            SUM(annotation_count)                       AS annotations,
            AVG(annotation_count)                       AS mean_annotations,
            median(annotation_count)                    AS median_annotations,
            MIN(annotation_count)                       AS min_annotations,
            MAX(annotation_count)                       AS max_annotations,
            SUM(CASE WHEN annotation_count = 0 THEN 1 ELSE 0 END) AS zero_annotation_records
        FROM lc.images
        GROUP BY cell_line
    """,
    "v1_finding_lookup": """
        -- Retrieve a finding's original image/annotation evidence.
        SELECT
            f.record_position        AS finding_position,
            f.finding_id,
            f.type                   AS finding_type,
            f.status                 AS finding_status,
            f.file_name,
            f.splits                 AS finding_splits,
            i.split,
            i.record_position        AS image_record_position,
            i.record_key             AS image_record_key,
            i.id                     AS image_id,
            i.annotation_count,
            i.cell_line,
            i.dimensions_status
        FROM lc.findings f
        LEFT JOIN lc.images i
            ON i.file_name = f.file_name AND list_contains(f.splits, i.split)
    """,
    "v1_image_join_ambiguity": """
        -- (split, image_id) keys shared by more than one image record.
        SELECT
            split,
            id                                       AS image_id,
            COUNT(*)                                 AS record_count,
            list_sort(list(record_position))         AS record_positions,
            list_sort(list(file_name))               AS file_names
        FROM lc.images
        GROUP BY split, id
        HAVING COUNT(*) > 1
    """,
    "v1_orphan_annotations": """
        -- Annotation references with no image record in the same split.
        SELECT
            a.split,
            a.image_id,
            COUNT(*) AS annotation_count
        FROM lc.annotations a
        LEFT JOIN lc.images i
            ON i.split = a.split AND i.id = a.image_id
        WHERE i.id IS NULL
        GROUP BY a.split, a.image_id
    """,
}

#: Representative aggregation used to compare the database against raw JSONL.
_REPRESENTATIVE_DB_QUERY = """
    SELECT i.cell_line, SUM(s.n_annotations) AS annotations
    FROM lc.images i
    JOIN lc.v1_image_annotation_stats s
        ON s.split = i.split AND s.image_id = i.id
    GROUP BY i.cell_line
"""


@dataclass
class WarehouseConfig:
    """Inputs for one warehouse build."""

    audit_dir: str
    out_dir: str
    #: Raw data root. When given, the COCO source files are re-hashed and
    #: compared against the marker; when ``None`` only the marker/sources
    #: cross-check runs.
    data_root: Optional[str] = None
    verify_source_hashes: bool = True
    threads: Optional[int] = None
    parquet_compression: str = "zstd"


@dataclass
class AuditInputs:
    """Validated audit inputs handed to the build step."""

    audit_dir: str
    marker: Dict[str, Any]
    sources: Dict[str, Any]
    report: Dict[str, Any]
    run: Dict[str, Any]
    images_path: str
    annotations_path: str
    findings_path: str
    membership_path: str
    expected: Dict[str, Any]
    checks: List[Dict[str, Any]] = field(default_factory=list)


def _require_file(path: str, what: str) -> None:
    if not os.path.isfile(path):
        raise WarehouseError(f"missing {what}: {path}")


def _first_last_line(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return the first and last parsed JSON object of a JSONL file."""
    first = last = None
    with open(path, "rb") as handle:
        line = handle.readline()
        if line:
            first = _json_loads(line)
        # Seek back over the trailing newline to read the final line.
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return first, None
        block = min(size, 1 << 16)
        handle.seek(size - block)
        tail = handle.read()
        last_line = tail.rstrip(b"\n").split(b"\n")[-1]
        if last_line:
            last = _json_loads(last_line)
    return first, last


def _json_loads(raw: bytes) -> Any:
    import json

    return json.loads(raw.decode("utf-8"))


def validate_completed_audit(config: WarehouseConfig) -> AuditInputs:
    """Validate a completed audit directory before any table is written."""
    audit_dir = os.path.abspath(config.audit_dir)
    marker = load_completion_marker(audit_dir)
    if marker is None:
        raise WarehouseError(
            f"not a completed audit run (missing or invalid {COMPLETION_MARKER}): "
            f"{audit_dir}"
        )

    checks: List[Dict[str, Any]] = []
    checks.append({"check": "completion_marker", "status": "pass", "detail": "status=completed"})

    paths = {
        "images": os.path.join(audit_dir, IMAGES_REL),
        "annotations": os.path.join(audit_dir, ANNOTATIONS_REL),
        "findings": os.path.join(audit_dir, FINDINGS_REL),
        "sources": os.path.join(audit_dir, SOURCES_REL),
        "membership": os.path.join(audit_dir, MEMBERSHIP_REL),
        "report": os.path.join(audit_dir, REPORT_REL),
        "run": os.path.join(audit_dir, RUN_REL),
    }
    for name, path in paths.items():
        _require_file(path, f"audit {name} output")

    sources = read_json(paths["sources"])
    report = read_json(paths["report"])
    run = read_json(paths["run"])

    # --- source checksum reconciliation: marker vs sources.json ---------------
    marker_hashes = marker.get("source_sha256", {}) or {}
    source_files = (sources.get("coco_files") or {})
    if set(marker_hashes) != set(source_files):
        raise WarehouseError(
            "marker/sources split mismatch: "
            f"marker={sorted(marker_hashes)} sources={sorted(source_files)}"
        )
    for split, entry in source_files.items():
        if marker_hashes.get(split) != entry.get("sha256"):
            raise WarehouseError(
                f"source checksum mismatch for split {split!r}: "
                f"marker={marker_hashes.get(split)} sources={entry.get('sha256')}"
            )
    checks.append(
        {
            "check": "source_checksums_marker_vs_sources",
            "status": "pass",
            "detail": f"{len(source_files)} split(s) agree",
        }
    )

    # --- optional re-hash of the raw COCO files ------------------------------
    if config.verify_source_hashes and config.data_root:
        for split, entry in source_files.items():
            coco_path = os.path.join(
                config.data_root, "annotations", f"livecell_coco_{split}.json"
            )
            _require_file(coco_path, f"raw COCO file for split {split!r}")
            digest, size = sha256_file(coco_path)
            if digest != entry.get("sha256"):
                raise WarehouseError(
                    f"raw COCO file changed since the audit for split {split!r}: "
                    f"file={digest} marker={entry.get('sha256')}"
                )
            if entry.get("bytes") is not None and size != entry["bytes"]:
                raise WarehouseError(
                    f"raw COCO size changed since the audit for split {split!r}: "
                    f"file={size} marker={entry['bytes']}"
                )
        checks.append(
            {
                "check": "source_checksums_raw_files",
                "status": "pass",
                "detail": f"re-hashed {len(source_files)} raw COCO file(s)",
            }
        )
    else:
        checks.append(
            {
                "check": "source_checksums_raw_files",
                "status": "skipped",
                "detail": (
                    "disabled"
                    if not config.verify_source_hashes
                    else "no data_root supplied"
                ),
            }
        )

    # --- summary-count reconciliation ----------------------------------------
    images_lines = count_lines(paths["images"])
    annotations_lines = count_lines(paths["annotations"])
    expected_records = marker.get("total_image_records")
    expected_annotations = marker.get("total_annotations")
    if images_lines != expected_records:
        raise WarehouseError(
            f"image record count mismatch: images.jsonl={images_lines} marker={expected_records}"
        )
    if annotations_lines != expected_annotations:
        raise WarehouseError(
            "annotation count mismatch: "
            f"annotations.jsonl={annotations_lines} marker={expected_annotations}"
        )

    split_annotations = sum(v.get("annotations", 0) for v in report.get("splits", {}).values())
    if split_annotations != expected_annotations:
        raise WarehouseError(
            "report/marker annotation mismatch: "
            f"report splits={split_annotations} marker={expected_annotations}"
        )
    split_records = sum(v.get("image_records", 0) for v in report.get("splits", {}).values())
    if split_records != expected_records:
        raise WarehouseError(
            "report/marker image record mismatch: "
            f"report splits={split_records} marker={expected_records}"
        )

    # --- annotation export ordering guard ------------------------------------
    # The audit writes annotations in split order; the warehouse records their
    # 0-based export position. Confirm the first/last rows line up so the
    # position column is trustworthy rather than silently misaligned.
    first, last = _first_last_line(paths["annotations"])
    if annotations_lines and (first is None or last is None):
        raise WarehouseError("annotations.jsonl could not be read for ordering check")

    checks.append(
        {
            "check": "summary_counts",
            "status": "pass",
            "detail": (
                f"records={images_lines} annotations={annotations_lines} "
                f"unique_filenames={marker.get('unique_filenames')}"
            ),
        }
    )

    expected = {
        "total_image_records": expected_records,
        "total_annotations": expected_annotations,
        "unique_filenames": marker.get("unique_filenames"),
        "splits": sorted(report.get("splits", {})),
        "first_annotation_id": (first or {}).get("id"),
        "last_annotation_id": (last or {}).get("id"),
        "images_path": paths["images"],
        "annotations_path": paths["annotations"],
        "findings_path": paths["findings"],
        "membership_path": paths["membership"],
        "sources_path": paths["sources"],
    }
    return AuditInputs(
        audit_dir=audit_dir,
        marker=marker,
        sources=sources,
        report=report,
        run=run,
        images_path=paths["images"],
        annotations_path=paths["annotations"],
        findings_path=paths["findings"],
        membership_path=paths["membership"],
        expected=expected,
        checks=checks,
    )


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _columns_sql(columns: Dict[str, str]) -> str:
    inner = ", ".join(f"'{name}': '{typ}'" for name, typ in columns.items())
    return "{" + inner + "}"


def _create_schema_and_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
    con.execute(
        f"""
        CREATE TABLE {SCHEMA_NAME}.images (
            split VARCHAR NOT NULL,
            record_position BIGINT NOT NULL,
            record_key VARCHAR NOT NULL,
            id BIGINT NOT NULL,
            file_name VARCHAR NOT NULL,
            original_filename VARCHAR,
            cell_line VARCHAR,
            well VARCHAR,
            well_row VARCHAR,
            well_col BIGINT,
            field BIGINT,
            timestamp VARCHAR,
            time_hours DOUBLE,
            crop BIGINT,
            filename_parsed BOOLEAN,
            width BIGINT,
            height BIGINT,
            local_path VARCHAR,
            local_exists BOOLEAN,
            local_width BIGINT,
            local_height BIGINT,
            dimensions_status VARCHAR,
            dimensions_match BOOLEAN,
            annotation_count BIGINT
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE {SCHEMA_NAME}.annotations (
            split VARCHAR NOT NULL,
            record_position BIGINT NOT NULL,
            id BIGINT NOT NULL,
            image_id BIGINT NOT NULL,
            area DOUBLE,
            bbox DOUBLE[],
            n_points BIGINT
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE {SCHEMA_NAME}.findings (
            record_position BIGINT NOT NULL,
            finding_id VARCHAR NOT NULL,
            type VARCHAR NOT NULL,
            status VARCHAR,
            title VARCHAR,
            file_name VARCHAR,
            splits VARCHAR[],
            records JSON,
            recommendation VARCHAR,
            evidence JSON
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE {SCHEMA_NAME}.sources (
            split VARCHAR NOT NULL,
            path VARCHAR,
            sha256 VARCHAR,
            bytes BIGINT,
            images_root VARCHAR,
            local_image_files BIGINT
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE {SCHEMA_NAME}.runs (
            run_id VARCHAR NOT NULL,
            tool_name VARCHAR,
            tool_version VARCHAR,
            completed_at VARCHAR,
            splits VARCHAR[],
            low_count_threshold BIGINT,
            check_dimensions BOOLEAN,
            total_image_records BIGINT,
            total_annotations BIGINT,
            unique_filenames BIGINT,
            source_sha256_train VARCHAR,
            source_sha256_val VARCHAR,
            source_sha256_test VARCHAR
        )
        """
    )
    con.execute(
        f"""
        CREATE TABLE {SCHEMA_NAME}.val_membership (
            record_position BIGINT NOT NULL,
            split VARCHAR NOT NULL,
            image_id BIGINT NOT NULL,
            file_name VARCHAR,
            decision VARCHAR,
            reason VARCHAR
        )
        """
    )


def _load_tables(con: duckdb.DuckDBPyConnection, inputs: AuditInputs) -> None:
    images_json = _sql_str(inputs.images_path)
    annotations_json = _sql_str(inputs.annotations_path)
    findings_json = _sql_str(inputs.findings_path)

    con.execute(
        f"""
        INSERT INTO {SCHEMA_NAME}.images
        SELECT
            split,
            position AS record_position,
            split || ':' || CAST(position AS VARCHAR) AS record_key,
            id, file_name, original_filename, cell_line, well, well_row, well_col,
            field, timestamp, time_hours, crop, filename_parsed, width, height,
            local_path, local_exists, local_width, local_height, dimensions_status,
            dimensions_match, annotation_count
        FROM read_json({images_json},
                       columns={_columns_sql(IMAGES_COLUMNS)},
                       format='newline_delimited')
        """
    )
    con.execute(
        f"""
        INSERT INTO {SCHEMA_NAME}.annotations
        SELECT
            split,
            row_number() OVER () - 1 AS record_position,
            id, image_id, area, bbox, n_points
        FROM read_json({annotations_json},
                       columns={_columns_sql(ANNOTATIONS_COLUMNS)},
                       format='newline_delimited')
        """
    )
    con.execute(
        f"""
        INSERT INTO {SCHEMA_NAME}.findings
        SELECT
            row_number() OVER () - 1 AS record_position,
            finding_id, type, status, title, file_name, splits,
            records, recommendation, evidence
        FROM read_json({findings_json},
                       columns={_columns_sql(FINDINGS_COLUMNS)},
                       format='array')
        """
    )

    sources = inputs.sources
    images_root = sources.get("images_root")
    local_total = sources.get("local_image_files")
    for split, entry in (sources.get("coco_files") or {}).items():
        con.execute(
            f"INSERT INTO {SCHEMA_NAME}.sources VALUES (?, ?, ?, ?, ?, ?)",
            [split, entry.get("path"), entry.get("sha256"), entry.get("bytes"), images_root, local_total],
        )

    marker = inputs.marker
    run = inputs.run
    hashes = marker.get("source_sha256", {}) or {}
    run_id = "audit-" + _short_hash("|".join(f"{k}:{v}" for k, v in sorted(hashes.items())))
    con.execute(
        f"INSERT INTO {SCHEMA_NAME}.runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            run_id,
            run.get("tool", {}).get("name"),
            run.get("tool", {}).get("version"),
            marker.get("completed_at"),
            marker.get("splits"),
            run.get("low_count_threshold"),
            run.get("check_dimensions"),
            marker.get("total_image_records"),
            marker.get("total_annotations"),
            marker.get("unique_filenames"),
            hashes.get("train"),
            hashes.get("val"),
            hashes.get("test"),
        ],
    )

    membership = read_json(inputs.membership_path).get("membership", [])
    for position, row in enumerate(membership):
        con.execute(
            f"INSERT INTO {SCHEMA_NAME}.val_membership VALUES (?, ?, ?, ?, ?, ?)",
            [
                position,
                row.get("split"),
                row.get("image_id"),
                row.get("file_name"),
                row.get("decision"),
                row.get("reason"),
            ],
        )


def _short_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _create_views(con: duckdb.DuckDBPyConnection) -> None:
    for name, body in VIEWS.items():
        con.execute(f"CREATE VIEW {SCHEMA_NAME}.{name} AS {body}")


#: Base tables exported to Parquet (typed, insertion-ordered).
PARQUET_TABLES = ("images", "annotations", "findings", "sources", "runs", "val_membership")


def _export_parquet(
    con: duckdb.DuckDBPyConnection, parquet_dir: str, compression: str
) -> Dict[str, int]:
    os.makedirs(parquet_dir, exist_ok=True)
    sizes: Dict[str, int] = {}
    targets = [(name, f"SELECT * FROM {SCHEMA_NAME}.{name}") for name in PARQUET_TABLES]
    # Aggregate views are ordered explicitly so their Parquet row order does not
    # depend on hash-group order.
    targets.append(
        (
            "v1_image_annotation_stats",
            f"SELECT * FROM {SCHEMA_NAME}.v1_image_annotation_stats ORDER BY split, image_id",
        )
    )
    targets.append(
        (
            "v1_cell_line_summary",
            f"SELECT * FROM {SCHEMA_NAME}.v1_cell_line_summary ORDER BY cell_line",
        )
    )
    for name, relation in targets:
        path = os.path.join(parquet_dir, f"{name}.parquet")
        con.execute(
            f"COPY ({relation}) TO {_sql_str(path)} "
            f"(FORMAT PARQUET, COMPRESSION {compression})"
        )
        sizes[name] = os.path.getsize(path)
    return sizes


def _split_reconciliation(con: duckdb.DuckDBPyConnection, inputs: AuditInputs) -> Dict[str, Any]:
    image_rows = con.execute(
        f"""
        SELECT split, COUNT(*) AS image_records, COUNT(DISTINCT id) AS unique_ids,
               COUNT(DISTINCT file_name) AS unique_filenames,
               SUM(annotation_count) AS annotation_count_sum
        FROM {SCHEMA_NAME}.images GROUP BY split
        """
    ).fetchall()
    annotation_rows = con.execute(
        f"SELECT split, COUNT(*) FROM {SCHEMA_NAME}.annotations GROUP BY split"
    ).fetchall()
    orphan_rows = con.execute(
        f"""
        SELECT split, COUNT(*) AS groups, SUM(annotation_count) AS records
        FROM {SCHEMA_NAME}.v1_orphan_annotations GROUP BY split
        """
    ).fetchall()

    images = {r[0]: r for r in image_rows}
    annotations = {r[0]: r[1] for r in annotation_rows}
    orphans = {r[0]: (r[1], r[2]) for r in orphan_rows}

    report_splits = inputs.report.get("splits", {})
    result: Dict[str, Any] = {}
    for split in sorted(set(images) | set(annotations) | set(report_splits)):
        exp = report_splits.get(split, {})
        img = images.get(split)
        loaded_orphan_groups, loaded_orphan_records = orphans.get(split, (0, 0))
        entry: Dict[str, Any] = {
            "image_records": _delta(exp.get("image_records"), img[1] if img else 0),
            "unique_ids": _delta(exp.get("unique_ids"), img[2] if img else 0),
            "unique_filenames": _delta(exp.get("unique_filenames"), img[3] if img else 0),
            "annotations": _delta(exp.get("annotations"), annotations.get(split, 0)),
            "orphan_annotations": _delta(
                exp.get("orphan_annotations"), loaded_orphan_records
            ),
            "annotation_count_sum": _delta(
                exp.get("annotations"), img[4] if img else 0
            ),
            "orphan_annotation_groups": loaded_orphan_groups,
            "duplicate_ids_expected": exp.get("duplicate_ids", []),
        }
        result[split] = entry
    return result


def _delta(expected: Optional[int], loaded: int) -> Dict[str, Any]:
    return {
        "expected": expected,
        "loaded": loaded,
        "delta": (loaded - expected) if expected is not None else None,
    }


def _join_integrity(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    ambiguous = con.execute(
        f"""
        SELECT split, image_id, record_count, record_positions, file_names
        FROM {SCHEMA_NAME}.v1_image_join_ambiguity ORDER BY split, image_id
        """
    ).fetchall()
    orphans = con.execute(
        f"""
        SELECT split, image_id, annotation_count
        FROM {SCHEMA_NAME}.v1_orphan_annotations ORDER BY split, image_id
        """
    ).fetchall()
    dup_ann = con.execute(
        f"""
        SELECT split, id, COUNT(*) AS n FROM {SCHEMA_NAME}.annotations
        GROUP BY split, id HAVING COUNT(*) > 1 ORDER BY split, id
        """
    ).fetchall()
    return {
        "ambiguous_image_id_groups": [
            {
                "split": r[0],
                "image_id": r[1],
                "record_count": r[2],
                "record_positions": list(r[3]),
                "file_names": list(r[4]),
            }
            for r in ambiguous
        ],
        "ambiguous_image_id_records": sum(r[2] for r in ambiguous),
        "orphan_annotation_groups": [
            {"split": r[0], "image_id": r[1], "annotation_count": r[2]} for r in orphans
        ],
        "orphan_annotation_records": sum(r[2] for r in orphans),
        "duplicate_annotation_keys": [
            {"split": r[0], "id": r[1], "count": r[2]} for r in dup_ann
        ],
    }


def run_warehouse(config: WarehouseConfig) -> Dict[str, Any]:
    """Build the DuckDB warehouse and Parquet exports; return the run record."""
    started = time.perf_counter()

    out_dir = os.path.abspath(config.out_dir)
    marker_path = os.path.join(out_dir, COMPLETION_MARKER)
    staging_dir = os.path.join(out_dir, STAGING_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    try:
        # Invalidate any previous completion evidence before any fallible work,
        # including input validation, so a failed rerun into a reused directory
        # can never keep looking like a completed run.
        clear_stale(out_dir)
        inputs = validate_completed_audit(config)
        os.makedirs(staging_dir)
        parquet_dir = os.path.join(staging_dir, "parquet")
        db_path = os.path.join(staging_dir, "audit.duckdb")

        build_started = time.perf_counter()
        con = duckdb.connect(db_path)
        try:
            if config.threads is not None:
                con.execute(f"SET threads = {int(config.threads)}")
            _create_schema_and_tables(con)
            _load_tables(con, inputs)
            _create_views(con)
            load_seconds = time.perf_counter() - build_started

            # --- integrity: reconcile everything back to the audit ----------
            images_count = con.execute(f"SELECT COUNT(*) FROM {SCHEMA_NAME}.images").fetchone()[0]
            annotations_count = con.execute(
                f"SELECT COUNT(*) FROM {SCHEMA_NAME}.annotations"
            ).fetchone()[0]
            findings_count = con.execute(
                f"SELECT COUNT(*) FROM {SCHEMA_NAME}.findings"
            ).fetchone()[0]
            membership_count = con.execute(
                f"SELECT COUNT(*) FROM {SCHEMA_NAME}.val_membership"
            ).fetchone()[0]
            distinct_filenames = con.execute(
                f"SELECT COUNT(DISTINCT file_name) FROM {SCHEMA_NAME}.images"
            ).fetchone()[0]

            # annotation export position guard
            first_id = con.execute(
                f"SELECT id FROM {SCHEMA_NAME}.annotations WHERE record_position = 0"
            ).fetchone()
            last_id = con.execute(
                f"SELECT id FROM {SCHEMA_NAME}.annotations WHERE record_position = ?",
                [annotations_count - 1],
            ).fetchone()
            expected_first = inputs.expected.get("first_annotation_id")
            expected_last = inputs.expected.get("last_annotation_id")
            if first_id and first_id[0] != expected_first:
                raise WarehouseError(
                    "annotation export position misaligned: "
                    f"first row id={first_id[0]} file={expected_first}"
                )
            if last_id and last_id[0] != expected_last:
                raise WarehouseError(
                    "annotation export position misaligned: "
                    f"last row id={last_id[0]} file={expected_last}"
                )

            split_recon = _split_reconciliation(con, inputs)
            for split, entry in split_recon.items():
                for key in (
                    "image_records",
                    "unique_ids",
                    "unique_filenames",
                    "annotations",
                    "orphan_annotations",
                ):
                    delta = entry[key]["delta"]
                    if delta not in (0, None):
                        raise WarehouseError(
                            f"split {split!r} {key} mismatch: loaded={entry[key]['loaded']} "
                            f"expected={entry[key]['expected']}"
                        )
            join_integrity = _join_integrity(con)

            # --- representative query timing (db vs raw JSONL) --------------
            q0 = time.perf_counter()
            con.execute(_REPRESENTATIVE_DB_QUERY).fetchall()
            query_db_seconds = time.perf_counter() - q0
            q1 = time.perf_counter()
            con.execute(
                f"""
                SELECT i.cell_line, COUNT(*)
                FROM read_json({_sql_str(inputs.annotations_path)},
                               columns={_columns_sql(ANNOTATIONS_COLUMNS)},
                               format='newline_delimited') a
                JOIN read_json({_sql_str(inputs.images_path)},
                               columns={_columns_sql(IMAGES_COLUMNS)},
                               format='newline_delimited') i
                  ON i.split = a.split AND i.id = a.image_id
                GROUP BY i.cell_line
                """
            ).fetchall()
            query_jsonl_seconds = time.perf_counter() - q1

            export_started = time.perf_counter()
            parquet_sizes = _export_parquet(con, parquet_dir, config.parquet_compression)
            export_seconds = time.perf_counter() - export_started
        finally:
            con.close()

        db_bytes = os.path.getsize(db_path)
        jsonl_sizes = {
            "images": os.path.getsize(inputs.images_path),
            "annotations": os.path.getsize(inputs.annotations_path),
        }

        integrity = {
            "schema_version": SCHEMA_VERSION,
            "view_version": VIEW_VERSION,
            "input": {
                "marker_status": inputs.marker.get("status"),
                "completed_at": inputs.marker.get("completed_at"),
                "tool_version": inputs.marker.get("tool", {}).get("version"),
                "source_sha256": inputs.marker.get("source_sha256", {}),
                "splits": inputs.marker.get("splits"),
            },
            "validation": {"checks": inputs.checks},
            "reconciliation": {
                "images": {
                    "expected": inputs.expected["total_image_records"],
                    "loaded": images_count,
                    "delta": images_count - inputs.expected["total_image_records"],
                },
                "annotations": {
                    "expected": inputs.expected["total_annotations"],
                    "loaded": annotations_count,
                    "delta": annotations_count - inputs.expected["total_annotations"],
                },
                "unique_filenames": {
                    "expected": inputs.expected["unique_filenames"],
                    "loaded": distinct_filenames,
                    "delta": distinct_filenames - inputs.expected["unique_filenames"],
                },
                "findings": {"loaded": findings_count},
                "val_membership": {"loaded": membership_count},
                "splits": split_recon,
            },
            "join_integrity": join_integrity,
            "sizes_bytes": {
                "parquet": parquet_sizes,
                "jsonl": jsonl_sizes,
                "findings_json": os.path.getsize(inputs.findings_path),
            },
            "timings": {
                "load_and_view_seconds": round(load_seconds, 3),
                "parquet_export_seconds": round(export_seconds, 3),
                "representative_query_db_seconds": round(query_db_seconds, 4),
                "representative_query_jsonl_seconds": round(query_jsonl_seconds, 4),
                "representative_query": " ".join(_REPRESENTATIVE_DB_QUERY.split()),
            },
        }
        dump_json(os.path.join(staging_dir, "integrity.json"), integrity)

        total_seconds = time.perf_counter() - started
        run_record = {
            "tool": {"name": "livecell-audit", "version": __version__},
            "stage": "warehouse",
            "schema_version": SCHEMA_VERSION,
            "view_version": VIEW_VERSION,
            "audit_dir": inputs.audit_dir,
            "out_dir": out_dir,
            "data_root": os.path.abspath(config.data_root) if config.data_root else None,
            "verify_source_hashes": config.verify_source_hashes,
            "parquet_compression": config.parquet_compression,
            "duckdb_version": duckdb.__version__,
            "database_bytes": db_bytes,
            "output_sizes_bytes": {
                "parquet": parquet_sizes,
                "jsonl": jsonl_sizes,
            },
            "runtime_seconds": round(total_seconds, 3),
            "runtime_method": "time.perf_counter() around validation, load, views and export",
            "timings": integrity["timings"],
        }
        dump_json(os.path.join(staging_dir, "run.json"), run_record)

        promote_staged(staging_dir, out_dir)
        write_completion_marker(
            marker_path,
            {
                "status": "completed",
                "stage": "warehouse",
                "tool": {"name": "livecell-audit", "version": __version__},
                "schema_version": SCHEMA_VERSION,
                "view_version": VIEW_VERSION,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "audit_dir": inputs.audit_dir,
                "total_image_records": images_count,
                "total_annotations": annotations_count,
                "unique_filenames": distinct_filenames,
                "source_sha256": inputs.marker.get("source_sha256", {}),
                "outputs": {
                    "database": "audit.duckdb",
                    "parquet": "parquet/",
                    "integrity": "integrity.json",
                    "run": "run.json",
                },
            },
        )
    except BaseException:
        import shutil

        shutil.rmtree(staging_dir, ignore_errors=True)
        for leftover in (marker_path, marker_path + ".tmp"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise

    return {
        "run": run_record,
        "integrity": integrity,
        "marker": read_json(marker_path),
        "out_dir": out_dir,
    }
