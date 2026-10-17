"""Milestone 3: pandas feature table, cell-line summaries and feature dictionary.

The feature builder consumes a *completed* warehouse directory (see
:mod:`livecell_audit.warehouse`) and produces one documented feature row per
**annotation-version image record**. It joins three sources with explicit,
validated keys:

1. image metadata and version context from ``lc.images`` (key: ``(split,
   record_position)``);
2. annotation aggregates computed in SQL by ``lc.v1_image_annotation_stats``
   (key: ``(split, image_id)``), joined only where that key is unambiguous;
3. image-only statistics read once per distinct ``file_name`` from the TIFFs.

Identity and join rules
-----------------------
- The unique row key is ``record_key`` = ``(split, record_position)``. The
  original COCO ``id`` is a field, never a global key.
- ``(split, image_id)`` may be ambiguous (several image records share an id).
  Such rows are **quarantined**: annotation features are set to null and
  ``annotation_join_status = "ambiguous_image_id"``; the builder never guesses
  or multiplies counts. The ambiguity is recorded in ``run.json``.
- Orphan annotations (an ``image_id`` with no image record) are preserved in
  the warehouse raw tables and reported, never attached to a record.
- An image record with zero annotations gets ``n_annotations = 0`` (a real
  measurement) while area/bbox/polygon summaries stay null (undefined), so
  unavailable values are never turned into fabricated zeros.

Image statistics
----------------
Each distinct ``file_name`` is read at most once. TIFF pixel intensities are
used as stored (8-bit unsigned, 0-255 for this dataset) with no rescaling:
``pixel_mean`` / ``pixel_std`` are the mean and population standard deviation
over all pixels and ``gradient_mean`` is the mean magnitude of the
central-difference gradient (``numpy.gradient``). Uniform but valid images
legitimately have ``pixel_std = 0`` and ``gradient_mean = 0``; missing or
unreadable files keep null statistics and an explicit status. These are texture
proxies, not measurements of cell number or true confluence.

Alternate annotation versions are all retained; image-only statistics are
reused by filename. Deduplicating physical filenames for model fitting is an
explicit later policy, not a decision this stage makes silently.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd

from . import __version__
from .completion import (
    COMPLETION_MARKER,
    MANIFEST_NAME,
    STAGING_DIRNAME,
    build_manifest,
    clear_stale,
    dump_json,
    load_completion_marker,
    promote_staged,
    read_json,
    sha256_file,
    verify_manifest,
    write_completion_marker,
)
from .errors import FeatureError
from .warehouse import SCHEMA_VERSION as WAREHOUSE_SCHEMA_VERSION
from .warehouse import VIEW_VERSION as WAREHOUSE_VIEW_VERSION

FEATURE_SCHEMA_VERSION = "1"
SPLIT_ORDER = {"train": 0, "val": 1, "test": 2}

#: Required feature outputs listed in the feature manifest.
FEATURE_OUTPUT_PATHS = (
    "features.parquet",
    "features.jsonl",
    "cell_line_summary.json",
    "cell_line_summary.csv",
    "cell_line_summary.parquet",
    "feature_distributions.json",
    "feature_dictionary.json",
    "run.json",
)

#: TIFF intensity contract published in ``feature_dictionary.json``.
IMAGE_STATISTICS_CONTRACT = {
    "source": "raw TIFF pixel values as stored on disk",
    "normalization": "none (no rescaling, no per-image contrast stretch)",
    "units": "stored integer intensity levels; 0-255 for 8-bit unsigned data",
    "bit_depth": (
        "LIVECell TIFFs are 8-bit unsigned. A 16-bit or otherwise different bit "
        "depth is read as stored and is not rescaled to 0-255, so pixel_mean, "
        "pixel_std, gradient_mean, pixel_min and pixel_max are not comparable "
        "across bit depths; a mixed-bit-depth input is a documented limitation."
    ),
    "channel_policy": "a multi-channel TIFF is reduced to its first channel",
    "uniform_images": "a valid uniform image has pixel_std = 0 and gradient_mean = 0",
    "unreadable_or_missing": (
        "pixel statistics are null and image_features_status records "
        "missing_file / unreadable / not_attempted"
    ),
}

# --------------------------------------------------------------------------- #
# Feature dictionary (single source of truth for column order and types)
# --------------------------------------------------------------------------- #

#: dtype is the DuckDB/Parquet type; ``depends_on_annotations`` marks features
#: whose value is derived from the polygon labels rather than the image file.
FEATURE_DICTIONARY: List[Dict[str, Any]] = [
    # -- identity / keys -----------------------------------------------------
    {"name": "split", "dtype": "VARCHAR", "source": "audit tables/images.jsonl",
     "formula": "COCO split name", "unit": "category", "depends_on_annotations": False,
     "missing_behavior": "never missing",
     "description": "Split the image record belongs to (train/val/test)."},
    {"name": "record_position", "dtype": "BIGINT", "source": "audit tables/images.jsonl",
     "formula": "0-based position in the split's images array", "unit": "index",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Image-record key part; unique within a split."},
    {"name": "record_key", "dtype": "VARCHAR", "source": "derived",
     "formula": "split || ':' || record_position", "unit": "identifier",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Unique image-record key (split plus original position)."},
    {"name": "image_id", "dtype": "BIGINT", "source": "audit tables/images.jsonl",
     "formula": "COCO image id", "unit": "identifier", "depends_on_annotations": False,
     "missing_behavior": "never missing",
     "description": "Original COCO id, scoped to the split; a field, not a global key."},
    {"name": "file_name", "dtype": "VARCHAR", "source": "audit tables/images.jsonl",
     "formula": "COCO file_name", "unit": "identifier", "depends_on_annotations": False,
     "missing_behavior": "never missing",
     "description": "Logical image identity (one acquisition); not a pixel-identity claim."},
    {"name": "original_filename", "dtype": "VARCHAR", "source": "audit tables/images.jsonl",
     "formula": "COCO original_filename", "unit": "identifier",
     "depends_on_annotations": False, "missing_behavior": "null when absent",
     "description": "Companion filename recorded by the dataset authors."},
    # -- acquisition metadata ------------------------------------------------
    {"name": "cell_line", "dtype": "VARCHAR", "source": "filename parse",
     "formula": "leading token before '_Phase_'", "unit": "category",
     "depends_on_annotations": False, "missing_behavior": "null if filename unparsed",
     "description": "Cell line; acquisition context, not biological growth."},
    {"name": "well", "dtype": "VARCHAR", "source": "filename parse",
     "formula": "microplate well, e.g. A7", "unit": "category",
     "depends_on_annotations": False, "missing_behavior": "null if filename unparsed",
     "description": "Microplate well."},
    {"name": "well_row", "dtype": "VARCHAR", "source": "filename parse",
     "formula": "well letter", "unit": "category", "depends_on_annotations": False,
     "missing_behavior": "null if filename unparsed", "description": "Well row letter."},
    {"name": "well_col", "dtype": "BIGINT", "source": "filename parse",
     "formula": "well column number", "unit": "index", "depends_on_annotations": False,
     "missing_behavior": "null if filename unparsed", "description": "Well column number."},
    {"name": "field", "dtype": "BIGINT", "source": "filename parse",
     "formula": "field index within the well", "unit": "index",
     "depends_on_annotations": False, "missing_behavior": "null if filename unparsed",
     "description": "Acquisition field index."},
    {"name": "timestamp", "dtype": "VARCHAR", "source": "filename parse",
     "formula": "<days>d<hours>h<minutes>m", "unit": "duration string",
     "depends_on_annotations": False, "missing_behavior": "null if filename unparsed",
     "description": "Raw incubation timestamp string."},
    {"name": "time_hours", "dtype": "DOUBLE", "source": "filename parse",
     "formula": "days*24 + hours + minutes/60", "unit": "hours",
     "depends_on_annotations": False, "missing_behavior": "null if filename unparsed",
     "description": "Incubation time in hours."},
    {"name": "crop", "dtype": "BIGINT", "source": "filename parse",
     "formula": "crop index within the field", "unit": "index",
     "depends_on_annotations": False, "missing_behavior": "null if filename unparsed",
     "description": "Crop index."},
    {"name": "filename_parsed", "dtype": "BOOLEAN", "source": "filename parse",
     "formula": "filename matches the acquisition scheme", "unit": "flag",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Whether the acquisition metadata could be parsed."},
    # -- version context -----------------------------------------------------
    {"name": "versions_in_split", "dtype": "BIGINT", "source": "derived from images",
     "formula": "count of records with the same (split, file_name)", "unit": "count",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Number of annotation versions of this filename in this split."},
    {"name": "records_for_filename", "dtype": "BIGINT", "source": "derived from images",
     "formula": "count of records with the same file_name across splits", "unit": "count",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Total image records sharing this filename."},
    {"name": "splits_for_filename", "dtype": "BIGINT", "source": "derived from images",
     "formula": "distinct splits referencing this file_name", "unit": "count",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "How many splits reference this filename."},
    {"name": "is_alternate_version", "dtype": "BOOLEAN", "source": "derived",
     "formula": "versions_in_split > 1", "unit": "flag",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "True for records sharing a filename within a split."},
    {"name": "is_cross_split_overlap", "dtype": "BOOLEAN", "source": "derived",
     "formula": "splits_for_filename > 1", "unit": "flag",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "True when the filename appears in more than one split."},
    # -- annotation features (depend on labels) ------------------------------
    {"name": "n_annotations", "dtype": "BIGINT", "source": "SQL aggregation of annotations",
     "formula": "COUNT(*) grouped by (split, image_id)", "unit": "count",
     "depends_on_annotations": True,
     "missing_behavior": "0 when the record has no annotations; null when the join is ambiguous",
     "description": "Number of annotations pointing at this image record."},
    {"name": "area_sum", "dtype": "DOUBLE", "source": "annotations.area",
     "formula": "SUM(area)", "unit": "pixels^2", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Sum of COCO annotation areas."},
    {"name": "area_mean", "dtype": "DOUBLE", "source": "annotations.area",
     "formula": "AVG(area)", "unit": "pixels^2", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Mean COCO annotation area."},
    {"name": "area_median", "dtype": "DOUBLE", "source": "annotations.area",
     "formula": "median(area)", "unit": "pixels^2", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Median COCO annotation area."},
    {"name": "area_min", "dtype": "DOUBLE", "source": "annotations.area",
     "formula": "MIN(area)", "unit": "pixels^2", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Smallest annotation area."},
    {"name": "area_max", "dtype": "DOUBLE", "source": "annotations.area",
     "formula": "MAX(area)", "unit": "pixels^2", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Largest annotation area."},
    {"name": "area_std", "dtype": "DOUBLE", "source": "annotations.area",
     "formula": "stddev_samp(area)", "unit": "pixels^2", "depends_on_annotations": True,
     "missing_behavior": "null for <2 annotations or ambiguous",
     "description": "Sample standard deviation of annotation areas."},
    {"name": "bbox_width_mean", "dtype": "DOUBLE", "source": "annotations.bbox[3]",
     "formula": "AVG(bbox width)", "unit": "pixels", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Mean bounding-box width."},
    {"name": "bbox_width_median", "dtype": "DOUBLE", "source": "annotations.bbox[3]",
     "formula": "median(bbox width)", "unit": "pixels", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Median bounding-box width."},
    {"name": "bbox_height_mean", "dtype": "DOUBLE", "source": "annotations.bbox[4]",
     "formula": "AVG(bbox height)", "unit": "pixels", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Mean bounding-box height."},
    {"name": "bbox_height_median", "dtype": "DOUBLE", "source": "annotations.bbox[4]",
     "formula": "median(bbox height)", "unit": "pixels", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Median bounding-box height."},
    {"name": "n_points_mean", "dtype": "DOUBLE", "source": "annotations.n_points",
     "formula": "AVG(polygon point count)", "unit": "points", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Mean number of polygon points per annotation."},
    {"name": "n_points_median", "dtype": "DOUBLE", "source": "annotations.n_points",
     "formula": "median(polygon point count)", "unit": "points", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Median number of polygon points per annotation."},
    {"name": "n_points_min", "dtype": "BIGINT", "source": "annotations.n_points",
     "formula": "MIN(polygon point count)", "unit": "points", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Fewest polygon points on an annotation."},
    {"name": "n_points_max", "dtype": "BIGINT", "source": "annotations.n_points",
     "formula": "MAX(polygon point count)", "unit": "points", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Most polygon points on an annotation."},
    {"name": "n_points_sum", "dtype": "BIGINT", "source": "annotations.n_points",
     "formula": "SUM(polygon point count)", "unit": "points", "depends_on_annotations": True,
     "missing_behavior": "null when no annotations or ambiguous",
     "description": "Total polygon points across annotations."},
    {"name": "annotation_join_status", "dtype": "VARCHAR", "source": "derived",
     "formula": "'ok' | 'ambiguous_image_id'", "unit": "category",
     "depends_on_annotations": True, "missing_behavior": "never missing",
     "description": "Whether the (split, image_id) annotation join was unambiguous."},
    # -- image dimensions ----------------------------------------------------
    {"name": "width", "dtype": "BIGINT", "source": "audit tables/images.jsonl",
     "formula": "recorded width", "unit": "pixels", "depends_on_annotations": False,
     "missing_behavior": "never missing", "description": "Image width from the COCO record."},
    {"name": "height", "dtype": "BIGINT", "source": "audit tables/images.jsonl",
     "formula": "recorded height", "unit": "pixels", "depends_on_annotations": False,
     "missing_behavior": "never missing", "description": "Image height from the COCO record."},
    {"name": "image_area_pixels", "dtype": "BIGINT", "source": "derived",
     "formula": "width * height", "unit": "pixels", "depends_on_annotations": False,
     "missing_behavior": "never missing", "description": "Recorded pixel area."},
    {"name": "local_width", "dtype": "BIGINT", "source": "audit dimension check",
     "formula": "local TIFF width", "unit": "pixels", "depends_on_annotations": False,
     "missing_behavior": "null when missing/unreadable/not checked",
     "description": "Width read from the local TIFF."},
    {"name": "local_height", "dtype": "BIGINT", "source": "audit dimension check",
     "formula": "local TIFF height", "unit": "pixels", "depends_on_annotations": False,
     "missing_behavior": "null when missing/unreadable/not checked",
     "description": "Height read from the local TIFF."},
    {"name": "dimensions_status", "dtype": "VARCHAR", "source": "audit dimension check",
     "formula": "matching|mismatching|missing|unreadable|not_checked", "unit": "category",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Outcome of the record/local dimension check."},
    {"name": "dimensions_match", "dtype": "BOOLEAN", "source": "audit dimension check",
     "formula": "dimensions_status == 'matching'", "unit": "flag",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "True only when the local size equals the record."},
    # -- image-only statistics ----------------------------------------------
    {"name": "pixel_mean", "dtype": "DOUBLE", "source": "TIFF read once per filename",
     "formula": "mean of stored pixel values", "unit": "intensity (0-255)",
     "depends_on_annotations": False, "missing_behavior": "null when missing/unreadable",
     "description": "Mean raw stored grayscale intensity (no normalization); texture proxy only."},
    {"name": "pixel_std", "dtype": "DOUBLE", "source": "TIFF read once per filename",
     "formula": "population standard deviation of pixel values", "unit": "intensity (0-255)",
     "depends_on_annotations": False, "missing_behavior": "null when missing/unreadable",
     "description": "Contrast proxy on raw stored intensities (no normalization); 0 for a valid uniform image."},
    {"name": "gradient_mean", "dtype": "DOUBLE", "source": "TIFF read once per filename",
     "formula": "mean magnitude of numpy.gradient central differences",
     "unit": "intensity/pixel", "depends_on_annotations": False,
     "missing_behavior": "null when missing/unreadable",
     "description": "Edge/texture proxy on raw stored intensities (no normalization); 0 for a valid uniform image."},
    {"name": "pixel_min", "dtype": "DOUBLE", "source": "TIFF read once per filename",
     "formula": "minimum pixel value", "unit": "intensity (0-255)",
     "depends_on_annotations": False, "missing_behavior": "null when missing/unreadable",
     "description": "Darkest raw stored pixel value."},
    {"name": "pixel_max", "dtype": "DOUBLE", "source": "TIFF read once per filename",
     "formula": "maximum pixel value", "unit": "intensity (0-255)",
     "depends_on_annotations": False, "missing_behavior": "null when missing/unreadable",
     "description": "Brightest raw stored pixel value."},
    {"name": "image_features_status", "dtype": "VARCHAR", "source": "derived",
     "formula": "'ok' | 'missing_file' | 'unreadable'", "unit": "category",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Whether image-only statistics could be computed."},
    {"name": "image_features_available", "dtype": "BOOLEAN", "source": "derived",
     "formula": "image_features_status == 'ok'", "unit": "flag",
     "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "True when pixel statistics are present."},
    # -- quality / missingness flags ----------------------------------------
    {"name": "has_image_file", "dtype": "BOOLEAN", "source": "audit tables/images.jsonl",
     "formula": "local_exists", "unit": "flag", "depends_on_annotations": False,
     "missing_behavior": "never missing", "description": "Local TIFF exists."},
    {"name": "has_annotations", "dtype": "BOOLEAN", "source": "derived",
     "formula": "n_annotations > 0", "unit": "flag", "depends_on_annotations": True,
     "missing_behavior": "null when the annotation join is ambiguous",
     "description": "The record has at least one annotation."},
    {"name": "zero_annotation", "dtype": "BOOLEAN", "source": "derived",
     "formula": "n_annotations == 0", "unit": "flag", "depends_on_annotations": True,
     "missing_behavior": "null when the annotation join is ambiguous",
     "description": "The record has no annotations (a measured zero, not missing data)."},
    {"name": "metadata_complete", "dtype": "BOOLEAN", "source": "derived",
     "formula": "filename_parsed and cell_line/well/field/crop/time_hours present",
     "unit": "flag", "depends_on_annotations": False, "missing_behavior": "never missing",
     "description": "Acquisition metadata parsed and complete."},
]

FEATURE_COLUMNS: List[str] = [entry["name"] for entry in FEATURE_DICTIONARY]
FEATURE_TYPES: Dict[str, str] = {entry["name"]: entry["dtype"] for entry in FEATURE_DICTIONARY}

#: Features summarised by cell line and as quantiles.
DISTRIBUTION_FEATURES = [
    "n_annotations",
    "area_mean",
    "area_median",
    "bbox_width_mean",
    "bbox_height_mean",
    "n_points_mean",
    "pixel_mean",
    "pixel_std",
    "gradient_mean",
]


@dataclass
class FeatureConfig:
    """Inputs for one feature build."""

    warehouse_dir: str
    out_dir: str
    #: Raw data root used to resolve ``local_path``. Derived from the warehouse
    #: ``sources`` table when omitted.
    data_root: Optional[str] = None
    #: Development-only cap on distinct filenames read for image statistics.
    #: Recorded in ``run.json``; never used for a delivered run.
    max_filenames: Optional[int] = None


def _image_statistics(path: str) -> Dict[str, Any]:
    """Compute image-only statistics for one TIFF, or an explicit failure."""
    from PIL import Image

    nulls = {
        "image_features_status": "unreadable",
        "pixel_mean": None,
        "pixel_std": None,
        "gradient_mean": None,
        "pixel_min": None,
        "pixel_max": None,
    }
    try:
        with Image.open(path) as image:
            image.load()
            array = np.asarray(image)
    except Exception:
        return dict(nulls)
    if array.size == 0:
        return dict(nulls)
    if array.ndim == 3:
        # Multi-channel TIFF: use the first channel; documented, not guessed.
        array = array.reshape(array.shape[0], array.shape[1], -1)[..., 0]
    if array.ndim != 2:
        return dict(nulls)
    values = array.astype(np.float64, copy=False)
    gradient: Optional[float]
    if values.shape[0] >= 2 and values.shape[1] >= 2:
        grad_y, grad_x = np.gradient(values)
        gradient = float(np.sqrt(grad_x * grad_x + grad_y * grad_y).mean())
    else:
        gradient = None
    return {
        "image_features_status": "ok",
        "pixel_mean": float(values.mean()),
        "pixel_std": float(values.std()),
        "gradient_mean": gradient,
        "pixel_min": float(values.min()),
        "pixel_max": float(values.max()),
    }


def _split_rank(series: pd.Series) -> pd.Series:
    return series.map(lambda value: SPLIT_ORDER.get(value, 99)).astype("int64")


def _resolve_data_root(con: duckdb.DuckDBPyConnection, config: FeatureConfig) -> Optional[str]:
    if config.data_root:
        return os.path.abspath(config.data_root)
    row = con.execute("SELECT images_root FROM lc.sources LIMIT 1").fetchone()
    if row and row[0]:
        images_root = row[0]
        return os.path.dirname(images_root) if os.path.basename(images_root) == "images" else images_root
    return None


def build_feature_frame(
    con: duckdb.DuckDBPyConnection,
    config: FeatureConfig,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Build the feature frame and a small diagnostics record."""
    images = con.execute("SELECT * FROM lc.images").df().rename(columns={"id": "image_id"})
    stats = con.execute("SELECT * FROM lc.v1_image_annotation_stats").df()
    ambiguous = con.execute("SELECT split, image_id FROM lc.v1_image_join_ambiguity").df()
    orphans = con.execute("SELECT split, image_id, annotation_count FROM lc.v1_orphan_annotations").df()

    stats_columns = [c for c in stats.columns if c not in ("split", "image_id")]

    # --- explicit validated join: (split, image_id), many_to_one -------------
    merged = images.merge(
        stats, on=["split", "image_id"], how="left", validate="many_to_one"
    )

    def _key(split: pd.Series, image_id: pd.Series) -> pd.Series:
        return split.astype(str) + "|" + image_id.astype(str)

    ambiguous_set = set(_key(ambiguous["split"], ambiguous["image_id"])) if len(ambiguous) else set()
    ambiguous_mask = _key(merged["split"], merged["image_id"]).isin(ambiguous_set)

    # Quarantine ambiguous rows: never guess, never multiply.
    merged.loc[ambiguous_mask, stats_columns] = np.nan
    merged["annotation_join_status"] = np.where(
        ambiguous_mask, "ambiguous_image_id", "ok"
    )

    # Zero annotations is a measured value; area/bbox/polygon summaries stay null.
    non_ambiguous = ~ambiguous_mask
    merged["n_annotations"] = merged["n_annotations"].astype("Int64")
    merged.loc[non_ambiguous, "n_annotations"] = (
        merged.loc[non_ambiguous, "n_annotations"].fillna(0).astype("Int64")
    )

    # --- integrity: the SQL count must equal the audit's annotation_count ----
    lhs = merged.loc[non_ambiguous, "annotation_count"].astype("int64").reset_index(drop=True)
    rhs = merged.loc[non_ambiguous, "n_annotations"].astype("int64").reset_index(drop=True)
    if not bool((lhs == rhs).all()):
        bad = int((lhs != rhs).sum())
        raise FeatureError(
            f"annotation join integrity failure: {bad} unambiguous record(s) disagree "
            "between the audit annotation_count and the SQL aggregate"
        )

    # --- image-only statistics: read each distinct filename once ------------
    data_root = _resolve_data_root(con, config)
    filenames = sorted(images["file_name"].dropna().unique().tolist())
    if config.max_filenames is not None:
        filenames = filenames[: config.max_filenames]
    local_paths: Dict[str, Optional[str]] = {}
    for file_name, group in images.groupby("file_name"):
        if file_name not in filenames:
            continue
        row = group.iloc[0]
        local_path = row["local_path"]
        if bool(row["local_exists"]) and local_path:
            if data_root and not os.path.isabs(local_path):
                local_paths[file_name] = os.path.join(data_root, local_path)
            else:
                local_paths[file_name] = local_path
        else:
            local_paths[file_name] = None

    image_feature_rows: Dict[str, Dict[str, Any]] = {}
    for file_name in filenames:
        path = local_paths.get(file_name)
        if path is None or not os.path.isfile(path):
            image_feature_rows[file_name] = {
                "image_features_status": "missing_file",
                "pixel_mean": None,
                "pixel_std": None,
                "gradient_mean": None,
                "pixel_min": None,
                "pixel_max": None,
            }
        else:
            image_feature_rows[file_name] = _image_statistics(path)

    image_feature_frame = pd.DataFrame.from_dict(image_feature_rows, orient="index")
    image_feature_frame.index.name = "file_name"
    merged = merged.merge(
        image_feature_frame, left_on="file_name", right_index=True, how="left",
        validate="many_to_one",
    )
    # Records whose filename was not read (development max_filenames limit) get
    # an explicit status rather than a silent null.
    read_filenames = set(filenames)
    not_attempted_mask = ~merged["file_name"].isin(read_filenames)
    merged.loc[not_attempted_mask, "image_features_status"] = "not_attempted"

    # --- derived version context -------------------------------------------
    merged["versions_in_split"] = merged.groupby(["split", "file_name"])["record_position"].transform("size").astype("Int64")
    merged["records_for_filename"] = merged.groupby("file_name")["record_position"].transform("size").astype("Int64")
    merged["splits_for_filename"] = merged.groupby("file_name")["split"].transform("nunique").astype("Int64")
    merged["is_alternate_version"] = (merged["versions_in_split"] > 1).astype("boolean")
    merged["is_cross_split_overlap"] = (merged["splits_for_filename"] > 1).astype("boolean")

    # --- flags ---------------------------------------------------------------
    merged["image_area_pixels"] = (merged["width"].astype("Int64") * merged["height"].astype("Int64")).astype("Int64")
    merged["image_features_available"] = (merged["image_features_status"] == "ok")
    merged["has_image_file"] = merged["local_exists"].astype("boolean")
    merged["has_annotations"] = merged["n_annotations"].gt(0).astype("boolean")
    merged["zero_annotation"] = merged["n_annotations"].eq(0).astype("boolean")
    merged["metadata_complete"] = (
        merged["filename_parsed"].fillna(False).astype(bool)
        & merged["cell_line"].notna()
        & merged["well"].notna()
        & merged["field"].notna()
        & merged["crop"].notna()
        & merged["time_hours"].notna()
    )

    # --- deterministic ordering and column order ----------------------------
    merged["_rank"] = _split_rank(merged["split"])
    merged = merged.sort_values(["_rank", "record_position"], kind="stable").drop(columns=["_rank"])
    frame = merged[FEATURE_COLUMNS].reset_index(drop=True)
    frame = _cast_frame(frame)

    diagnostics = {
        "records": int(len(frame)),
        "distinct_filenames_total": int(images["file_name"].nunique()),
        "filenames_read": int(len(filenames)),
        "partial": bool(len(filenames) < int(images["file_name"].nunique())),
        "max_filenames": config.max_filenames,
        "ambiguous_records": int(ambiguous_mask.sum()),
        "ambiguous_keys": [
            {"split": r[0], "image_id": int(r[1])} for r in ambiguous.itertuples(index=False)
        ],
        "orphan_annotation_groups": int(len(orphans)),
        "orphan_annotation_records": int(orphans["annotation_count"].sum()) if len(orphans) else 0,
        "annotation_total_attached": int(frame["n_annotations"].fillna(0).astype("int64").sum()),
        "annotation_total_orphan": int(orphans["annotation_count"].sum()) if len(orphans) else 0,
        "image_features_ok": int((frame["image_features_status"] == "ok").sum()),
        "image_features_missing_file": int((frame["image_features_status"] == "missing_file").sum()),
        "image_features_unreadable": int((frame["image_features_status"] == "unreadable").sum()),
        "image_features_not_attempted": int((frame["image_features_status"] == "not_attempted").sum()),
        "zero_annotation_records": int(frame["zero_annotation"].fillna(False).sum()),
        "alternate_version_records": int(frame["is_alternate_version"].fillna(False).sum()),
        "cross_split_overlap_records": int(frame["is_cross_split_overlap"].fillna(False).sum()),
    }
    return frame, diagnostics


