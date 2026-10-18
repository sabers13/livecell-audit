"""Milestone 4: evaluated review ranking (baselines + Isolation Forest).

Scientific question
-------------------
At the *same* review budget, does an Isolation Forest ranking offer useful
prioritisation compared with simple baselines? A negative result is valid.

Scores produced here indicate *unusualness*, never a probability that a label is
wrong. They are a review aid: a sparse image may be annotated correctly.

Protocol (declared and frozen before any conclusion)
----------------------------------------------------
- seed ``42`` default; sensitivity seeds ``7``/``42``/``101``;
- review budgets ``10``/``25``/``50`` images;
- Isolation Forest ``n_estimators=200``, ``max_samples=min(256, n_fit)``,
  ``contamination='auto'``, ``n_jobs=1``;
- preprocessing (median imputation + robust scaling) and models are fitted on
  **fit rows only** — never on held-out, validation, test or known-case rows;
- one canonical annotation version per filename for fitting and budgeted
  review, chosen deterministically: prefer train, then val, then test; within a
  split the lowest original id, then the lowest record position. This is a
  modelling policy, not proof that the chosen annotation is correct. All
  versions are retained in the provenance export with their count range.

Grouping and partition
----------------------
Images are grouped by ``(cell_line, well, field)`` so time points, crops and
alternate versions stay together. Within each cell line the groups are split
deterministically into approximately 75% fit / 25% held-out (at least one group
on each side when possible). This is a practical leakage proxy, not proof of
biological independence. Rows whose filename cannot be parsed are never merged
into a normal group: each unparsed filename is its own conservative group.

Model choice
------------
A per-cell-line Isolation Forest is used where the cell line has at least
``MIN_CELL_LINE_FIT_IMAGES`` fit images; otherwise a single documented global
model fitted on *all fit rows* scores the row. Robust-statistics baselines use
the same fit-only fallback rule. No model is ever fitted on validation, test or
held-out rows.

Global queue
------------
Scores from separate per-cell-line models are not automatically comparable, so
each method's priority is an empirical percentile against a **fit-only**
reference distribution for the model that produced the score (see
:func:`empirical_percentile`). The raw score is preserved alongside. The
priority is a relative review rank, not an error probability.
"""

from __future__ import annotations

import os
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    count_lines,
    dump_json,
    load_completion_marker,
    promote_staged,
    read_json,
    sha256_file,
    verify_manifest,
    write_completion_marker,
)
from .errors import FeatureError, RankingError
from .features import (
    ANNOTATION_FEATURE_COLUMNS,
    WAREHOUSE_MEMORY_LIMIT_MB,
    WAREHOUSE_THREADS,
    aggregate_annotation_features,
    aggregate_annotation_features_sql,
    assert_release_ready,
    connect_warehouse,
)
from .known_cases import KNOWN_SPARSE_CASES
from .warehouse import SCHEMA_VERSION as WAREHOUSE_SCHEMA_VERSION
from .warehouse import VIEW_VERSION as WAREHOUSE_VIEW_VERSION

RANKING_SCHEMA_VERSION = "1"

#: Declared, frozen protocol (see the module docstring).
DEFAULT_SEED = 42
SENSITIVITY_SEEDS: Tuple[int, ...] = (7, 42, 101)
BUDGETS: Tuple[int, ...] = (10, 25, 50)
IF_N_ESTIMATORS = 200
IF_MAX_SAMPLES = 256
IF_CONTAMINATION = "auto"
IF_N_JOBS = 1
MIN_CELL_LINE_FIT_IMAGES = 20
PARTITION_HOLDOUT_FRACTION = 0.25
PARTITION_SEED = 42

#: Nine known sparse-label regression filenames; every record with one of these
#: filenames is excluded from fitting and preprocessing.
KNOWN_CASE_FILENAMES = frozenset(str(case["file_name"]) for case in KNOWN_SPARSE_CASES)

METHODS: Tuple[str, ...] = ("low_count", "robust", "isolation_forest", "random")

#: Numeric observed features used as model predictors. Identifier, split,
#: acquisition-context and bookkeeping columns are deliberately excluded (see
#: :data:`EXCLUDED_FEATURES`). Annotation-derived features may be null (zero
#: labels) and are imputed from fit rows; image-only features describe the
#: stored TIFF pixels.
MODEL_FEATURES: Tuple[str, ...] = (
    "n_annotations",
    "area_sum",
    "area_mean",
    "area_median",
    "area_min",
    "area_max",
    "area_std",
    "bbox_width_mean",
    "bbox_width_median",
    "bbox_height_mean",
    "bbox_height_median",
    "n_points_mean",
    "n_points_median",
    "n_points_min",
    "n_points_max",
    "n_points_sum",
    "pixel_mean",
    "pixel_std",
    "gradient_mean",
    "pixel_min",
    "pixel_max",
    "width",
    "height",
    "image_area_pixels",
)

#: Every feature-table column deliberately NOT used as a predictor, with the
#: reason, published in the model card.
EXCLUDED_FEATURES: Dict[str, str] = {
    "split": "split identity, not an image property",
    "record_position": "row key",
    "record_key": "row key",
    "image_id": "original COCO id (identifier)",
    "file_name": "filename (identifier)",
    "original_filename": "filename (identifier)",
    "cell_line": "acquisition/grouping context",
    "well": "acquisition context",
    "well_row": "acquisition context",
    "well_col": "acquisition context",
    "field": "acquisition context",
    "timestamp": "acquisition context",
    "time_hours": "acquisition context",
    "crop": "acquisition context",
    "filename_parsed": "metadata flag",
    "versions_in_split": "version bookkeeping",
    "records_for_filename": "version bookkeeping",
    "splits_for_filename": "version bookkeeping",
    "is_alternate_version": "version bookkeeping",
    "is_cross_split_overlap": "version bookkeeping",
    "annotation_join_status": "join bookkeeping",
    "dimensions_status": "audit bookkeeping",
    "dimensions_match": "audit bookkeeping",
    "local_width": "local TIFF dimension check (nullable)",
    "local_height": "local TIFF dimension check (nullable)",
    "image_features_status": "computation status",
    "image_features_available": "computation status",
    "has_image_file": "availability flag",
    "has_annotations": "derived from n_annotations",
    "zero_annotation": "derived from n_annotations",
    "metadata_complete": "metadata flag",
}

ROBUST_SCALE_FACTOR = 1.4826  # MAD -> robust standard-deviation scale for a normal
ANNOTATION_REPRODUCTION_TOLERANCE = 1e-6
#: Number of images sampled to verify the pandas annotation aggregation against
#: the warehouse view on the real data. Bounded so the check does not materialise
#: the whole 1.66 M-row annotation table in memory; the focused tests check
#: equality exhaustively on a fixture.
ANNOTATION_REPRODUCTION_SAMPLE_SIZE = 500

#: Deterministic scientific exports (byte-identical across reruns).
RANKING_OUTPUT_PATHS: Tuple[str, ...] = (
    "candidates.jsonl",
    "candidates.csv",
    "candidates.parquet",
    "version_provenance.jsonl",
    "partition_manifest.json",
    "model_card.json",
    "fit_reference.json",
    "summary.json",
    "run.json",
)

#: Required files a consumer must find before trusting a ranking run.
RANKING_REQUIRED_OUTPUTS: Tuple[str, ...] = (
    "candidates.jsonl",
    "candidates.parquet",
    "partition_manifest.json",
    "model_card.json",
    "fit_reference.json",
    "summary.json",
    "run.json",
)

#: Identity/context columns published with every candidate row.
CANDIDATE_IDENTITY_COLUMNS: Tuple[str, ...] = (
    "record_key",
    "file_name",
    "split",
    "image_id",
    "record_position",
    "cell_line",
    "well",
    "field",
    "crop",
    "time_hours",
    "acquisition_group",
    "is_known_case",
    "canonical_rule",
    "versions_total",
    "versions_eligible",
)


@dataclass
class RankingConfig:
    """Inputs for one ranking build."""

    features_dir: str
    warehouse_dir: str
    out_dir: str
    seed: int = DEFAULT_SEED
    budgets: Tuple[int, ...] = BUDGETS
    if_n_estimators: int = IF_N_ESTIMATORS
    if_max_samples: int = IF_MAX_SAMPLES
    if_contamination: str = IF_CONTAMINATION
    if_n_jobs: int = IF_N_JOBS


# --------------------------------------------------------------------------- #
# input verification: completed feature run + matching warehouse
# --------------------------------------------------------------------------- #