def _cast_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast every column to the dtype promised by the feature dictionary."""
    frame = frame.copy()
    for entry in FEATURE_DICTIONARY:
        name, dtype = entry["name"], entry["dtype"]
        if name not in frame.columns:
            frame[name] = None
        if dtype == "BIGINT":
            frame[name] = frame[name].astype("Int64")
        elif dtype == "DOUBLE":
            frame[name] = frame[name].astype("float64")
        elif dtype == "BOOLEAN":
            frame[name] = frame[name].astype("boolean")
        else:
            frame[name] = frame[name].astype(object).where(frame[name].notna(), None)
    return frame[FEATURE_COLUMNS]


def _quantiles(series: pd.Series) -> Optional[Dict[str, Any]]:
    values = series.dropna()
    if values.empty:
        return None
    quantiles = values.quantile([0.0, 0.25, 0.5, 0.75, 1.0])
    return {
        "count": int(values.shape[0]),
        "min": float(quantiles.loc[0.0]),
        "q25": float(quantiles.loc[0.25]),
        "median": float(quantiles.loc[0.5]),
        "q75": float(quantiles.loc[0.75]),
        "max": float(quantiles.loc[1.0]),
        "mean": float(values.mean()),
        "std": float(values.std()) if values.shape[0] > 1 else None,
        "missing": int(series.isna().sum()),
    }


def build_summaries(frame: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Cell-line summary table plus per-feature quantile distributions."""
    labelled = frame.copy()
    labelled["cell_line"] = labelled["cell_line"].fillna("(unparsed)")

    grouped = labelled.groupby("cell_line", dropna=False)
    summary = pd.DataFrame(
        {
            "image_records": grouped.size(),
            "unique_filenames": grouped["file_name"].nunique(),
            "annotations": grouped["n_annotations"].sum(min_count=1),
            "mean_annotations": grouped["n_annotations"].mean(),
            "median_annotations": grouped["n_annotations"].median(),
            "min_annotations": grouped["n_annotations"].min(),
            "max_annotations": grouped["n_annotations"].max(),
            "zero_annotation_records": grouped["zero_annotation"].sum(),
            "alternate_version_records": grouped["is_alternate_version"].sum(),
            "cross_split_overlap_records": grouped["is_cross_split_overlap"].sum(),
            "image_features_missing": grouped["image_features_available"].apply(
                lambda values: int((~values.fillna(False)).sum())
            ),
            "pixel_std_mean": grouped["pixel_std"].mean(),
            "gradient_mean_mean": grouped["gradient_mean"].mean(),
        }
    ).reset_index().sort_values("cell_line", kind="stable").reset_index(drop=True)

    distributions: Dict[str, Any] = {"overall": {}, "by_cell_line": {}}
    for name in DISTRIBUTION_FEATURES:
        distributions["overall"][name] = _quantiles(frame[name])
        distributions["by_cell_line"][name] = {
            str(cell_line): _quantiles(group[name])
            for cell_line, group in labelled.groupby("cell_line", sort=True)
        }
    return summary, distributions


def _sql_str(value: str) -> str:
    """Quote a filesystem path as a DuckDB string literal (apostrophe-safe)."""
    return "'" + value.replace("'", "''") + "'"


def _write_frame_outputs(
    frame: pd.DataFrame,
    summary: pd.DataFrame,
    staging_dir: str,
) -> Dict[str, int]:
    """Write JSONL/CSV plus typed Parquet (via DuckDB, no PyArrow needed)."""
    sizes: Dict[str, int] = {}
    features_jsonl = os.path.join(staging_dir, "features.jsonl")
    frame.to_json(features_jsonl, orient="records", lines=True, force_ascii=False)
    sizes["features_jsonl"] = os.path.getsize(features_jsonl)

    summary_jsonl = os.path.join(staging_dir, "cell_line_summary.jsonl")
    summary.to_json(summary_jsonl, orient="records", lines=True, force_ascii=False)
    summary_csv = os.path.join(staging_dir, "cell_line_summary.csv")
    summary.to_csv(summary_csv, index=False)
    sizes["cell_line_summary_csv"] = os.path.getsize(summary_csv)

    columns_sql = ", ".join(f"'{c}': '{FEATURE_TYPES[c]}'" for c in FEATURE_COLUMNS)
    projection = ", ".join(FEATURE_COLUMNS)
    con = duckdb.connect()
    try:
        features_parquet = os.path.join(staging_dir, "features.parquet")
        con.execute(
            f"COPY (SELECT {projection} FROM read_json({_sql_str(features_jsonl)}, "
            f"columns={{{columns_sql}}}, format='newline_delimited')) "
            f"TO {_sql_str(features_parquet)} (FORMAT PARQUET, COMPRESSION zstd)"
        )
        sizes["features_parquet"] = os.path.getsize(features_parquet)

        summary_parquet = os.path.join(staging_dir, "cell_line_summary.parquet")
        con.execute(
            f"COPY (SELECT * FROM read_json({_sql_str(summary_jsonl)}, "
            "format='newline_delimited')) "
            f"TO {_sql_str(summary_parquet)} (FORMAT PARQUET, COMPRESSION zstd)"
        )
        sizes["cell_line_summary_parquet"] = os.path.getsize(summary_parquet)
    finally:
        con.close()
    os.remove(summary_jsonl)
    return sizes