def require_verified_inputs(
    features_dir: str, warehouse_dir: str
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Verify a completed feature run and the warehouse it was built from.

    ``features.assert_release_ready`` reconciles the feature marker with its
    protected manifest, dictionary, diagnostics and actual feature table. On top
    of that this checks the warehouse identity the feature marker recorded:

    - the warehouse marker is an exact, non-partial ``warehouse`` stage;
    - the warehouse ``manifest.json`` SHA-256 equals the feature marker's
      ``warehouse_manifest_sha256`` (the database is the one the features came
      from, not a different run with fitting column names);
    - the warehouse marker's ``source_sha256`` equals the feature marker's, and
      every warehouse manifest file matches its size/SHA-256.
    """
    features_dir = os.path.abspath(features_dir)
    warehouse_dir = os.path.abspath(warehouse_dir)
    try:
        feature_marker = assert_release_ready(features_dir)
    except FeatureError as exc:
        raise RankingError(f"feature inputs are not release-ready: {exc}") from exc

    dictionary = read_json(os.path.join(features_dir, "feature_dictionary.json"))

    warehouse_marker = load_completion_marker(warehouse_dir)
    if warehouse_marker is None:
        raise RankingError(f"not a completed warehouse run: {warehouse_dir}")
    if warehouse_marker.get("stage") != "warehouse":
        raise RankingError(
            f"expected a 'warehouse' stage marker, got {warehouse_marker.get('stage')!r}"
        )
    if warehouse_marker.get("partial"):
        raise RankingError("warehouse run is marked partial; not usable for ranking")
    if str(warehouse_marker.get("schema_version")) != WAREHOUSE_SCHEMA_VERSION:
        raise RankingError(
            f"warehouse schema_version {warehouse_marker.get('schema_version')!r} "
            f"!= {WAREHOUSE_SCHEMA_VERSION!r}"
        )
    if warehouse_marker.get("view_version") != WAREHOUSE_VIEW_VERSION:
        raise RankingError(
            f"warehouse view_version {warehouse_marker.get('view_version')!r} "
            f"!= {WAREHOUSE_VIEW_VERSION!r}"
        )

    manifest_path = os.path.join(warehouse_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise RankingError(f"warehouse manifest is missing: {manifest_path}")
    warehouse_manifest_sha, _ = sha256_file(manifest_path)
    recorded = feature_marker.get("warehouse_manifest_sha256")
    if recorded != warehouse_manifest_sha:
        raise RankingError(
            "warehouse does not match the feature run: warehouse manifest SHA-256 "
            f"{warehouse_manifest_sha} != feature marker {recorded}"
        )
    if warehouse_marker.get("source_sha256") != feature_marker.get("source_sha256"):
        raise RankingError(
            "warehouse source hashes do not match the feature run: refusing to "
            "combine a database from a different run"
        )
    if (dictionary.get("source_manifest") or {}).get("source_sha256") != feature_marker.get(
        "source_sha256"
    ):
        raise RankingError("feature dictionary provenance disagrees with the feature marker")

    verification = verify_manifest(
        warehouse_dir,
        stage="warehouse",
        expected_sha256=(warehouse_marker.get("manifest") or {}).get("sha256"),
        required_paths=("audit.duckdb", "integrity.json", "run.json"),
    )
    if not verification["ok"]:
        raise RankingError(
            "warehouse manifest verification failed: " + "; ".join(verification["problems"])
        )
    return feature_marker, dictionary, warehouse_marker


def load_feature_frame(features_dir: str) -> pd.DataFrame:
    """Read the completed feature table from its Parquet export."""
    path = os.path.join(os.path.abspath(features_dir), "features.parquet")
    con = duckdb.connect()
    try:
        frame = con.execute(
            "SELECT * FROM read_parquet(" + _sql_str(path) + ")"
        ).df()
    finally:
        con.close()
    if frame.empty:
        raise RankingError("feature table is empty")
    return frame


def load_annotations(
    warehouse_dir: str, keys: Optional[Sequence[Tuple[str, int]]] = None
) -> pd.DataFrame:
    """Read raw annotation records (for recomputation) from the warehouse.

    ``record_position`` is the warehouse's unique annotation-row identity and is
    used for the synthetic retention mask so duplicate annotation ids (which the
    warehouse preserves) can never multiply kept rows.

    ``keys`` restricts the read to a set of ``(split, image_id)`` images. Passing
    a bounded key set is the memory-safe path: the full annotation table is
    1.66 M rows and materialising an object ``bbox`` column for all of it is the
    dominant memory cost, so callers only request the images they need. Callers
    that only need to *re-aggregate* annotations should use
    :func:`aggregate_annotation_features_sql` instead, which never materialises
    the annotation rows at all.
    """
    path = os.path.join(os.path.abspath(warehouse_dir), "audit.duckdb")
    con = connect_warehouse(path)
    try:
        if keys is None:
            return con.execute(
                "SELECT split, record_position, image_id, id, area, bbox, n_points "
                "FROM lc.annotations"
            ).df()
        pairs = sorted({(str(split), int(image_id)) for split, image_id in keys})
        if not pairs:
            return con.execute(
                "SELECT split, record_position, image_id, id, area, bbox, n_points "
                "FROM lc.annotations WHERE FALSE"
            ).df()
        con.execute("CREATE TEMP TABLE sel(split VARCHAR, image_id BIGINT)")
        con.executemany("INSERT INTO sel VALUES (?, ?)", pairs)
        return con.execute(
            "SELECT a.split, a.record_position, a.image_id, a.id, a.area, a.bbox, a.n_points "
            "FROM lc.annotations a JOIN sel s USING (split, image_id)"
        ).df()
    finally:
        con.close()


def load_annotation_identities(
    warehouse_dir: str, keys: Optional[Sequence[Tuple[str, int]]] = None
) -> pd.DataFrame:
    """Read only the identity columns of a bounded set of images' annotations.

    Returns ``split, image_id, record_position, id`` — no ``area``/``bbox``
    payload — so the annotation-identity plan of a large image set can be built
    without materialising the annotation geometry. ``record_position`` is the
    unique annotation-row identity; duplicate annotation ids (preserved by the
    warehouse) are never treated as a key.
    """
    path = os.path.join(os.path.abspath(warehouse_dir), "audit.duckdb")
    con = connect_warehouse(path)
    try:
        if keys is None:
            return con.execute(
                "SELECT split, image_id, record_position, id FROM lc.annotations"
            ).df()
        pairs = sorted({(str(split), int(image_id)) for split, image_id in keys})
        if not pairs:
            return con.execute(
                "SELECT split, image_id, record_position, id FROM lc.annotations WHERE FALSE"
            ).df()
        con.execute("CREATE TEMP TABLE sel(split VARCHAR, image_id BIGINT)")
        con.executemany("INSERT INTO sel VALUES (?, ?)", pairs)
        return con.execute(
            "SELECT a.split, a.image_id, a.record_position, a.id "
            "FROM lc.annotations a JOIN sel s USING (split, image_id)"
        ).df()
    finally:
        con.close()


def load_annotation_aggregates(
    warehouse_dir: str,
    keys: Optional[Sequence[Tuple[str, int]]] = None,
    record_positions: Optional[Sequence[int]] = None,
) -> pd.DataFrame:
    """Aggregate a bounded annotation subset in SQL over the warehouse.

    Delegates to :func:`features.aggregate_annotation_features_sql` with the
    warehouse opened under the job's thread/memory caps, so a recomputation
    reads only the selected rows and returns only the per-image aggregates.
    """
    path = os.path.join(os.path.abspath(warehouse_dir), "audit.duckdb")
    con = connect_warehouse(path)
    try:
        return aggregate_annotation_features_sql(
            con, keys=keys, record_positions=record_positions
        )
    finally:
        con.close()


def load_annotation_stats(warehouse_dir: str) -> pd.DataFrame:
    """Read the warehouse's per-image annotation aggregate view."""
    path = os.path.join(os.path.abspath(warehouse_dir), "audit.duckdb")
    con = connect_warehouse(path)
    try:
        frame = con.execute("SELECT * FROM lc.v1_image_annotation_stats").df()
    finally:
        con.close()
    return frame


def feature_build_identity(feature_marker: Dict[str, Any]) -> Dict[str, Any]:
    """The exact feature/warehouse build identity a downstream stage must match.

    The feature marker's ``manifest.sha256`` protects every feature output byte
    (so a re-built feature table over changed TIFF pixels has a different
    identity even when the COCO source hashes are unchanged), and
    ``warehouse_manifest_sha256`` pins the database the features came from.
    """
    manifest = feature_marker.get("manifest") or {}
    return {
        "source_sha256": feature_marker.get("source_sha256"),
        "feature_schema_version": feature_marker.get("feature_schema_version"),
        "feature_records": feature_marker.get("feature_records"),
        "feature_manifest_sha256": manifest.get("sha256"),
        "warehouse_manifest_sha256": feature_marker.get("warehouse_manifest_sha256"),
    }


def assert_build_consistency(
    features_dir: str,
    ranking_dir: Optional[str] = None,
    evaluation_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Require evaluation/ranking/feature runs to come from the *same* builds.

    A downstream consumer (for example a website bundle exporter) must call this
    before combining artifacts. It rejects mixing a ranking or evaluation run
    with a feature build that has the same COCO source hashes but a different
    feature-manifest identity (for example after TIFF pixels changed), and the
    same for the warehouse identity.
    """
    feature_marker = assert_release_ready(os.path.abspath(features_dir))
    identity = feature_build_identity(feature_marker)
    if not identity["feature_manifest_sha256"]:
        raise RankingError("feature marker has no manifest identity")

    if ranking_dir is not None:
        ranking_marker = assert_ranking_ready(os.path.abspath(ranking_dir))
        recorded = ranking_marker.get("feature_build") or {}
        for key in (
            "source_sha256",
            "feature_manifest_sha256",
            "warehouse_manifest_sha256",
        ):
            if recorded.get(key) != identity.get(key):
                raise RankingError(
                    f"ranking {key} {recorded.get(key)!r} != feature build "
                    f"{identity.get(key)!r}; refusing to mix different builds"
                )
    if evaluation_dir is not None:
        evaluation_marker = load_completion_marker(os.path.abspath(evaluation_dir))
        if evaluation_marker is None or evaluation_marker.get("stage") != "evaluation":
            raise RankingError(f"not a completed evaluation run: {evaluation_dir}")
        recorded = evaluation_marker.get("feature_build") or {}
        for key in (
            "source_sha256",
            "feature_manifest_sha256",
            "warehouse_manifest_sha256",
        ):
            if recorded.get(key) != identity.get(key):
                raise RankingError(
                    f"evaluation {key} {recorded.get(key)!r} != feature build "
                    f"{identity.get(key)!r}; refusing to mix different builds"
                )
        ranking_build = evaluation_marker.get("ranking_build") or {}
        if ranking_build.get("feature_manifest_sha256") != identity["feature_manifest_sha256"]:
            raise RankingError(
                "evaluation ranking build does not match the feature build"
            )
    return identity


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------- #
# canonicalisation, grouping, partition
# --------------------------------------------------------------------------- #


def canonicalize_versions(
    frame: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Choose one eligible annotation version per filename, deterministically.

    Returns ``(canonical, provenance)``. ``canonical`` has one row per filename
    (the chosen version) and ``provenance`` records, for every filename, the
    chosen record and every available version with its annotation count, so the
    choice and the count range are fully exported. A filename with no eligible
    version (ambiguous join or unavailable image statistics) has no canonical
    row and an explicit reason in the provenance.
    """
    work = frame.copy()
    work["split_rank_num"] = work["split"].map(
        {"train": 0, "val": 1, "test": 2}
    ).fillna(99).astype("int64")
    work["eligible_version"] = (
        (work["annotation_join_status"] == "ok")
        & (work["image_features_status"] == "ok")
    )

    eligible = work[work["eligible_version"]].sort_values(
        ["split_rank_num", "image_id", "record_position"], kind="stable"
    )
    canonical = eligible.drop_duplicates(subset=["file_name"], keep="first").copy()
    canonical["canonical_rule"] = "prefer_train_then_val_then_test;lowest_id_then_position"

    provenance_rows: List[Dict[str, Any]] = []
    for file_name, group in work.groupby("file_name", sort=True):
        group = group.sort_values(
            ["split_rank_num", "image_id", "record_position"], kind="stable"
        )
        chosen = group[group["eligible_version"]].iloc[0] if group["eligible_version"].any() else None
        versions = [
            {
                "record_key": str(row.record_key),
                "split": str(row.split),
                "image_id": int(row.image_id),
                "record_position": int(row.record_position),
                "n_annotations": (
                    int(row.n_annotations) if pd.notna(row.n_annotations) else None
                ),
                "annotation_join_status": str(row.annotation_join_status),
                "image_features_status": str(row.image_features_status),
                "eligible": bool(row.eligible_version),
            }
            for row in group.itertuples(index=False)
        ]
        counts = [v["n_annotations"] for v in versions if v["n_annotations"] is not None]
        if chosen is None:
            if any(v["annotation_join_status"] != "ok" for v in versions):
                exclusion_reason = "ambiguous_annotation_join"
            else:
                statuses = sorted(
                    {v["image_features_status"] for v in versions if v["image_features_status"] != "ok"}
                )
                exclusion_reason = "image_features_" + (statuses[0] if statuses else "unavailable")
        else:
            exclusion_reason = None
        provenance_rows.append(
            {
                "file_name": str(file_name),
                "chosen_record_key": None if chosen is None else str(chosen.record_key),
                "chosen_split": None if chosen is None else str(chosen.split),
                "chosen_image_id": None if chosen is None else int(chosen.image_id),
                "chosen_record_position": (
                    None if chosen is None else int(chosen.record_position)
                ),
                "chosen_n_annotations": (
                    None
                    if chosen is None or pd.notna(chosen.n_annotations) is False
                    else int(chosen.n_annotations)
                ),
                "selection_rule": (
                    "prefer_train_then_val_then_test;lowest_id_then_position"
                    if chosen is not None
                    else None
                ),
                "selection_status": "chosen" if chosen is not None else "no_eligible_version",
                "exclusion_reason": exclusion_reason,
                "n_versions_total": len(versions),
                "n_versions_eligible": int(group["eligible_version"].sum()),
                "annotation_count_min": min(counts) if counts else None,
                "annotation_count_max": max(counts) if counts else None,
                "versions": versions,
            }
        )
    provenance = pd.DataFrame(provenance_rows)
    canonical = canonical.drop(columns=["split_rank_num", "eligible_version"]).reset_index(drop=True)
    return canonical, provenance


def acquisition_group(row: pd.Series) -> str:
    """Return the acquisition group id for one record.

    Parsed filenames group by ``(cell_line, well, field)`` so time points, crops
    and alternate versions stay together. An unparsed filename is its own
    conservative group (``unparsed|<file_name>``) and is never merged into a
    normal group.
    """
    cell_line, well, field = row.get("cell_line"), row.get("well"), row.get("field")
    if pd.notna(cell_line) and pd.notna(well) and pd.notna(field):
        return f"{cell_line}|{well}|field{int(field)}"
    return f"unparsed|{row.get('file_name')}"


def _add_groups(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["acquisition_group"] = [
        acquisition_group(row) for _, row in frame.iterrows()
    ]
    frame["is_known_case"] = frame["file_name"].isin(KNOWN_CASE_FILENAMES)
    return frame


def _rng_permutation(items: Sequence[str], seed: int) -> List[str]:
    ordered = sorted(str(item) for item in items)
    if len(ordered) <= 1:
        return ordered
    rng = np.random.default_rng(seed)
    return [ordered[i] for i in rng.permutation(len(ordered))]


def partition_groups(
    analysis: pd.DataFrame,
    holdout_fraction: float = PARTITION_HOLDOUT_FRACTION,
    seed: int = PARTITION_SEED,
) -> Dict[str, Any]:
    """Split each cell line's training acquisition groups ~75/25 (fit/hold-out).

    Only eligible official *training* rows take part. Every cell line with at
    least two groups keeps at least one fit and one held-out group. The split is
    deterministic for a fixed seed. Returns the partition manifest payload; the
    per-record assignment is attached by :func:`build_analysis_state`.
    """
    train = analysis[analysis["split"] == "train"]
    cell_lines: Dict[str, Any] = {}
    for line, group in train.groupby("cell_line", dropna=False):
        key = "(unparsed)" if pd.isna(line) else str(line)
        cells = group[~group["is_known_case"]]
        groups = sorted(cells["acquisition_group"].unique().tolist())
        n = len(groups)
        if n == 0:
            fit_groups: List[str] = []
            holdout_groups: List[str] = []
        elif n == 1:
            fit_groups, holdout_groups = groups, []
        else:
            n_holdout = max(1, int(round(n * holdout_fraction)))
            n_holdout = min(n_holdout, n - 1)
            permuted = _rng_permutation(groups, seed)
            holdout_groups = sorted(permuted[:n_holdout])
            fit_groups = sorted(permuted[n_holdout:])
        cell_lines[key] = {
            "partition_key": key,
            "cell_line": None if pd.isna(line) else str(line),
            "n_groups": n,
            "fit_groups": fit_groups,
            "holdout_groups": holdout_groups,
            "n_fit_images": int(cells["acquisition_group"].isin(fit_groups).sum()),
            "n_holdout_images": int(cells["acquisition_group"].isin(holdout_groups).sum()),
            "n_known_case_records_excluded": int(group["is_known_case"].sum()),
        }
    return {
        "policy": (
            "group related images by (cell_line, well, field); keep time points, "
            "crops and alternate annotation versions together; split each cell "
            "line's eligible training groups deterministically ~75/25 with at "
            "least one fit and one held-out group when possible"
        ),
        "caveat": (
            "practical leakage proxy only, not proof of biological independence; "
            "with 4-6 training acquisition groups per cell line a held-out group "
            "is a coarse holdout"
        ),
        "holdout_fraction_target": holdout_fraction,
        "partition_seed": seed,
        "group_definition": "cell_line|well|field; unparsed filenames are their own group",
        "missing_metadata_policy": (
            "an unparsed filename becomes group 'unparsed|<file_name>' and is never "
            "merged into a normal acquisition group"
        ),
        "cell_lines": cell_lines,
    }


# --------------------------------------------------------------------------- #
# fit-only reference statistics and models
# --------------------------------------------------------------------------- #


def _numeric_matrix(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    """Return a ``float64`` matrix, converting nullable integers to NaN."""
    if not columns:
        return np.empty((len(frame), 0), dtype="float64")
    block = frame.loc[:, list(columns)]
    return block.astype("float64").to_numpy(dtype="float64")


def _numeric_values(series: pd.Series) -> np.ndarray:
    return series.astype("float64").to_numpy(dtype="float64", na_value=np.nan)


def empirical_percentile(value: float, sorted_reference: Sequence[float]) -> Optional[float]:
    """Mid-rank empirical percentile of ``value`` in a sorted reference.

    Higher values get higher priorities. ``(less + 0.5 * equal) / n`` gives a
    stable value in ``[0, 1]``. With a *finite* fit-only reference the maximum
    achievable priority is ``1.0``: any score at or above the reference maximum
    shares it, and the reference maximum itself (when unique) maps to
    ``(n - 0.5) / n``. This tail tie is documented, not hidden; a percentile is
    a relative review priority, not an error probability.
    """
    n = len(sorted_reference)
    if n == 0:
        return None
    less = bisect_left(sorted_reference, value)
    equal = bisect_right(sorted_reference, value) - less
    return (less + 0.5 * equal) / n


def _robust_median_mad(values: np.ndarray) -> Tuple[float, float]:
    finite = values[~np.isnan(values)]
    if finite.size == 0:
        return float("nan"), float("nan")
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    return median, mad


def _robust_raw_scores(
    frame: pd.DataFrame,
    feature_columns: Sequence[str],
    stats: Dict[str, Dict[str, float]],
    global_stats: Dict[str, Dict[str, float]],
) -> np.ndarray:
    """Mean absolute robust z-score per row (higher = more unusual).

    Each feature is standardised by the fit-only median and 1.4826*MAD of the
    row's cell line. A zero MAD falls back to the fit-only global MAD for that
    feature; if that is also zero the feature is skipped for that row rather
    than divided by zero. Missing values are skipped, never imputed here.
    """
    columns = [c for c in feature_columns if c in frame.columns]
    if not columns:
        return np.full(len(frame), np.nan)
    component_arrays: List[np.ndarray] = []
    for name in columns:
        values = _numeric_values(frame[name])
        median = stats.get("median", {}).get(name)
        mad = stats.get("mad", {}).get(name)
        if median is None or mad is None or not np.isfinite(median) or not np.isfinite(mad) or mad <= 0:
            median = global_stats.get("median", {}).get(name)
            mad = global_stats.get("mad", {}).get(name)
        if median is None or mad is None or not np.isfinite(median) or not np.isfinite(mad) or mad <= 0:
            continue
        component_arrays.append(np.abs((values - median) / (ROBUST_SCALE_FACTOR * mad)))
    if not component_arrays:
        return np.full(len(frame), np.nan)
    stack = np.vstack(component_arrays)
    counts = np.sum(~np.isnan(stack), axis=0)
    sums = np.nansum(stack, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    return raw


@dataclass
class RankingState:
    """Fit-only statistics, fitted models and references for one ranking run."""

    config: RankingConfig
    feature_columns: List[str]
    model_specs: Dict[str, Dict[str, Any]]
    fitted_cell_lines: List[str]
    cell_line_order: List[str]
    if_reference: Dict[str, Dict[str, List[float]]]
    robust_stats: Dict[str, Dict[str, Dict[str, float]]]
    global_robust_stats: Dict[str, Dict[str, float]]
    robust_features: List[str]
    robust_reference: Dict[str, List[float]]
    global_columns: List[str]
    dropped_all_missing_global: List[str]
    partition: Dict[str, Any]
    annotation_reproduction: Dict[str, Any]
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def model_key_for(self, frame: pd.DataFrame) -> np.ndarray:
        is_fitted = frame["cell_line"].isin(self.fitted_cell_lines).to_numpy()
        keys = np.where(is_fitted, frame["cell_line"].astype(object), "global")
        return keys

    def score_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Attach every method's raw score, priority and rank to ``frame``.

        All methods rank the same rows (the caller's candidate pool). Filenames
        and record keys are used for stable tie-breaking, never as model inputs.
        """
        return _score_frame(self, frame)


def _select_columns(frame: pd.DataFrame) -> Tuple[List[str], List[str]]:
    used, dropped = [], []
    for name in MODEL_FEATURES:
        if name not in frame.columns:
            dropped.append(name)
            continue
        if frame[name].notna().any():
            used.append(name)
        else:
            dropped.append(name)
    return used, dropped


def _fit_isolation_forest(columns: List[str], fit_frame: pd.DataFrame, config: RankingConfig, seed: int):
    from sklearn.ensemble import IsolationForest
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import RobustScaler

    max_samples = min(config.if_max_samples, len(fit_frame))
    pipeline = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", RobustScaler()),
            (
                "iforest",
                IsolationForest(
                    n_estimators=config.if_n_estimators,
                    max_samples=max_samples,
                    contamination=config.if_contamination,
                    n_jobs=config.if_n_jobs,
                    random_state=seed,
                ),
            ),
        ]
    )
    pipeline.fit(fit_frame[columns].to_numpy(dtype="float64"))
    return pipeline, max_samples


def build_analysis_state(
    config: RankingConfig,
    feature_marker: Dict[str, Any],
    annotation_stats: pd.DataFrame,
    frame: Optional[pd.DataFrame] = None,
) -> Tuple[RankingState, pd.DataFrame, pd.DataFrame]:
    """Build the canonical analysis frame, the partition and the fitted models.

    Returns ``(state, analysis, provenance)``. Fitting and every reference
    distribution use fit rows only. ``frame`` lets a test inject a modified
    feature frame (for example with perturbed held-out values) without changing
    the inputs on disk.

    The annotation recomputation is verified against the warehouse view on a
    deterministic, memory-bounded sample of images (default
    ``ANNOTATION_REPRODUCTION_SAMPLE_SIZE``): the SQL aggregation used by the
    synthetic annotation-loss stage and the pandas reference helper are each
    compared with ``lc.v1_image_annotation_stats``. The full 1.66 M-row
    annotation table is never materialised; the focused test checks the pandas
    helper exhaustively on a fixture.
    """
    if frame is None:
        frame = load_feature_frame(config.features_dir)
    canonical, provenance = canonicalize_versions(frame)
    canonical = canonical.merge(
        provenance[
            ["file_name", "n_versions_total", "n_versions_eligible", "selection_status"]
        ].rename(
            columns={
                "n_versions_total": "versions_total",
                "n_versions_eligible": "versions_eligible",
            }
        ),
        on="file_name",
        how="left",
        validate="one_to_one",
    )
    canonical = _add_groups(canonical)

    # --- verify the annotation recomputation reproduces the warehouse view --
    # Sample a bounded, deterministic set of images so the check never touches
    # every annotation row (the full table is ~1.66 M rows). Both recomputations
    # are bounded to exactly this sample.
    reference = annotation_stats.copy()
    sample_size = min(ANNOTATION_REPRODUCTION_SAMPLE_SIZE, len(reference))
    if sample_size:
        rng = np.random.default_rng(DEFAULT_SEED)
        picked = np.sort(rng.permutation(len(reference))[:sample_size])
        sample = reference.iloc[picked]
        sample_keys = list(zip(sample["split"], sample["image_id"]))
    else:  # pragma: no cover - an empty warehouse is rejected earlier
        sample = reference
        sample_keys = []
    # SQL: aggregate only the sampled images in the warehouse (no pandas rows).
    sql_reproduced = load_annotation_aggregates(config.warehouse_dir, keys=sample_keys)
    # pandas reference: the same definitions applied to the sampled raw records.
    sample_annotations = load_annotations(config.warehouse_dir, keys=sample_keys)
    pandas_reproduced = aggregate_annotation_features(sample_annotations).reset_index()

    def _column_diffs(frame: pd.DataFrame) -> Dict[str, float]:
        joined = sample.merge(
            frame, on=["split", "image_id"], suffixes=("_sql", "_py"), how="left"
        )
        diffs: Dict[str, float] = {}
        for name in ANNOTATION_FEATURE_COLUMNS:
            lhs = joined[name + "_sql"].astype("float64")
            rhs = joined[name + "_py"].astype("float64")
            both_missing = lhs.isna() & rhs.isna()
            diff = (lhs - rhs).abs()
            diff[both_missing] = 0.0
            # One side missing where the other has a value is a definition mismatch.
            if (lhs.isna() != rhs.isna()).any():
                worst = float("inf")
            else:
                worst = float(diff.max()) if len(diff) else 0.0
                if np.isnan(worst):
                    worst = float("inf")
            diffs[name] = worst
        return diffs

    pandas_diffs = _column_diffs(pandas_reproduced)
    sql_diffs = _column_diffs(sql_reproduced)
    reproduction: Dict[str, Any] = {
        "columns": pandas_diffs,
        "max_abs_diff": max([0.0, *pandas_diffs.values()]),
        "sql_columns": sql_diffs,
        "max_abs_diff_sql": max([0.0, *sql_diffs.values()]),
        "sampled_images": int(sample_size),
        "total_images": int(len(reference)),
        "note": (
            "pandas reference helper and the SQL aggregation used for synthetic "
            "recomputation are each compared with lc.v1_image_annotation_stats on "
            "a deterministic bounded sample; the focused test checks the pandas "
            "helper exhaustively on a fixture"
        ),
    }
    if (
        reproduction["max_abs_diff"] > ANNOTATION_REPRODUCTION_TOLERANCE
        or reproduction["max_abs_diff_sql"] > ANNOTATION_REPRODUCTION_TOLERANCE
    ):
        raise RankingError(
            "annotation aggregation does not reproduce the warehouse view "
            f"(pandas max abs diff {reproduction['max_abs_diff']}, sql max abs diff "
            f"{reproduction['max_abs_diff_sql']}); refusing to fabricate features"
        )

    # --- partition ---------------------------------------------------------
    partition = partition_groups(canonical)
    fit_groups_by_line = {
        key: set(value["fit_groups"]) for key, value in partition["cell_lines"].items()
    }

    def _bucket(row: pd.Series) -> str:
        return "(unparsed)" if pd.isna(row["cell_line"]) else str(row["cell_line"])

    analysis = canonical.copy()
    analysis["_bucket"] = analysis.apply(_bucket, axis=1)
    analysis["partition"] = "evaluation_only"
    analysis.loc[analysis["split"] == "train", "partition"] = "held_out"
    analysis.loc[analysis["split"] == "val", "partition"] = "validation"
    analysis.loc[analysis["split"] == "test", "partition"] = "test"
    excluded_reasons: Dict[str, str] = {}
    train_mask = analysis["split"] == "train"
    for idx, row in analysis[train_mask].iterrows():
        if row["is_known_case"]:
            analysis.at[idx, "partition"] = "excluded_known_case"
            continue
        if row["acquisition_group"] in fit_groups_by_line.get(row["_bucket"], set()):
            analysis.at[idx, "partition"] = "fit"

    # Unusable filenames never get a canonical row; record every version of them
    # with an explicit exclusion reason so unavailable inputs are never scored.
    excluded_reasons: Dict[str, str] = {}
    excluded_versions: List[Dict[str, Any]] = []
    for prov_row in provenance[provenance["selection_status"] != "chosen"].itertuples(index=False):
        for version in prov_row.versions:
            excluded_reasons[str(version["record_key"])] = str(prov_row.exclusion_reason)
            excluded_versions.append(
                {
                    "record_key": str(version["record_key"]),
                    "file_name": str(prov_row.file_name),
                    "split": str(version["split"]),
                    "partition": "excluded_unusable",
                    "exclusion_reason": str(prov_row.exclusion_reason),
                }
            )
    # Defensive: a canonical row should always be eligible; keep the guard so an
    # ineligible canonical row can never be scored silently.
    unusable = analysis[
        (analysis["annotation_join_status"] != "ok")
        | (analysis["image_features_status"] != "ok")
    ]
    for _idx, row in unusable.iterrows():
        reason = (
            "ambiguous_annotation_join"
            if row["annotation_join_status"] != "ok"
            else f"image_features_{row['image_features_status']}"
        )
        excluded_reasons[str(row["record_key"])] = reason
    analysis = analysis[~analysis["record_key"].isin(excluded_reasons)].reset_index(drop=True)

    fit_rows = analysis[analysis["partition"] == "fit"]
    if fit_rows.empty:
        raise RankingError("no eligible fit rows: the partition produced an empty fit set")

    # --- feature selection -------------------------------------------------
    global_columns, dropped_global = _select_columns(fit_rows)
    if not global_columns:
        raise RankingError("no usable model features in the fit rows")

    cell_line_order = sorted(
        str(x) for x in fit_rows["cell_line"].dropna().unique().tolist()
    )
    model_specs: Dict[str, Dict[str, Any]] = {}
    if_reference: Dict[str, Dict[str, List[float]]] = {}
    robust_stats: Dict[str, Dict[str, Dict[str, float]]] = {}
    robust_reference: Dict[str, List[float]] = {}
    fitted_cell_lines: List[str] = []

    global_stats = {
        "median": {name: _robust_median_mad(_numeric_values(fit_rows[name]))[0] for name in global_columns},
        "mad": {name: _robust_median_mad(_numeric_values(fit_rows[name]))[1] for name in global_columns},
    }
    # robust features = those with a usable global median/MAD scale
    robust_features = [
        name
        for name in global_columns
        if np.isfinite(global_stats["median"][name]) and global_stats["mad"][name] > 0
    ]

    def _fit_one(key: str, rows: pd.DataFrame) -> bool:
        columns = [
            c for c in MODEL_FEATURES if c in rows.columns and rows[c].notna().any()
        ]
        if not columns:
            return False
        model_specs[key] = {
            "columns": columns,
            "dropped_all_missing": [c for c in MODEL_FEATURES if c not in columns],
            "fit_rows": int(len(rows)),
            "scored_as": "cell_line" if key != "global" else "global_fallback",
        }
        for seed in SENSITIVITY_SEEDS:
            pipeline, max_samples = _fit_isolation_forest(columns, rows, config, seed)
            model_specs[key].setdefault("max_samples", {})[str(seed)] = max_samples
            raw = -pipeline.score_samples(_numeric_matrix(rows, columns))
            if_reference.setdefault(key, {})[str(seed)] = sorted(float(v) for v in raw)
            model_specs[key].setdefault("pipelines", {})[str(seed)] = pipeline
        stats = {
            "median": {
                name: _robust_median_mad(_numeric_values(rows[name]))[0] for name in global_columns
            },
            "mad": {
                name: _robust_median_mad(_numeric_values(rows[name]))[1] for name in global_columns
            },
            "fit_rows": int(len(rows)),
        }
        robust_stats[key] = stats
        raw_robust = _robust_raw_scores(rows, robust_features, stats, global_stats)
        robust_reference[key] = sorted(float(v) for v in raw_robust if not np.isnan(v))
        return True

    if not _fit_one("global", fit_rows):  # pragma: no cover - guarded above
        raise RankingError("no usable features for the global fit-only model")
    for line in cell_line_order:
        rows = fit_rows[fit_rows["cell_line"] == line]
        if len(rows) >= MIN_CELL_LINE_FIT_IMAGES and _fit_one(line, rows):
            fitted_cell_lines.append(line)

    state = RankingState(
        config=config,
        feature_columns=list(MODEL_FEATURES),
        model_specs=model_specs,
        fitted_cell_lines=fitted_cell_lines,
        cell_line_order=cell_line_order,
        if_reference=if_reference,
        robust_stats=robust_stats,
        global_robust_stats=global_stats,
        robust_features=robust_features,
        robust_reference=robust_reference,
        global_columns=global_columns,
        dropped_all_missing_global=dropped_global,
        partition=partition,
        annotation_reproduction=reproduction,
    )

    # --- assemble the partition manifest per record ------------------------
    record_partition = []
    for row in analysis.itertuples(index=False):
        record_partition.append(
            {
                "record_key": str(row.record_key),
                "file_name": str(row.file_name),
                "split": str(row.split),
                "cell_line": None if pd.isna(row.cell_line) else str(row.cell_line),
                "acquisition_group": str(row.acquisition_group),
                "is_known_case": bool(row.is_known_case),
                "partition": str(row.partition),
            }
        )
    for entry in excluded_versions:
        record_partition.append(entry)
    partition["records"] = sorted(
        record_partition, key=lambda entry: (entry["partition"], entry["record_key"])
    )
    partition["counts"] = {
        "fit": int((analysis["partition"] == "fit").sum()),
        "held_out": int((analysis["partition"] == "held_out").sum()),
        "validation": int((analysis["partition"] == "validation").sum()),
        "test": int((analysis["partition"] == "test").sum()),
        "excluded_known_case": int((analysis["partition"] == "excluded_known_case").sum()),
        "excluded_unusable": len(excluded_reasons),
        "excluded_unusable_filenames": int(
            (provenance["selection_status"] == "no_eligible_version").sum()
        ),
        "canonical_filenames": int(len(analysis)),
        "distinct_filenames_total": int(len(provenance)),
        "canonical_filenames_no_eligible_version": int(
            (provenance["selection_status"] == "no_eligible_version").sum()
        ),
    }
    partition["fit_only_rule"] = (
        "only eligible official training rows in fit acquisition groups enter "
        "fitting/preprocessing; validation, test, held-out, known-case and "
        "unusable records never enter fitting"
    )
    return state, analysis, provenance


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def _rank_column(priorities: np.ndarray, identities: pd.DataFrame) -> np.ndarray:
    """1-based rank, highest priority first, stable filename tie-break."""
    order = np.lexsort(
        (
            identities["record_key"].to_numpy(),
            identities["file_name"].to_numpy(),
            -priorities,
        )
    )
    ranks = np.empty(len(priorities), dtype="int64")
    ranks[order] = np.arange(1, len(priorities) + 1, dtype="int64")
    return ranks


def _score_frame(state: RankingState, frame: pd.DataFrame) -> pd.DataFrame:
    scored = frame.copy().reset_index(drop=True)
    keys = np.asarray(state.model_key_for(scored), dtype=object)

    # low count: fewer annotations is more urgent; counts need no rescaling
    if "n_annotations" in scored.columns:
        low = -_numeric_values(scored["n_annotations"])
    else:  # pragma: no cover - n_annotations is a required feature
        low = np.full(len(scored), np.nan)
    scored["low_count_raw"] = low

    # robust statistical baseline (per-cell-line fit-only median/MAD)
    robust_raw = np.full(len(scored), np.nan)
    for key in sorted(set(keys)):
        mask = keys == key
        stats = state.robust_stats.get(key)
        if stats is None:
            stats = state.robust_stats["global"]
        robust_raw[mask] = _robust_raw_scores(
            scored.loc[mask], state.robust_features, stats, state.global_robust_stats
        )
    scored["robust_raw"] = robust_raw
    robust_priority = np.full(len(scored), np.nan)
    for key in sorted(set(keys)):
        mask = keys == key
        reference = state.robust_reference.get(key) or state.robust_reference.get("global", [])
        robust_priority[mask] = [
            empirical_percentile(float(v), reference) if np.isfinite(v) else np.nan
            for v in robust_raw[mask]
        ]
    scored["robust_priority"] = robust_priority

    # Isolation Forest per declared seed
    for seed in SENSITIVITY_SEEDS:
        raw = np.full(len(scored), np.nan)
        priority = np.full(len(scored), np.nan)
        for key in sorted(set(keys)):
            mask = keys == key
            spec = state.model_specs.get(key) or state.model_specs["global"]
            pipeline = spec["pipelines"][str(seed)]
            values = -pipeline.score_samples(_numeric_matrix(scored.loc[mask], spec["columns"]))
            raw[mask] = values
            reference = state.if_reference[key][str(seed)]
            priority[mask] = [empirical_percentile(float(v), reference) for v in values]
        scored[f"if_raw_seed{seed}"] = raw
        scored[f"if_priority_seed{seed}"] = priority
        scored[f"if_model_seed{seed}"] = keys

    # seeded random reference
    for seed in SENSITIVITY_SEEDS:
        rng = np.random.default_rng(seed)
        order = np.lexsort(
            (scored["record_key"].to_numpy(), scored["file_name"].to_numpy())
        )
        values = np.empty(len(scored), dtype="float64")
        values[order] = rng.random(len(scored))
        scored[f"random_priority_seed{seed}"] = values

    # convenience aliases for the default seed
    scored["if_raw"] = scored[f"if_raw_seed{DEFAULT_SEED}"]
    scored["if_priority"] = scored[f"if_priority_seed{DEFAULT_SEED}"]
    scored["if_model"] = scored[f"if_model_seed{DEFAULT_SEED}"]
    scored["random_priority"] = scored[f"random_priority_seed{DEFAULT_SEED}"]

    # ranks: identical pool, highest priority first, filename tie-break
    scored["low_count_rank"] = _rank_column(np.nan_to_num(low, nan=-np.inf), scored)
    scored["robust_rank"] = _rank_column(np.nan_to_num(robust_priority, nan=-np.inf), scored)
    for seed in SENSITIVITY_SEEDS:
        scored[f"if_rank_seed{seed}"] = _rank_column(
            np.nan_to_num(scored[f"if_priority_seed{seed}"].to_numpy(), nan=-np.inf), scored
        )
        scored[f"random_rank_seed{seed}"] = _rank_column(
            scored[f"random_priority_seed{seed}"].to_numpy(), scored
        )
    scored["if_rank"] = scored[f"if_rank_seed{DEFAULT_SEED}"]
    scored["random_rank"] = scored[f"random_rank_seed{DEFAULT_SEED}"]
    return scored


def build_candidates(state: RankingState, analysis: pd.DataFrame) -> pd.DataFrame:
    """Frozen review pool: every canonical row that is not a fit row.

    The pool is held-out training groups, the official validation split, the
    official test split and any known-case rows whose acquisition group would
    otherwise be a fit group (they are excluded from fitting, so they must be
    review candidates). Each physical image appears once (canonical version).
    """
    fit_keys = set(analysis.loc[analysis["partition"] == "fit", "record_key"])
    pool = analysis[~analysis["record_key"].isin(fit_keys)].copy()
    if pool.empty:
        raise RankingError("review pool is empty")
    scored = _score_frame(state, pool)
    ordered_columns = [
        *CANDIDATE_IDENTITY_COLUMNS,
        *[c for c in MODEL_FEATURES if c in scored.columns],
        *[c for c in scored.columns if c not in CANDIDATE_IDENTITY_COLUMNS and c not in MODEL_FEATURES],
    ]
    return scored[ordered_columns]


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #


def _model_card(
    state: RankingState,
    feature_marker: Dict[str, Any],
    dictionary: Dict[str, Any],
) -> Dict[str, Any]:
    partition = state.partition
    return {
        "ranking_schema_version": RANKING_SCHEMA_VERSION,
        "feature_schema_version": dictionary.get("feature_schema_version"),
        "stage": "ranking",
        "question": (
            "At the same review budget, does an Isolation Forest ranking offer "
            "useful prioritisation compared with simple baselines? A negative "
            "result is valid."
        ),
        "score_semantics": (
            "Scores indicate unusualness, never the probability that a label is "
            "wrong. Priority is a fit-only empirical percentile in [0, 1] and is "
            "a relative review priority, not an error probability."
        ),
        "protocol": {
            "default_seed": DEFAULT_SEED,
            "sensitivity_seeds": list(SENSITIVITY_SEEDS),
            "budgets": list(state.config.budgets),
            "isolation_forest": {
                "n_estimators": state.config.if_n_estimators,
                "max_samples": f"min({state.config.if_max_samples}, n_fit)",
                "contamination": state.config.if_contamination,
                "n_jobs": state.config.if_n_jobs,
                "score_transform": "priority = -score_samples (higher = more unusual)",
            },
            "preprocessing": (
                "sklearn Pipeline(SimpleImputer(median), RobustScaler, IsolationForest), "
                "fitted on fit rows only"
            ),
            "min_cell_line_fit_images": MIN_CELL_LINE_FIT_IMAGES,
            "partition_holdout_fraction": PARTITION_HOLDOUT_FRACTION,
            "partition_seed": PARTITION_SEED,
        },
        "feature_selection": {
            "model_features": list(MODEL_FEATURES),
            "global_columns": state.global_columns,
            "dropped_all_missing_global": state.dropped_all_missing_global,
            "robust_features": state.robust_features,
            "excluded_features": EXCLUDED_FEATURES,
            "all_missing_handling": (
                "a feature absent for every fit row of a model is dropped for "
                "that model; remaining nulls are imputed with the model's "
                "fit-only median"
            ),
            "zero_mad_handling": (
                "robust baseline: a zero MAD falls back to the fit-only global "
                "MAD; if that is also zero the feature is skipped for that row"
            ),
        },
        "models": {
            key: {
                "kind": spec["scored_as"],
                "columns": spec["columns"],
                "dropped_all_missing": spec["dropped_all_missing"],
                "fit_rows": spec["fit_rows"],
                "max_samples": spec.get("max_samples"),
            }
            for key, spec in sorted(state.model_specs.items())
        },
        "fitted_cell_lines": state.fitted_cell_lines,
        "global_fallback_rule": (
            "a cell line with fewer than "
            f"{MIN_CELL_LINE_FIT_IMAGES} fit images uses the documented global "
            "model fitted on all fit rows; no model is ever fitted on "
            "validation, test, held-out or known-case rows"
        ),
        "partition": {
            "policy": partition["policy"],
            "caveat": partition["caveat"],
            "counts": partition["counts"],
            "cell_lines": partition["cell_lines"],
        },
        "annotation_reproduction": state.annotation_reproduction,
        "inputs": {
            "feature_schema_version": feature_marker.get("feature_schema_version"),
            "feature_records": feature_marker.get("feature_records"),
            "source_sha256": feature_marker.get("source_sha256"),
            "feature_manifest_sha256": (feature_marker.get("manifest") or {}).get("sha256"),
            "warehouse_manifest_sha256": feature_marker.get("warehouse_manifest_sha256"),
            "warehouse_schema_version": dictionary.get("source_manifest", {}).get(
                "warehouse_schema_version"
            ),
            "warehouse_view_version": dictionary.get("source_manifest", {}).get(
                "warehouse_view_version"
            ),
        },
        "limitations": [
            "Scores are unusualness indicators; a sparse image may be annotated correctly.",
            "The nine known cases are a regression exercise, not recall over all bad labels.",
            "Acquisition groups are a leakage proxy, not proof of biological independence.",
            "Official test imagery is ranked only for exploration, never for tuning.",
            "No calibration against real annotation error is claimed or performed.",
        ],
    }


def _deltas_summary(values: pd.Series) -> Dict[str, Any]:
    if values.empty:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": int(values.shape[0]),
        "mean": float(values.mean()),
        "median": float(values.median()),
        "min": float(values.min()),
        "max": float(values.max()),
    }


def _public_summary(
    state: RankingState,
    feature_marker: Dict[str, Any],
    candidates: pd.DataFrame,
    provenance: pd.DataFrame,
) -> Dict[str, Any]:
    """Compact machine-readable summary with no private absolute paths."""
    known_in_pool = int(candidates["is_known_case"].sum())
    return {
        "schema_version": RANKING_SCHEMA_VERSION,
        "stage": "ranking",
        "generated_stage": "ranking",
        "protocol": {
            "default_seed": DEFAULT_SEED,
            "sensitivity_seeds": list(SENSITIVITY_SEEDS),
            "budgets": list(state.config.budgets),
            "isolation_forest": {
                "n_estimators": state.config.if_n_estimators,
                "max_samples_cap": state.config.if_max_samples,
                "contamination": state.config.if_contamination,
                "n_jobs": state.config.if_n_jobs,
            },
        },
        "methods": list(METHODS),
        "counts": {
            "feature_records": int(feature_marker.get("feature_records") or 0),
            "canonical_filenames": int(len(provenance)),
            "review_pool_candidates": int(len(candidates)),
            "known_case_candidates": known_in_pool,
            "fit_rows": state.partition["counts"]["fit"],
            "held_out_rows": state.partition["counts"]["held_out"],
            "validation_rows": state.partition["counts"]["validation"],
            "test_rows": state.partition["counts"]["test"],
            "cell_line_models": len(state.fitted_cell_lines),
            "global_fallback_models": 1,
        },
        "score_semantics": (
            "unusualness priority in [0, 1] (fit-only percentile); not an error "
            "probability; raw scores preserved in candidates.jsonl"
        ),
        "provenance": {
            "source_sha256": feature_marker.get("source_sha256"),
            "feature_schema_version": feature_marker.get("feature_schema_version"),
            "feature_manifest_sha256": (feature_marker.get("manifest") or {}).get("sha256"),
            "warehouse_manifest_sha256": feature_marker.get("warehouse_manifest_sha256"),
            "annotation_reproduction_max_abs_diff": state.annotation_reproduction[
                "max_abs_diff"
            ],
            "annotation_reproduction_max_abs_diff_sql": state.annotation_reproduction[
                "max_abs_diff_sql"
            ],
        },
        "artifacts": {
            "candidates": "candidates.jsonl",
            "candidates_parquet": "candidates.parquet",
            "version_provenance": "version_provenance.jsonl",
            "partition_manifest": "partition_manifest.json",
            "model_card": "model_card.json",
            "fit_reference": "fit_reference.json",
        },
    }


def _peak_rss_mb() -> Optional[float]:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:  # pragma: no cover - non-POSIX
        return None


def _write_candidate_outputs(candidates: pd.DataFrame, staging_dir: str) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    jsonl_path = os.path.join(staging_dir, "candidates.jsonl")
    candidates.to_json(jsonl_path, orient="records", lines=True, force_ascii=False)
    sizes["candidates_jsonl"] = os.path.getsize(jsonl_path)
    csv_path = os.path.join(staging_dir, "candidates.csv")
    candidates.to_csv(csv_path, index=False)
    sizes["candidates_csv"] = os.path.getsize(csv_path)
    parquet_path = os.path.join(staging_dir, "candidates.parquet")
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM read_json({_sql_str(jsonl_path)}, "
            "format='newline_delimited', auto_detect=true)) "
            f"TO {_sql_str(parquet_path)} (FORMAT PARQUET, COMPRESSION zstd)"
        )
    finally:
        con.close()
    sizes["candidates_parquet"] = os.path.getsize(parquet_path)
    return sizes


def _save_models(state: RankingState, staging_dir: str) -> List[str]:
    import joblib

    models_dir = os.path.join(staging_dir, "models")
    os.makedirs(models_dir, exist_ok=True)
    relatives: List[str] = []
    for key in sorted(state.model_specs):
        for seed in SENSITIVITY_SEEDS:
            relative = f"models/{_model_file_name(key, seed)}"
            joblib.dump(
                state.model_specs[key]["pipelines"][str(seed)],
                os.path.join(staging_dir, relative),
                compress=0,
            )
            relatives.append(relative)
    return relatives


def _model_file_name(key: str, seed: int) -> str:
    safe = key.replace("|", "_").replace("/", "_")
    return f"{safe}__seed{seed}.joblib"


def run_ranking(config: RankingConfig) -> Dict[str, Any]:
    """Build the frozen ranking, baselines and provenance; return the run record."""
    started = time.perf_counter()
    features_dir = os.path.abspath(config.features_dir)
    warehouse_dir = os.path.abspath(config.warehouse_dir)
    out_dir = os.path.abspath(config.out_dir)
    marker_path = os.path.join(out_dir, COMPLETION_MARKER)
    staging_dir = os.path.join(out_dir, STAGING_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    try:
        clear_stale(out_dir)
        feature_marker, dictionary, _warehouse_marker = require_verified_inputs(
            features_dir, warehouse_dir
        )
        os.makedirs(staging_dir)

        build_started = time.perf_counter()
        annotation_stats = load_annotation_stats(warehouse_dir)
        state, analysis, provenance = build_analysis_state(
            config, feature_marker, annotation_stats
        )
        candidates = build_candidates(state, analysis)
        build_seconds = time.perf_counter() - build_started

        sizes = _write_candidate_outputs(candidates, staging_dir)
        provenance_path = os.path.join(staging_dir, "version_provenance.jsonl")
        provenance.to_json(provenance_path, orient="records", lines=True, force_ascii=False)
        sizes["version_provenance_jsonl"] = os.path.getsize(provenance_path)

        model_card = _model_card(state, feature_marker, dictionary)
        dump_json(os.path.join(staging_dir, "model_card.json"), model_card)
        dump_json(
            os.path.join(staging_dir, "partition_manifest.json"), state.partition
        )
        fit_reference = {
            "ranking_schema_version": RANKING_SCHEMA_VERSION,
            "model_features_requested": list(MODEL_FEATURES),
            "global_columns": state.global_columns,
            "robust_features": state.robust_features,
            "models": {
                key: {
                    "columns": spec["columns"],
                    "fit_rows": spec["fit_rows"],
                    "max_samples": spec.get("max_samples"),
                }
                for key, spec in sorted(state.model_specs.items())
            },
            "if_reference_percentiles": {
                key: {seed: len(values) for seed, values in seeds.items()}
                for key, seeds in sorted(state.if_reference.items())
            },
            "if_reference_anomaly_raw": {
                key: {seed: values for seed, values in sorted(seeds.items())}
                for key, seeds in sorted(state.if_reference.items())
            },
            "robust_stats": state.robust_stats,
            "robust_reference": state.robust_reference,
            "note": (
                "all reference distributions are fit-only; the isolation-forest "
                "reference stores -score_samples (higher = more unusual) for the "
                "fit rows scored by that model"
            ),
        }
        dump_json(os.path.join(staging_dir, "fit_reference.json"), fit_reference)

        summary = _public_summary(state, feature_marker, candidates, provenance)
        dump_json(os.path.join(staging_dir, "summary.json"), summary)

        model_relatives = _save_models(state, staging_dir)

        run_record = {
            "tool": {"name": "livecell-audit", "version": __version__},
            "stage": "ranking",
            "ranking_schema_version": RANKING_SCHEMA_VERSION,
            "features_dir": features_dir,
            "warehouse_dir": warehouse_dir,
            "out_dir": out_dir,
            "seed": config.seed,
            "budgets": list(config.budgets),
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "build_seconds": round(build_seconds, 3),
            "peak_memory_mb": _peak_rss_mb(),
            "runtime_method": (
                "time.perf_counter() around input verification, annotation "
                "reproduction, fitting, scoring and writes"
            ),
            "versions": {
                "python": __import__("sys").version.split()[0],
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "duckdb": duckdb.__version__,
                "scikit_learn": _sklearn_version(),
            },
            "feature_build": feature_build_identity(feature_marker),
            "diagnostics": state.diagnostics,
            "resource_limits": {
                "duckdb_threads": WAREHOUSE_THREADS,
                "duckdb_memory_limit_mb": WAREHOUSE_MEMORY_LIMIT_MB,
                "note": (
                    "warehouse reads are capped so a run stays inside the capped "
                    "job budget instead of sizing itself against system memory"
                ),
            },
            "output_sizes_bytes": sizes,
            "n_models_saved": len(model_relatives),
        }
        dump_json(os.path.join(staging_dir, "run.json"), run_record)

        manifest_relatives = list(RANKING_OUTPUT_PATHS) + model_relatives
        manifest = build_manifest(
            "ranking",
            RANKING_SCHEMA_VERSION,
            staging_dir,
            manifest_relatives,
            extra={"partial": False, "seed": config.seed},
        )
        dump_json(os.path.join(staging_dir, MANIFEST_NAME), manifest)
        manifest_digest, manifest_bytes = sha256_file(os.path.join(staging_dir, MANIFEST_NAME))

        promote_staged(staging_dir, out_dir)
        write_completion_marker(
            marker_path,
            {
                "status": "completed",
                "stage": "ranking",
                "partial": False,
                "tool": {"name": "livecell-audit", "version": __version__},
                "ranking_schema_version": RANKING_SCHEMA_VERSION,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "features_dir": features_dir,
                "warehouse_dir": warehouse_dir,
                "seed": config.seed,
                "budgets": list(config.budgets),
                "candidate_rows": int(len(candidates)),
                "fit_rows": state.partition["counts"]["fit"],
                "known_case_candidates": int(candidates["is_known_case"].sum()),
                "source_sha256": feature_marker.get("source_sha256"),
                "feature_schema_version": feature_marker.get("feature_schema_version"),
                "feature_build": feature_build_identity(feature_marker),
                "warehouse_manifest_sha256": feature_marker.get("warehouse_manifest_sha256"),
                "manifest": {
                    "file": MANIFEST_NAME,
                    "sha256": manifest_digest,
                    "bytes": manifest_bytes,
                },
                "outputs": {
                    "candidates": "candidates.jsonl",
                    "partition": "partition_manifest.json",
                    "model_card": "model_card.json",
                    "fit_reference": "fit_reference.json",
                    "summary": "summary.json",
                    "models": "models/",
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
        "candidates": candidates,
        "state": state,
        "marker": read_json(marker_path),
        "out_dir": out_dir,
    }


def _sklearn_version() -> Optional[str]:
    try:
        import sklearn

        return sklearn.__version__
    except ImportError:  # pragma: no cover
        return None


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def verify_ranking_outputs(out_dir: str) -> Dict[str, Any]:
    """Verify a completed ranking run's marker, manifest and record counts."""
    out_dir = os.path.abspath(out_dir)
    marker = load_completion_marker(out_dir)
    if marker is None:
        return {
            "ok": False,
            "problems": [f"not a completed ranking run: {out_dir}"],
            "marker": None,
            "manifest": None,
        }
    problems: List[str] = []
    if marker.get("stage") != "ranking":
        problems.append(f"unexpected stage {marker.get('stage')!r}")
    if str(marker.get("ranking_schema_version")) != RANKING_SCHEMA_VERSION:
        problems.append(
            f"ranking_schema_version {marker.get('ranking_schema_version')!r} "
            f"!= {RANKING_SCHEMA_VERSION!r}"
        )
    if marker.get("partial"):
        problems.append("ranking run is marked partial; not release-ready")
    feature_build = marker.get("feature_build") or {}
    if not feature_build.get("feature_manifest_sha256"):
        problems.append("ranking marker has no feature build identity")
    manifest_info = marker.get("manifest") or {}
    if not manifest_info.get("sha256"):
        problems.append("marker has no manifest checksum")
        return {"ok": False, "problems": problems, "marker": marker, "manifest": None}
    result = verify_manifest(
        out_dir,
        stage="ranking",
        expected_sha256=manifest_info.get("sha256"),
        required_paths=RANKING_REQUIRED_OUTPUTS,
    )
    problems.extend(result["problems"])
    manifest = result["manifest"]
    if manifest is not None and bool(manifest.get("partial")):
        problems.append("manifest marks the ranking run partial")
    candidates_path = os.path.join(out_dir, "candidates.jsonl")
    if os.path.isfile(candidates_path):
        lines = count_lines(candidates_path)
        expected = marker.get("candidate_rows")
        if expected is not None and lines != expected:
            problems.append(
                f"candidates.jsonl rows={lines} != marker candidate_rows={expected}"
            )
    else:
        problems.append("missing candidates.jsonl")
    return {
        "ok": not problems,
        "problems": problems,
        "marker": marker,
        "manifest": manifest,
    }


def assert_ranking_ready(out_dir: str) -> Dict[str, Any]:
    """Return the marker of a verified ranking run or raise :class:`RankingError`."""
    result = verify_ranking_outputs(out_dir)
    if not result["ok"]:
        raise RankingError(
            "ranking outputs are not release-ready: " + "; ".join(result["problems"])
        )
    return result["marker"]


@dataclass
class FrozenRanking:
    """A completed ranking run, loaded for evaluation without refitting."""

    marker: Dict[str, Any]
    candidates: pd.DataFrame
    provenance: pd.DataFrame
    partition: Dict[str, Any]
    model_card: Dict[str, Any]
    fit_reference: Dict[str, Any]
    summary: Dict[str, Any]
    if_reference: Dict[str, Dict[str, List[float]]]
    robust_stats: Dict[str, Dict[str, Dict[str, float]]]
    robust_reference: Dict[str, List[float]]
    robust_features: List[str]
    fitted_cell_lines: List[str]
    if_pipelines: Dict[Tuple[str, int], Any]

    def score_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        return _score_frame_frozen(self, frame)

    def reported_version(self, file_name: str, split: str, image_id: int) -> Optional[Dict[str, Any]]:
        """Return the provenance version for an exact (filename, split, id)."""
        match = self.provenance[self.provenance["file_name"] == file_name]
        if match.empty:
            return None
        for version in match.iloc[0]["versions"]:
            if version["split"] == split and int(version["image_id"]) == int(image_id):
                return version
        return None


def load_frozen_ranking(out_dir: str) -> FrozenRanking:
    """Load and verify a completed ranking run for evaluation."""
    import joblib

    marker = assert_ranking_ready(out_dir)
    out_dir = os.path.abspath(out_dir)
    candidates_path = os.path.join(out_dir, "candidates.jsonl")
    candidates = pd.read_json(candidates_path, orient="records", lines=True)
    provenance = pd.read_json(
        os.path.join(out_dir, "version_provenance.jsonl"), orient="records", lines=True
    )
    partition = read_json(os.path.join(out_dir, "partition_manifest.json"))
    model_card = read_json(os.path.join(out_dir, "model_card.json"))
    fit_reference = read_json(os.path.join(out_dir, "fit_reference.json"))
    summary = read_json(os.path.join(out_dir, "summary.json"))

    pipelines: Dict[Tuple[str, int], Any] = {}
    for key in fit_reference["models"]:
        for seed in SENSITIVITY_SEEDS:
            path = os.path.join(out_dir, "models", _model_file_name(key, seed))
            if not os.path.isfile(path):
                raise RankingError(f"saved model is missing: {path}")
            pipelines[(key, seed)] = joblib.load(path)
    fitted = [key for key in fit_reference["models"] if key != "global"]
    return FrozenRanking(
        marker=marker,
        candidates=candidates,
        provenance=provenance,
        partition=partition,
        model_card=model_card,
        fit_reference=fit_reference,
        summary=summary,
        if_reference={
            key: {seed: list(values) for seed, values in seeds.items()}
            for key, seeds in fit_reference["if_reference_anomaly_raw"].items()
        },
        robust_stats=fit_reference["robust_stats"],
        robust_reference={
            key: list(values) for key, values in fit_reference["robust_reference"].items()
        },
        robust_features=list(fit_reference["robust_features"]),
        fitted_cell_lines=sorted(fitted),
        if_pipelines=pipelines,
    )


def _score_frame_frozen(frozen: FrozenRanking, frame: pd.DataFrame) -> pd.DataFrame:
    """Score an arbitrary frame with the frozen models and fit-only references."""
    scored = frame.copy().reset_index(drop=True)
    global_stats = frozen.robust_stats["global"]
    is_fitted = scored["cell_line"].isin(frozen.fitted_cell_lines).to_numpy()
    keys = np.asarray(np.where(is_fitted, scored["cell_line"].astype(object), "global"), dtype=object)

    low = -_numeric_values(scored["n_annotations"])
    scored["low_count_raw"] = low

    robust_raw = np.full(len(scored), np.nan)
    robust_priority = np.full(len(scored), np.nan)
    for key in sorted(set(keys)):
        mask = keys == key
        stats = frozen.robust_stats.get(key, global_stats)
        robust_raw[mask] = _robust_raw_scores(
            scored.loc[mask], frozen.robust_features, stats, global_stats
        )
        reference = frozen.robust_reference.get(key) or frozen.robust_reference.get("global", [])
        robust_priority[mask] = [
            empirical_percentile(float(v), reference) if np.isfinite(v) else np.nan
            for v in robust_raw[mask]
        ]
    scored["robust_raw"] = robust_raw
    scored["robust_priority"] = robust_priority

    for seed in SENSITIVITY_SEEDS:
        raw = np.full(len(scored), np.nan)
        priority = np.full(len(scored), np.nan)
        for key in sorted(set(keys)):
            mask = keys == key
            pipeline = frozen.if_pipelines.get((key, seed)) or frozen.if_pipelines[("global", seed)]
            spec = frozen.fit_reference["models"].get(key) or frozen.fit_reference["models"]["global"]
            values = -pipeline.score_samples(_numeric_matrix(scored.loc[mask], spec["columns"]))
            raw[mask] = values
            reference = frozen.if_reference[key][str(seed)]
            priority[mask] = [empirical_percentile(float(v), reference) for v in values]
        scored[f"if_raw_seed{seed}"] = raw
        scored[f"if_priority_seed{seed}"] = priority
        scored[f"if_model_seed{seed}"] = keys

    for seed in SENSITIVITY_SEEDS:
        rng = np.random.default_rng(seed)
        order = np.lexsort((scored["record_key"].to_numpy(), scored["file_name"].to_numpy()))
        values = np.empty(len(scored), dtype="float64")
        values[order] = rng.random(len(scored))
        scored[f"random_priority_seed{seed}"] = values

    scored["low_count_rank"] = _rank_column(low, scored)
    scored["robust_rank"] = _rank_column(np.nan_to_num(robust_priority, nan=-np.inf), scored)
    for seed in SENSITIVITY_SEEDS:
        scored[f"if_rank_seed{seed}"] = _rank_column(
            np.nan_to_num(scored[f"if_priority_seed{seed}"].to_numpy(), nan=-np.inf), scored
        )
        scored[f"random_rank_seed{seed}"] = _rank_column(
            scored[f"random_priority_seed{seed}"].to_numpy(), scored
        )
    scored["if_raw"] = scored[f"if_raw_seed{DEFAULT_SEED}"]
    scored["if_priority"] = scored[f"if_priority_seed{DEFAULT_SEED}"]
    scored["if_rank"] = scored[f"if_rank_seed{DEFAULT_SEED}"]
    scored["random_priority"] = scored[f"random_priority_seed{DEFAULT_SEED}"]
    scored["random_rank"] = scored[f"random_rank_seed{DEFAULT_SEED}"]
    return scored


def method_rank_column(method: str, seed: int) -> str:
    """Rank column name for a method at a seed.

    ``low_count`` and ``robust`` do not depend on the configured model seed, so
    they have a single rank column; ``isolation_forest`` and ``random`` are
    seed-specific.
    """
    if method == "low_count":
        return "low_count_rank"
    if method == "robust":
        return "robust_rank"
    if method == "isolation_forest":
        return f"if_rank_seed{seed}"
    if method == "random":
        return f"random_rank_seed{seed}"
    raise RankingError(f"unknown method {method!r}")


def method_priority_column(method: str, seed: int) -> str:
    """Priority column name for a method at a seed."""
    if method == "low_count":
        return "low_count_raw"
    if method == "robust":
        return "robust_priority"
    if method == "isolation_forest":
        return f"if_priority_seed{seed}"
    if method == "random":
        return f"random_priority_seed{seed}"
    raise RankingError(f"unknown method {method!r}")


def method_raw_column(method: str, seed: int) -> str:
    """Raw (un-normalised) score column name for a method at a seed.

    For the Isolation Forest this is ``-score_samples`` (higher = more unusual);
    it is preserved so a percentile-tail tie can never hide a raw score change.
    """
    if method == "low_count":
        return "low_count_raw"
    if method == "robust":
        return "robust_raw"
    if method == "isolation_forest":
        return f"if_raw_seed{seed}"
    if method == "random":
        return f"random_priority_seed{seed}"
    raise RankingError(f"unknown method {method!r}")