def _require_completed_warehouse(warehouse_dir: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate a completed warehouse marker, manifest and exact stage/schema.

    A valid JSON marker is not sufficient: the marker must declare the expected
    stage and schema/view versions, carry a manifest checksum, and every file the
    manifest lists (database, Parquet, integrity, run) must match its size and
    SHA-256. This rejects a mixed database/marker pair and a modified database.
    """
    marker = load_completion_marker(warehouse_dir)
    if marker is None:
        raise FeatureError(
            f"not a completed warehouse run (missing or invalid {COMPLETION_MARKER}): "
            f"{warehouse_dir}"
        )
    if marker.get("stage") != "warehouse":
        raise FeatureError(
            f"expected a 'warehouse' stage marker, got {marker.get('stage')!r}"
        )
    if str(marker.get("schema_version")) != WAREHOUSE_SCHEMA_VERSION:
        raise FeatureError(
            f"warehouse schema_version mismatch: {marker.get('schema_version')!r} "
            f"!= {WAREHOUSE_SCHEMA_VERSION!r}"
        )
    if marker.get("view_version") != WAREHOUSE_VIEW_VERSION:
        raise FeatureError(
            f"warehouse view_version mismatch: {marker.get('view_version')!r} "
            f"!= {WAREHOUSE_VIEW_VERSION!r}"
        )
    if marker.get("partial"):
        raise FeatureError("warehouse run is marked partial; not usable for features")
    manifest_info = marker.get("manifest") or {}
    if not manifest_info.get("sha256"):
        raise FeatureError("warehouse marker has no manifest checksum")
    result = verify_manifest(
        warehouse_dir,
        stage="warehouse",
        expected_sha256=manifest_info.get("sha256"),
        required_paths=("audit.duckdb", "integrity.json", "run.json"),
    )
    if not result["ok"]:
        raise FeatureError(
            "warehouse manifest verification failed: " + "; ".join(result["problems"])
        )
    return marker, result["manifest"]


def _verify_warehouse_provenance(
    con: duckdb.DuckDBPyConnection,
    marker: Dict[str, Any],
    integrity: Optional[Dict[str, Any]],
) -> List[str]:
    """Cross-check the database, its marker and integrity.json provenance.

    Verifies the DB's own ``runs``/``sources`` source checksums and counts, the
    live table counts, and the integrity reconciliation all agree with the
    marker, so a tampered marker (or a database from a different run) is
    rejected instead of propagating false source provenance.
    """
    problems: List[str] = []
    expected_hashes = marker.get("source_sha256") or {}
    rows = con.execute(
        "SELECT run_id, source_sha256_train, source_sha256_val, source_sha256_test, "
        "total_image_records, total_annotations, unique_filenames FROM lc.runs"
    ).fetchall()
    if len(rows) != 1:
        problems.append(f"expected exactly one runs row, found {len(rows)}")
    else:
        _run_id, h_train, h_val, h_test, n_img, n_ann, n_fn = rows[0]
        for split, value in (("train", h_train), ("val", h_val), ("test", h_test)):
            if expected_hashes.get(split) != value:
                problems.append(
                    f"runs.source_sha256_{split}={value!r} != marker "
                    f"{expected_hashes.get(split)!r}"
                )
        for label, value, expected in (
            ("total_image_records", n_img, marker.get("total_image_records")),
            ("total_annotations", n_ann, marker.get("total_annotations")),
            ("unique_filenames", n_fn, marker.get("unique_filenames")),
        ):
            if value != expected:
                problems.append(f"runs.{label}={value!r} != marker {expected!r}")
    for split, sha in con.execute("SELECT split, sha256 FROM lc.sources").fetchall():
        if expected_hashes.get(split) != sha:
            problems.append(
                f"sources.sha256[{split}]={sha!r} != marker {expected_hashes.get(split)!r}"
            )
    images = con.execute("SELECT COUNT(*) FROM lc.images").fetchone()[0]
    annotations = con.execute("SELECT COUNT(*) FROM lc.annotations").fetchone()[0]
    filenames = con.execute("SELECT COUNT(DISTINCT file_name) FROM lc.images").fetchone()[0]
    for label, value, expected in (
        ("images", images, marker.get("total_image_records")),
        ("annotations", annotations, marker.get("total_annotations")),
        ("unique_filenames", filenames, marker.get("unique_filenames")),
    ):
        if value != expected:
            problems.append(f"database {label} count={value!r} != marker {expected!r}")
    if integrity is not None:
        if (integrity.get("input") or {}).get("source_sha256") != expected_hashes:
            problems.append("integrity.input.source_sha256 != marker.source_sha256")
        reconciliation = integrity.get("reconciliation") or {}
        for key, name in (
            ("images", "total_image_records"),
            ("annotations", "total_annotations"),
            ("unique_filenames", "unique_filenames"),
        ):
            loaded = (reconciliation.get(key) or {}).get("loaded")
            if loaded != marker.get(name):
                problems.append(
                    f"integrity.reconciliation.{key}.loaded={loaded!r} != marker {name}"
                )
    return problems


def run_features(config: FeatureConfig) -> Dict[str, Any]:
    """Build the feature table and summaries; return the run record."""
    started = time.perf_counter()
    warehouse_dir = os.path.abspath(config.warehouse_dir)

    out_dir = os.path.abspath(config.out_dir)
    marker_path = os.path.join(out_dir, COMPLETION_MARKER)
    staging_dir = os.path.join(out_dir, STAGING_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    try:
        # Invalidate any previous completion evidence before any fallible work,
        # including warehouse validation, so a failed rerun into a reused
        # directory can never keep looking like a completed run.
        clear_stale(out_dir)
        marker, warehouse_manifest = _require_completed_warehouse(warehouse_dir)
        db_path = os.path.join(warehouse_dir, "audit.duckdb")
        integrity = read_json(os.path.join(warehouse_dir, "integrity.json"))

        os.makedirs(staging_dir)

        con = duckdb.connect(db_path, read_only=True)
        try:
            problems = _verify_warehouse_provenance(con, marker, integrity)
            if problems:
                raise FeatureError(
                    "warehouse provenance verification failed: " + "; ".join(problems)
                )
            run_row = con.execute("SELECT run_id FROM lc.runs LIMIT 1").fetchone()
            run_id = run_row[0] if run_row else None
            build_started = time.perf_counter()
            frame, diagnostics = build_feature_frame(con, config)
            build_seconds = time.perf_counter() - build_started
            summary, distributions = build_summaries(frame)
        finally:
            con.close()

        partial = bool(diagnostics.get("partial"))
        mode = "partial" if partial else "full"

        sizes = _write_frame_outputs(frame, summary, staging_dir)
        dump_json(
            os.path.join(staging_dir, "cell_line_summary.json"),
            {
                "grouping": "cell_line",
                "mode": mode,
                "note": (
                    "Acquisition metadata is context, not proof of biological "
                    "growth; annotation counts are label evidence, not cell counts. "
                    "Ambiguous annotation joins are excluded (null), not doubled."
                ),
                "summary": jsonable_records(summary),
            },
        )
        dump_json(os.path.join(staging_dir, "feature_distributions.json"), distributions)
        dump_json(
            os.path.join(staging_dir, "feature_dictionary.json"),
            {
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "mode": mode,
                "image_statistics": IMAGE_STATISTICS_CONTRACT,
                "source_manifest": {
                    "audit_run_id": run_id,
                    "source_sha256": marker.get("source_sha256", {}),
                    "warehouse_schema_version": marker.get("schema_version"),
                    "warehouse_view_version": marker.get("view_version"),
                },
                "features": FEATURE_DICTIONARY,
            },
        )

        total_seconds = time.perf_counter() - started
        run_record = {
            "tool": {"name": "livecell-audit", "version": __version__},
            "stage": "features",
            "mode": mode,
            "partial": partial,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "warehouse_dir": warehouse_dir,
            "out_dir": out_dir,
            "data_root": os.path.abspath(config.data_root) if config.data_root else None,
            "max_filenames": config.max_filenames,
            "warehouse_schema_version": marker.get("schema_version"),
            "warehouse_view_version": marker.get("view_version"),
            "duckdb_version": duckdb.__version__,
            "pandas_version": pd.__version__,
            "numpy_version": np.__version__,
            "runtime_seconds": round(total_seconds, 3),
            "runtime_method": "time.perf_counter() around load, join, TIFF reads and writes",
            "build_seconds": round(build_seconds, 3),
            "diagnostics": diagnostics,
            "output_sizes_bytes": sizes,
        }
        dump_json(os.path.join(staging_dir, "run.json"), run_record)

        manifest = build_manifest(
            "features",
            FEATURE_SCHEMA_VERSION,
            staging_dir,
            FEATURE_OUTPUT_PATHS,
            extra={"mode": mode, "partial": partial},
        )
        dump_json(os.path.join(staging_dir, MANIFEST_NAME), manifest)
        manifest_digest, manifest_bytes = sha256_file(
            os.path.join(staging_dir, MANIFEST_NAME)
        )

        promote_staged(staging_dir, out_dir)
        write_completion_marker(
            marker_path,
            {
                "status": "completed",
                "stage": "features",
                "mode": mode,
                "partial": partial,
                "tool": {"name": "livecell-audit", "version": __version__},
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "warehouse_dir": warehouse_dir,
                "feature_records": int(len(frame)),
                "distinct_filenames": diagnostics["filenames_read"],
                "annotations_attached": diagnostics["annotation_total_attached"],
                "source_sha256": marker.get("source_sha256", {}),
                "warehouse_manifest_sha256": (marker.get("manifest") or {}).get("sha256"),
                "manifest": {
                    "file": MANIFEST_NAME,
                    "sha256": manifest_digest,
                    "bytes": manifest_bytes,
                },
                "outputs": {
                    "features_parquet": "features.parquet",
                    "features_jsonl": "features.jsonl",
                    "cell_line_summary": "cell_line_summary.json",
                    "feature_dictionary": "feature_dictionary.json",
                    "run": "run.json",
                    "manifest": MANIFEST_NAME,
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
        "diagnostics": diagnostics,
        "marker": read_json(marker_path),
        "out_dir": out_dir,
    }


def verify_features_outputs(out_dir: str) -> Dict[str, Any]:
    """Verify a completed feature run's marker and content manifest.

    Returns ``{"ok": bool, "problems": [...], "marker": ..., "manifest": ...}``
    for a consumer that wants to inspect rather than raise. A partial run is
    reported via ``problems`` so it cannot be published as a release dataset.
    """
    out_dir = os.path.abspath(out_dir)
    marker = load_completion_marker(out_dir)
    if marker is None:
        return {"ok": False, "problems": [f"not a completed feature run: {out_dir}"], "marker": None, "manifest": None}
    problems: List[str] = []
    if marker.get("stage") != "features":
        problems.append(f"unexpected stage {marker.get('stage')!r}")
    if str(marker.get("feature_schema_version")) != FEATURE_SCHEMA_VERSION:
        problems.append(
            f"feature_schema_version {marker.get('feature_schema_version')!r} "
            f"!= {FEATURE_SCHEMA_VERSION!r}"
        )
    if marker.get("partial"):
        problems.append("feature run is partial (development max_filenames); not release-ready")
    manifest_info = marker.get("manifest") or {}
    if not manifest_info.get("sha256"):
        problems.append("marker has no manifest checksum")
        return {"ok": False, "problems": problems, "marker": marker, "manifest": None}
    result = verify_manifest(
        out_dir,
        stage="features",
        expected_sha256=manifest_info.get("sha256"),
        required_paths=("features.parquet", "feature_dictionary.json"),
    )
    problems.extend(result["problems"])
    return {
        "ok": not problems,
        "problems": problems,
        "marker": marker,
        "manifest": result["manifest"],
    }


def assert_release_ready(out_dir: str) -> Dict[str, Any]:
    """Return the marker of a verified, full feature run or raise FeatureError.

    Consumers (for example the later scikit-learn stage) call this to reject a
    partial or tampered feature directory before using it.
    """
    result = verify_features_outputs(out_dir)
    if not result["ok"]:
        raise FeatureError(
            "feature outputs are not release-ready: " + "; ".join(result["problems"])
        )
    return result["marker"]


def jsonable_records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert a summary frame to plain JSON records with native types."""
    records: List[Dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        cleaned: Dict[str, Any] = {}
        for key, value in row.items():
            if value is None or (isinstance(value, float) and pd.isna(value)):
                cleaned[key] = None
            elif pd.isna(value):
                cleaned[key] = None
            elif hasattr(value, "item"):
                cleaned[key] = value.item()
            else:
                cleaned[key] = value
        records.append(cleaned)
    return records
