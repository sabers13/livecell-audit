"""Milestone 4 evaluation: known-case regression, synthetic loss, review sample.

Three different kinds of evidence are kept strictly separate:

A. **Known-case regression** — the nine filenames reported upstream as sparse
   labels are ranked against the *same* non-fit review pool as every other
   candidate. This is a regression exercise on nine known examples, not recall
   over all mislabelled images and not evidence of generalisation.

B. **Held-out synthetic annotation-loss experiment** — a fixed deterministic
   subset of untouched held-out training images has most of its annotations
   removed and every label-dependent feature is *recomputed* with the same
   definitions. Metrics are labelled simulation metrics; unchanged images are
   not expert-confirmed clean.

C. **Frozen human review sample** — disjoint top-ML, top-baseline and random
   strata with blank judgment/reason/expertise fields. No judgement is
   fabricated and real-world precision is left unmeasured.

No model, reference distribution or calibration here ever uses held-out,
validation, test or known-case data for fitting.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

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
from .errors import EvaluationError, RankingError
from .features import ANNOTATION_FEATURE_COLUMNS, WAREHOUSE_MEMORY_LIMIT_MB, WAREHOUSE_THREADS
from .known_cases import KNOWN_SPARSE_CASES, UPSTREAM_ISSUE, UPSTREAM_REPORTER
from .ranking import (
    BUDGETS,
    METHODS,
    SENSITIVITY_SEEDS,
    FrozenRanking,
    assert_build_consistency,
    load_annotation_aggregates,
    load_annotation_identities,
    load_frozen_ranking,
    method_priority_column,
    method_rank_column,
    method_raw_column,
    require_verified_inputs,
    verify_ranking_outputs,
)

EVALUATION_SCHEMA_VERSION = "1"

#: Deterministic synthetic-loss configuration (declared before seeing results).
ANNOTATION_LOSS_FRACTION = 0.10
ANNOTATION_LOSS_SELECTION_FRACTION = 0.5
ANNOTATION_LOSS_MASK_SEED = 42
ANNOTATION_LOSS_MIN_ANNOTATIONS = 2

#: Review sample configuration.
REVIEW_STRATUM_SIZE = 25
REVIEW_SAMPLE_SEED = 42
REVIEW_STRATA = (
    "top_isolation_forest",
    "top_robust_baseline",
    "top_low_count_baseline",
    "random_sample",
)

EVALUATION_OUTPUT_PATHS: Tuple[str, ...] = (
    "known_cases.json",
    "known_cases.csv",
    "synthetic_annotation_loss.json",
    "synthetic_paired_deltas.csv",
    "review_sample.json",
    "review_sample.csv",
    "evaluation_summary.json",
    "run.json",
)

EVALUATION_REQUIRED_OUTPUTS: Tuple[str, ...] = (
    "known_cases.json",
    "synthetic_annotation_loss.json",
    "review_sample.json",
    "evaluation_summary.json",
    "run.json",
)


@dataclass
class EvaluationConfig:
    """Inputs for one evaluation run."""

    features_dir: str
    ranking_dir: str
    warehouse_dir: str
    out_dir: str
    budgets: Tuple[int, ...] = BUDGETS


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def precision_at_k(labels: np.ndarray, ranks: np.ndarray, k: int) -> Dict[str, Any]:
    """Fraction of the top ``k`` ranked rows that carry the positive label.

    The denominator is ``min(k, len(pool))`` so every method uses the *same*
    pool and therefore the same denominator, and short pools are reported
    honestly instead of being padded.
    """
    pool = len(labels)
    denominator = min(int(k), pool)
    if denominator == 0:
        return {"k": int(k), "denominator": 0, "hits": 0, "precision": None}
    order = np.argsort(ranks, kind="stable")[:denominator]
    hits = int(labels[order].sum())
    return {
        "k": int(k),
        "denominator": denominator,
        "hits": hits,
        "precision": hits / denominator,
    }


def roc_auc(labels: np.ndarray, scores: np.ndarray) -> Dict[str, Any]:
    """Synthetic ROC AUC with a stable mid-rank tie treatment.

    Ties receive an average rank so a constant score yields AUC 0.5 rather than
    an arbitrary value. Rows whose score is not finite are excluded and the
    retained denominator is reported.
    """
    finite = np.isfinite(scores)
    y = labels[finite].astype("float64")
    x = scores[finite]
    if len(y) == 0 or y.min() == y.max():
        return {"auc": None, "n": int(len(y)), "positives": int(y.sum())}
    order = np.argsort(x, kind="stable")
    ranks = np.empty(len(x), dtype="float64")
    sorted_scores = x[order]
    sorted_ranks = np.arange(1, len(x) + 1, dtype="float64")
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        average = sorted_ranks[i : j + 1].mean()
        ranks[order[i : j + 1]] = average
        i = j + 1
    positives = int(y.sum())
    negatives = int(len(y) - positives)
    auc = (ranks[y == 1].sum() - positives * (positives + 1) / 2.0) / (positives * negatives)
    return {"auc": float(auc), "n": int(len(y)), "positives": positives}


# --------------------------------------------------------------------------- #
# A. known-case regression
# --------------------------------------------------------------------------- #


def known_case_regression(frozen: FrozenRanking, budgets: Sequence[int]) -> Dict[str, Any]:
    """Rank the nine known cases against the frozen non-fit review pool.

    A case counts as *recovered* only when its reported ``(split, image_id,
    filename)`` actually exists in the preserved version provenance **and** the
    canonical version chosen for that filename is that same reported version and
    ranks within the budget. A filename-level count (any version of the filename
    ranking within budget) is reported separately and clearly labelled, so a
    same-name but different-ID version is never called recovered.
    """
    candidates = frozen.candidates
    fit_filenames = {
        entry["file_name"]
        for entry in frozen.partition["records"]
        if entry.get("partition") == "fit"
    }

    rows: List[Dict[str, Any]] = []
    for case in KNOWN_SPARSE_CASES:
        file_name = str(case["file_name"])
        reported_split = str(case["split"])
        reported_id = int(case["image_id"])
        reported_version = frozen.reported_version(file_name, reported_split, reported_id)
        match = candidates[candidates["file_name"] == file_name]
        in_pool = not match.empty
        record = match.iloc[0] if in_pool else None
        canonical_matches = bool(
            record is not None
            and str(record["split"]) == reported_split
            and int(record["image_id"]) == reported_id
        )
        rows.append(
            {
                "file_name": file_name,
                "reported_split": reported_split,
                "reported_image_id": reported_id,
                "reported_version_present_in_provenance": reported_version is not None,
                "in_review_pool": bool(in_pool),
                "in_fit": bool(file_name in fit_filenames),
                "canonical_matches_reported_identity": canonical_matches,
                "record_key": None if record is None else str(record["record_key"]),
                "canonical_split": None if record is None else str(record["split"]),
                "canonical_image_id": None if record is None else int(record["image_id"]),
                "observed_annotations": (
                    None if record is None else int(record["n_annotations"])
                ),
                "reported_version_n_annotations": (
                    None if reported_version is None else reported_version["n_annotations"]
                ),
                "ranks": {},
                "priorities": {},
            }
        )
        if record is not None:
            for method in METHODS:
                for seed in SENSITIVITY_SEEDS:
                    rank_column = method_rank_column(method, seed)
                    priority_column = method_priority_column(method, seed)
                    rows[-1]["ranks"][f"{method}_seed{seed}"] = int(record[rank_column])
                    value = record[priority_column]
                    rows[-1]["priorities"][f"{method}_seed{seed}"] = (
                        None if pd.isna(value) else float(value)
                    )

    pool_size = int(len(candidates))
    recovery: Dict[str, Any] = {}
    for method in METHODS:
        for seed in SENSITIVITY_SEEDS:
            rank_column = method_rank_column(method, seed)
            per_budget = {}
            for k in budgets:
                identity_recovered = []
                filename_recovered = []
                for row in rows:
                    if not row["in_review_pool"]:
                        continue
                    match = candidates[candidates["file_name"] == row["file_name"]]
                    if match.empty:
                        continue
                    rank = int(match.iloc[0][rank_column])
                    if rank <= k:
                        filename_recovered.append(row["file_name"])
                        if (
                            row["reported_version_present_in_provenance"]
                            and row["canonical_matches_reported_identity"]
                        ):
                            identity_recovered.append(row["file_name"])
                per_budget[f"at_{k}"] = {
                    "recovered": len(identity_recovered),
                    "recovered_filename_level": len(filename_recovered),
                    "known_cases_total": len(KNOWN_SPARSE_CASES),
                    "pool_size": pool_size,
                    "budget": min(int(k), pool_size),
                    "recovered_filenames": identity_recovered,
                }
            recovery[f"{method}_seed{seed}"] = per_budget

    return {
        "kind": "known_case_regression",
        "evidence_type": (
            "regression exercise on nine known sparse-label cases; not recall over "
            "all mislabelled images and not a generalisation benchmark"
        ),
        "pool_definition": (
            "frozen review pool: every canonical non-fit row (held-out training "
            "groups, official validation split, official test split, and known-case "
            "rows excluded from fitting), deduplicated by filename"
        ),
        "recovery_rule": (
            "a case is recovered only when the reported (split, image_id, filename) "
            "exists in preserved provenance, the canonical version is that reported "
            "version, and it ranks within the budget; a filename-level count is "
            "reported separately"
        ),
        "pool_size": pool_size,
        "known_cases_total": len(KNOWN_SPARSE_CASES),
        "known_cases_in_pool": int(sum(1 for row in rows if row["in_review_pool"])),
        "known_cases_in_fit": int(sum(1 for row in rows if row["in_fit"])),
        "known_cases_reported_identity_present": int(
            sum(1 for row in rows if row["reported_version_present_in_provenance"])
        ),
        "known_cases_canonical_matches_reported_identity": int(
            sum(1 for row in rows if row["canonical_matches_reported_identity"])
        ),
        "upstream_issue": UPSTREAM_ISSUE,
        "upstream_reporter": UPSTREAM_REPORTER,
        "baseline_counts": [
            {
                "file_name": row["file_name"],
                "reported_image_id": row["reported_image_id"],
                "observed_annotations": row["observed_annotations"],
                "reported_version_n_annotations": row["reported_version_n_annotations"],
            }
            for row in rows
        ],
        "cases": rows,
        "recovery": recovery,
    }


# --------------------------------------------------------------------------- #
# B. synthetic annotation loss
# --------------------------------------------------------------------------- #


def _spread(values: Sequence[Any]) -> Dict[str, Any]:
    """Mean, spread and the raw per-seed values (``None`` entries excluded)."""
    finite = [float(v) for v in values if v is not None]
    if not finite:
        return {"n": 0, "mean": None, "min": None, "max": None, "range": None, "values": list(values)}
    return {
        "n": len(finite),
        "mean": sum(finite) / len(finite),
        "min": min(finite),
        "max": max(finite),
        "range": max(finite) - min(finite),
        "values": [None if v is None else float(v) for v in values],
    }


def _seed_summary(results: Dict[str, Any], budgets: Sequence[int]) -> Dict[str, Any]:
    """Mean/spread across seeds 7/42/101 at the fixed budgets, per method."""
    summary: Dict[str, Any] = {}
    for method in METHODS:
        per_budget = {}
        for k in budgets:
            per_budget[f"at_{k}"] = _spread(
                [
                    results[f"{method}_seed{seed}"]["precision_at_k"][f"at_{k}"]["precision"]
                    for seed in SENSITIVITY_SEEDS
                ]
            )
        summary[method] = {
            "precision_at_k": per_budget,
            "synthetic_roc_auc": _spread(
                [
                    results[f"{method}_seed{seed}"]["synthetic_roc_auc"]["auc"]
                    for seed in SENSITIVITY_SEEDS
                ]
            ),
            "note": (
                "seed sensitivity of the model/random reference; the corruption mask "
                "and partition are fixed across seeds"
            ),
        }
    return summary


def _select_loss_subset(pool: pd.DataFrame) -> List[str]:
    eligible = sorted(
        pool.loc[
            pool["n_annotations"] >= ANNOTATION_LOSS_MIN_ANNOTATIONS, "record_key"
        ].tolist()
    )
    if not eligible:
        return []
    rng = np.random.default_rng(ANNOTATION_LOSS_MASK_SEED)
    order = rng.permutation(len(eligible))
    take = max(1, int(round(len(eligible) * ANNOTATION_LOSS_SELECTION_FRACTION)))
    take = min(take, len(eligible))
    return sorted(eligible[i] for i in order[:take])


def _retention_plan(
    annotations: pd.DataFrame, selected: Sequence[Tuple[str, int]]
) -> Tuple[Dict[Tuple[str, int], Dict[str, Any]], Dict[str, Any]]:
    """Deterministically retain ~10% (>=1) of the annotations of each image.

    ``annotations`` must already be restricted to the selected images. The
    retained set is the lowest ``record_position`` values (a fixed, deliberately
    *structured* loss mask, not random dropout). Annotation rows are identified
    by their unique warehouse ``record_position`` so duplicate annotation ids
    (which the warehouse preserves) can never multiply kept rows; the original
    annotation ids are exported for provenance.
    """
    grouped = {
        (str(split), int(image_id)): group
        for (split, image_id), group in annotations.groupby(["split", "image_id"], sort=False)
    }
    retained: Dict[Tuple[str, int], Dict[str, Any]] = {}
    details: Dict[str, Any] = {}
    for split, image_id in selected:
        key = (split, int(image_id))
        group = grouped.get(key)
        if group is None:
            continue
        ordered = group.sort_values("record_position", kind="stable")
        positions = [int(v) for v in ordered["record_position"].tolist()]
        ids = [int(v) for v in ordered["id"].tolist()]
        n_original = len(positions)
        n_keep = max(1, int(round(ANNOTATION_LOSS_FRACTION * n_original)))
        n_keep = min(n_keep, n_original - 1) if n_original > 1 else n_original
        retained[key] = {
            "record_positions": positions[:n_keep],
            "ids": ids[:n_keep],
            "n_keep": n_keep,
        }
        details[f"{split}:{image_id}"] = {
            "split": split,
            "image_id": int(image_id),
            "annotations_original": n_original,
            "annotations_retained": n_keep,
            "retained_ids": ids[:n_keep],
            "retained_record_positions": positions[:n_keep],
            "duplicate_annotation_ids_in_image": n_original - len(set(ids)),
            "retention_fraction": n_keep / n_original,
        }
    return retained, details


def synthetic_annotation_loss(
    frozen: FrozenRanking,
    warehouse_dir: str,
    budgets: Sequence[int],
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """Held-out synthetic annotation-loss experiment over untouched held-out groups.

    Only the selected images' annotation *identities* are read from the
    warehouse, and only the retained rows are re-aggregated — in SQL, so an
    annotation ``bbox``/``area`` payload is never materialised for the whole
    pool. Selected images are chosen from the frozen candidate pool by
    ``record_key``; ``record_position`` identifies rows so duplicate annotation
    ids cannot multiply the retained set.
    """
    candidates = frozen.candidates
    pool = candidates[candidates["split"] == "train"].copy()
    known = pool["is_known_case"].to_numpy()
    pool = pool[~known].reset_index(drop=True)
    if pool.empty:
        raise EvaluationError("synthetic experiment pool is empty")
    if pool["file_name"].isin(
        set(str(case["file_name"]) for case in KNOWN_SPARSE_CASES)
    ).any():
        raise EvaluationError("known-case filenames leaked into the synthetic pool")

    selected_keys = _select_loss_subset(pool)
    if not selected_keys:
        raise EvaluationError("no eligible images for the synthetic annotation-loss experiment")

    selected_pairs = []
    for record_key in selected_keys:
        row = pool[pool["record_key"] == record_key].iloc[0]
        selected_pairs.append((str(row["split"]), int(row["image_id"])))

    # Restrict the annotation read to the selected images only (never the whole
    # held-out pool or the whole table) and take identity columns only. Unique
    # warehouse record_position identifies rows so duplicate annotation ids
    # cannot multiply the retained set.
    identities = load_annotation_identities(warehouse_dir, keys=selected_pairs)
    if identities.duplicated(subset=["split", "record_position"]).any():
        raise EvaluationError(
            "warehouse annotation record_position is not unique; unsupported input "
            "for the synthetic retention mask"
        )
    retained, details = _retention_plan(identities, selected_pairs)
    if not retained:
        raise EvaluationError("synthetic retention plan produced no candidates")
    duplicate_annotation_ids = int(
        sum(entry["duplicate_annotation_ids_in_image"] for entry in details.values())
    )

    # --- recompute label-dependent features from the retained records -------
    # Aggregated in SQL over exactly the retained record positions with the same
    # definitions as lc.v1_image_annotation_stats; no fabricated summary values.
    retained_positions = [
        position
        for (split, _image_id), plan in retained.items()
        for position in plan["record_positions"]
    ]
    recomputed = load_annotation_aggregates(
        warehouse_dir, record_positions=retained_positions
    )

    corrupted = pool.copy()
    for name in ANNOTATION_FEATURE_COLUMNS:
        corrupted[name] = corrupted[name].astype("float64")
    replay = recomputed.set_index(["split", "image_id"], drop=False)
    injected = np.zeros(len(corrupted), dtype=bool)
    for position, (split, image_id) in enumerate(
        zip(corrupted["split"], corrupted["image_id"])
    ):
        key = (str(split), int(image_id))
        if key not in retained:
            continue
        injected[position] = True
        if key not in replay.index:
            continue
        values = replay.loc[key]
        for name in ANNOTATION_FEATURE_COLUMNS:
            corrupted.at[position, name] = (
                float(values[name]) if pd.notna(values[name]) else np.nan
            )
        corrupted.at[position, "has_annotations"] = bool(values["n_annotations"] > 0)
        corrupted.at[position, "zero_annotation"] = bool(values["n_annotations"] == 0)
    corrupted["injected_annotation_loss"] = injected

    original_scored = frozen.score_frame(pool)
    corrupted_scored = frozen.score_frame(corrupted)

    labels_array = injected.astype("int64")
    results: Dict[str, Any] = {}
    for method in METHODS:
        for seed in SENSITIVITY_SEEDS:
            rank_column = method_rank_column(method, seed)
            priority_column = method_priority_column(method, seed)
            per_budget = {
                f"at_{k}": precision_at_k(
                    labels_array, corrupted_scored[rank_column].to_numpy(), k
                )
                for k in budgets
            }
            auc = roc_auc(labels_array, corrupted_scored[priority_column].to_numpy())
            deltas = (
                corrupted_scored[priority_column].to_numpy()
                - original_scored[priority_column].to_numpy()
            )
            injected_deltas = pd.Series(deltas[injected])
            raw_column = method_raw_column(method, seed)
            raw_deltas = (
                corrupted_scored[raw_column].to_numpy()
                - original_scored[raw_column].to_numpy()
            )
            injected_raw_deltas = pd.Series(raw_deltas[injected])
            results[f"{method}_seed{seed}"] = {
                "precision_at_k": per_budget,
                "synthetic_roc_auc": auc,
                "paired_priority_delta_injected": {
                    "count": int(injected_deltas.shape[0]),
                    "mean": float(injected_deltas.mean()) if len(injected_deltas) else None,
                    "median": float(injected_deltas.median()) if len(injected_deltas) else None,
                },
                "paired_raw_delta_injected": {
                    "raw_column": raw_column,
                    "count": int(injected_raw_deltas.shape[0]),
                    "mean": float(injected_raw_deltas.mean()) if len(injected_raw_deltas) else None,
                    "median": (
                        float(injected_raw_deltas.median()) if len(injected_raw_deltas) else None
                    ),
                },
            }

    seed_summary = _seed_summary(results, budgets)

    total_injected_annotations = sum(
        d["annotations_original"] - d["annotations_retained"] for d in details.values()
    )
    total_original_annotations = sum(d["annotations_original"] for d in details.values())
    payload = {
        "kind": "synthetic_annotation_loss",
        "evidence_type": (
            "simulation metrics on injected annotation loss; unchanged images are "
            "not expert-confirmed clean microscopy labels and synthetic precision "
            "is not real annotation-error precision"
        ),
        "pool_definition": (
            "untouched held-out training acquisition groups (canonical rows), known "
            "cases excluded; each physical image appears once, either original or "
            "synthetically corrupted"
        ),
        "pool_size": int(len(pool)),
        "injected_images": int(injected.sum()),
        "injected_fraction": float(injected.mean()),
        "selection": {
            "mask_seed": ANNOTATION_LOSS_MASK_SEED,
            "selection_fraction": ANNOTATION_LOSS_SELECTION_FRACTION,
            "min_annotations": ANNOTATION_LOSS_MIN_ANNOTATIONS,
            "loss_fraction": ANNOTATION_LOSS_FRACTION,
            "retention_rule": (
                "retain max(1, round(loss_fraction * n_original)) annotations, capped "
                "at n_original - 1; the retained set is the lowest annotation "
                "record_position values"
            ),
            "loss_scenario": (
                "structured loss: the retained annotations are the lowest warehouse "
                "record_position values of each image. This is a deliberately "
                "structured removal scenario, not random annotation dropout."
            ),
            "retained_annotations": int(total_original_annotations - total_injected_annotations),
            "original_annotations_of_injected": int(total_original_annotations),
            "duplicate_annotation_ids_in_injected_images": duplicate_annotation_ids,
            "annotation_row_identity": (
                "annotations are identified by unique warehouse record_position; "
                "duplicate annotation ids (preserved by the warehouse) cannot "
                "multiply the retained rows"
            ),
        },
        "sensitivity_scope": (
            "seeds vary the Isolation Forest fit and the random reference only; the "
            "synthetic corruption mask (mask_seed "
            f"{ANNOTATION_LOSS_MASK_SEED}) and the group partition are fixed across "
            "model seeds. This is model/random-reference seed sensitivity, not "
            "corruption-mask or partition-seed robustness."
        ),
        "feature_recomputation": (
            "every label-dependent feature was recomputed from the retained "
            "annotation records with the warehouse SQL aggregation "
            "(features.aggregate_annotation_features_sql), the same definitions as "
            "lc.v1_image_annotation_stats; image-only features are reused unchanged"
        ),
        "per_image": details,
        "results": results,
        "seed_summary": seed_summary,
        "seeds": list(SENSITIVITY_SEEDS),
        "budgets": list(budgets),
    }

    paired_rows = []
    for position, (record_key, file_name, split, image_id) in enumerate(
        zip(
            corrupted["record_key"],
            corrupted["file_name"],
            corrupted["split"],
            corrupted["image_id"],
        )
    ):
        if not injected[position]:
            continue
        entry = {
            "record_key": str(record_key),
            "file_name": str(file_name),
            "split": str(split),
            "image_id": int(image_id),
            "n_annotations_original": int(pool.iloc[position]["n_annotations"]),
            "n_annotations_corrupted": int(corrupted.iloc[position]["n_annotations"]),
        }
        for method in METHODS:
            for seed in SENSITIVITY_SEEDS:
                priority_column = method_priority_column(method, seed)
                raw_column = method_raw_column(method, seed)
                entry[f"{method}_seed{seed}_original"] = float(
                    original_scored.iloc[position][priority_column]
                )
                entry[f"{method}_seed{seed}_corrupted"] = float(
                    corrupted_scored.iloc[position][priority_column]
                )
                entry[f"{method}_seed{seed}_raw_original"] = float(
                    original_scored.iloc[position][raw_column]
                )
                entry[f"{method}_seed{seed}_raw_corrupted"] = float(
                    corrupted_scored.iloc[position][raw_column]
                )
        paired_rows.append(entry)
    paired = pd.DataFrame(paired_rows)
    return payload, paired


# --------------------------------------------------------------------------- #
# C. frozen human review sample
# --------------------------------------------------------------------------- #


def build_review_sample(candidates: pd.DataFrame) -> Dict[str, Any]:
    """Disjoint top-ML, top-baseline and random review strata with blank fields."""
    selected: List[Dict[str, Any]] = []
    taken: set = set()

    def take(frame: pd.DataFrame, rank_column: str, stratum: str, size: int) -> None:
        ordered = frame.sort_values(
            [rank_column, "file_name", "record_key"], kind="stable"
        )
        count = 0
        for row in ordered.itertuples(index=False):
            key = str(row.record_key)
            if key in taken:
                continue
            taken.add(key)
            selected.append(
                {
                    "stratum": stratum,
                    "record_key": key,
                    "file_name": str(row.file_name),
                    "split": str(row.split),
                    "cell_line": None if pd.isna(row.cell_line) else str(row.cell_line),
                    "is_known_case": bool(row.is_known_case),
                    "n_annotations": int(row.n_annotations),
                    "if_rank": int(row.if_rank),
                    "if_priority": None if pd.isna(row.if_priority) else float(row.if_priority),
                    "robust_rank": int(row.robust_rank),
                    "robust_priority": (
                        None if pd.isna(row.robust_priority) else float(row.robust_priority)
                    ),
                    "low_count_rank": int(row.low_count_rank),
                    "random_rank": int(row.random_rank),
                    # Blank, to be filled by a qualified human reviewer.
                    "judgment": None,
                    "reason": None,
                    "reviewer_expertise": None,
                    "reviewer_id": None,
                }
            )
            count += 1
            if count >= size:
                break

    take(candidates, "if_rank", "top_isolation_forest", REVIEW_STRATUM_SIZE)
    take(candidates, "robust_rank", "top_robust_baseline", REVIEW_STRATUM_SIZE)
    take(candidates, "low_count_rank", "top_low_count_baseline", REVIEW_STRATUM_SIZE)

    remaining = candidates[~candidates["record_key"].isin(taken)]
    rng = np.random.default_rng(REVIEW_SAMPLE_SEED)
    ordered = remaining.sort_values(["file_name", "record_key"], kind="stable")
    if len(ordered):
        picks = rng.permutation(len(ordered))[:REVIEW_STRATUM_SIZE]
        pool_rows = ordered.iloc[sorted(int(p) for p in picks)]
    else:
        pool_rows = ordered
    take(pool_rows, "if_rank", "random_sample", REVIEW_STRATUM_SIZE)

    return {
        "kind": "frozen_human_review_sample",
        "status": "awaiting_scientific_review",
        "reviewer_note": (
            "no qualified scientific reviewer is currently supplied; no judgment, "
            "reason or expertise field is fabricated and no expert validation is "
            "claimed. Real-world precision is unmeasured."
        ),
        "sampling_method": (
            "disjoint strata: top Isolation Forest (seed 42), top robust statistical "
            "baseline, top low-count baseline, then a deterministic random sample of "
            "the remainder (seed "
            f"{REVIEW_SAMPLE_SEED}); each stratum takes up to {REVIEW_STRATUM_SIZE} "
            "rows already not selected by an earlier stratum"
        ),
        "sampling_caveat": (
            "enriched (top-ranked + random) sample; do not read an unweighted "
            "population precision estimate from it"
        ),
        "review_criteria": [
            "Does the image show a cell population consistent with an unexpectedly "
            "low or unusually distributed annotation count?",
            "Are there visible unannotated cells?",
            "Is the image acquisition obviously degraded (focus, exposure, artefact)?",
            "Is the annotation plausible despite the low count?",
        ],
        "strata": list(REVIEW_STRATA),
        "reviewers": [],
        "records": selected,
    }


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #


def _public_summary(
    known: Dict[str, Any],
    synthetic: Dict[str, Any],
    sample: Dict[str, Any],
) -> Dict[str, Any]:
    """Compact machine-readable evaluation summary with no private paths."""
    best: Dict[str, Any] = {}
    for method in METHODS:
        for seed in SENSITIVITY_SEEDS:
            key = f"{method}_seed{seed}"
            entry = synthetic["results"][key]
            best[key] = {
                "precision_at_k": {
                    name: value["precision"] for name, value in entry["precision_at_k"].items()
                },
                "synthetic_roc_auc": entry["synthetic_roc_auc"]["auc"],
            }
    known_spread = {}
    for method in METHODS:
        known_spread[method] = {
            f"at_{budget}": _spread(
                [
                    known["recovery"][f"{method}_seed{seed}"][f"at_{budget}"]["recovered"]
                    for seed in SENSITIVITY_SEEDS
                ]
            )
            for budget in synthetic["budgets"]
        }
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "stage": "evaluation",
        "known_case_regression": {
            "pool_size": known["pool_size"],
            "known_cases_total": known["known_cases_total"],
            "known_cases_in_pool": known["known_cases_in_pool"],
            "known_cases_in_fit": known["known_cases_in_fit"],
            "known_cases_reported_identity_present": known[
                "known_cases_reported_identity_present"
            ],
            "known_cases_canonical_matches_reported_identity": known[
                "known_cases_canonical_matches_reported_identity"
            ],
            "recovery": {
                key: {
                    budget: value["recovered"] for budget, value in per_budget.items()
                }
                for key, per_budget in known["recovery"].items()
            },
            "recovery_seed_spread": known_spread,
        },
        "synthetic_annotation_loss": {
            "pool_size": synthetic["pool_size"],
            "injected_images": synthetic["injected_images"],
            "injected_fraction": synthetic["injected_fraction"],
            "budgets": synthetic["budgets"],
            "seeds": synthetic["seeds"],
            "metrics": best,
            "seed_summary": synthetic["seed_summary"],
            "loss_scenario": synthetic["selection"]["loss_scenario"],
        },
        "human_review": {
            "status": sample["status"],
            "records": len(sample["records"]),
            "strata": sample["strata"],
            "real_world_precision": None,
        },
        "evidence_notes": [
            "known-case recovery is a regression exercise on nine examples, not recall",
            "synthetic precision/AUC are simulation metrics, not real error precision",
            "real-world precision is unmeasured; no expert review available",
            "seed spread covers model/random-reference seeds only; mask and partition are fixed",
        ],
    }


def _write_known_cases(known: Dict[str, Any], staging_dir: str) -> None:
    dump_json(os.path.join(staging_dir, "known_cases.json"), known)
    frame = pd.DataFrame(
        [
            {
                "file_name": row["file_name"],
                "reported_split": row["reported_split"],
                "reported_image_id": row["reported_image_id"],
                "reported_version_present_in_provenance": row[
                    "reported_version_present_in_provenance"
                ],
                "canonical_split": row["canonical_split"],
                "canonical_image_id": row["canonical_image_id"],
                "canonical_matches_reported_identity": row[
                    "canonical_matches_reported_identity"
                ],
                "in_review_pool": row["in_review_pool"],
                "in_fit": row["in_fit"],
                "observed_annotations": row["observed_annotations"],
                "reported_version_n_annotations": row["reported_version_n_annotations"],
                **{f"rank_{k}": v for k, v in row["ranks"].items()},
            }
            for row in known["cases"]
        ]
    )
    frame.to_csv(os.path.join(staging_dir, "known_cases.csv"), index=False)


def _write_review_sample(sample: Dict[str, Any], staging_dir: str) -> None:
    dump_json(os.path.join(staging_dir, "review_sample.json"), sample)
    pd.DataFrame(sample["records"]).to_csv(
        os.path.join(staging_dir, "review_sample.csv"), index=False
    )


def run_evaluation(config: EvaluationConfig) -> Dict[str, Any]:
    """Run the three evidence tracks over a completed ranking run."""
    started = time.perf_counter()
    features_dir = os.path.abspath(config.features_dir)
    ranking_dir = os.path.abspath(config.ranking_dir)
    warehouse_dir = os.path.abspath(config.warehouse_dir)
    out_dir = os.path.abspath(config.out_dir)
    marker_path = os.path.join(out_dir, COMPLETION_MARKER)
    staging_dir = os.path.join(out_dir, STAGING_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    try:
        clear_stale(out_dir)
        feature_marker, _dictionary, _warehouse_marker = require_verified_inputs(
            features_dir, warehouse_dir
        )
        ranking_status = verify_ranking_outputs(ranking_dir)
        if not ranking_status["ok"]:
            raise EvaluationError(
                "ranking inputs are not usable: " + "; ".join(ranking_status["problems"])
            )
        ranking_marker = ranking_status["marker"]
        # Exact build linkage: a ranking or feature build with the same COCO
        # source hashes but a different feature/warehouse manifest identity
        # (for example after TIFF pixels changed) must never be mixed.
        try:
            feature_build = assert_build_consistency(features_dir, ranking_dir=ranking_dir)
        except RankingError as exc:
            raise EvaluationError(f"inputs are not consistent: {exc}") from exc
        if ranking_marker.get("source_sha256") != feature_marker.get("source_sha256"):
            raise EvaluationError(
                "ranking run was built from different source data than the features"
            )
        frozen = load_frozen_ranking(ranking_dir)

        os.makedirs(staging_dir)
        build_started = time.perf_counter()
        known = known_case_regression(frozen, config.budgets)
        # The synthetic experiment reads only the selected images' annotation
        # identities and re-aggregates only the retained rows, in SQL — never the
        # whole 1.66 M-row table and never the whole held-out pool.
        synthetic, paired = synthetic_annotation_loss(frozen, warehouse_dir, config.budgets)
        sample = build_review_sample(frozen.candidates)
        build_seconds = time.perf_counter() - build_started

        _write_known_cases(known, staging_dir)
        dump_json(
            os.path.join(staging_dir, "synthetic_annotation_loss.json"), synthetic
        )
        paired.to_csv(
            os.path.join(staging_dir, "synthetic_paired_deltas.csv"), index=False
        )
        _write_review_sample(sample, staging_dir)
        summary = _public_summary(known, synthetic, sample)
        dump_json(os.path.join(staging_dir, "evaluation_summary.json"), summary)

        run_record = {
            "tool": {"name": "livecell-audit", "version": __version__},
            "stage": "evaluation",
            "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
            "features_dir": features_dir,
            "ranking_dir": ranking_dir,
            "warehouse_dir": warehouse_dir,
            "out_dir": out_dir,
            "budgets": list(config.budgets),
            "seeds": list(SENSITIVITY_SEEDS),
            "feature_build": feature_build,
            "ranking_build": {
                "ranking_manifest_sha256": (ranking_marker.get("manifest") or {}).get("sha256"),
                "feature_manifest_sha256": (ranking_marker.get("feature_build") or {}).get(
                    "feature_manifest_sha256"
                ),
                "warehouse_manifest_sha256": ranking_marker.get("warehouse_manifest_sha256"),
            },
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "build_seconds": round(build_seconds, 3),
            "resource_limits": {
                "duckdb_threads": WAREHOUSE_THREADS,
                "duckdb_memory_limit_mb": WAREHOUSE_MEMORY_LIMIT_MB,
                "note": (
                    "annotation reads are bounded to the selected/retained rows and "
                    "capped so a run stays inside the capped job budget"
                ),
            },
            "versions": {
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "duckdb": duckdb.__version__,
            },
        }
        dump_json(os.path.join(staging_dir, "run.json"), run_record)

        manifest = build_manifest(
            "evaluation",
            EVALUATION_SCHEMA_VERSION,
            staging_dir,
            EVALUATION_OUTPUT_PATHS,
            extra={"partial": False},
        )
        dump_json(os.path.join(staging_dir, MANIFEST_NAME), manifest)
        manifest_digest, manifest_bytes = sha256_file(os.path.join(staging_dir, MANIFEST_NAME))

        promote_staged(staging_dir, out_dir)
        write_completion_marker(
            marker_path,
            {
                "status": "completed",
                "stage": "evaluation",
                "partial": False,
                "tool": {"name": "livecell-audit", "version": __version__},
                "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "features_dir": features_dir,
                "ranking_dir": ranking_dir,
                "warehouse_dir": warehouse_dir,
                "known_cases_total": known["known_cases_total"],
                "known_cases_in_pool": known["known_cases_in_pool"],
                "synthetic_pool_size": synthetic["pool_size"],
                "synthetic_injected_images": synthetic["injected_images"],
                "review_sample_records": len(sample["records"]),
                "source_sha256": feature_marker.get("source_sha256"),
                "feature_build": feature_build,
                "ranking_build": {
                    "ranking_manifest_sha256": (ranking_marker.get("manifest") or {}).get("sha256"),
                    "feature_manifest_sha256": (ranking_marker.get("feature_build") or {}).get(
                        "feature_manifest_sha256"
                    ),
                    "warehouse_manifest_sha256": ranking_marker.get("warehouse_manifest_sha256"),
                },
                "manifest": {
                    "file": MANIFEST_NAME,
                    "sha256": manifest_digest,
                    "bytes": manifest_bytes,
                },
                "outputs": {
                    "known_cases": "known_cases.json",
                    "synthetic": "synthetic_annotation_loss.json",
                    "paired_deltas": "synthetic_paired_deltas.csv",
                    "review_sample": "review_sample.json",
                    "summary": "evaluation_summary.json",
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
        "known_cases": known,
        "synthetic": synthetic,
        "review_sample": sample,
        "summary": summary,
        "marker": read_json(marker_path),
        "out_dir": out_dir,
    }


def verify_evaluation_outputs(out_dir: str) -> Dict[str, Any]:
    """Verify a completed evaluation run's marker, manifest and counts."""
    out_dir = os.path.abspath(out_dir)
    marker = load_completion_marker(out_dir)
    if marker is None:
        return {
            "ok": False,
            "problems": [f"not a completed evaluation run: {out_dir}"],
            "marker": None,
            "manifest": None,
        }
    problems: List[str] = []
    if marker.get("stage") != "evaluation":
        problems.append(f"unexpected stage {marker.get('stage')!r}")
    if str(marker.get("evaluation_schema_version")) != EVALUATION_SCHEMA_VERSION:
        problems.append(
            f"evaluation_schema_version {marker.get('evaluation_schema_version')!r} "
            f"!= {EVALUATION_SCHEMA_VERSION!r}"
        )
    if marker.get("partial"):
        problems.append("evaluation run is marked partial")
    feature_build = marker.get("feature_build") or {}
    if not feature_build.get("feature_manifest_sha256"):
        problems.append("evaluation marker has no feature build identity")
    ranking_build = marker.get("ranking_build") or {}
    if ranking_build.get("feature_manifest_sha256") != feature_build.get("feature_manifest_sha256"):
        problems.append("evaluation feature build != its ranking build (mixed builds)")
    manifest_info = marker.get("manifest") or {}
    if not manifest_info.get("sha256"):
        problems.append("marker has no manifest checksum")
        return {"ok": False, "problems": problems, "marker": marker, "manifest": None}
    result = verify_manifest(
        out_dir,
        stage="evaluation",
        expected_sha256=manifest_info.get("sha256"),
        required_paths=EVALUATION_REQUIRED_OUTPUTS,
    )
    problems.extend(result["problems"])
    sample_path = os.path.join(out_dir, "review_sample.json")
    if os.path.isfile(sample_path):
        sample = read_json(sample_path)
        if len(sample.get("records", [])) != marker.get("review_sample_records"):
            problems.append("review_sample records != marker review_sample_records")
        for record in sample.get("records", []):
            if record.get("judgment") is not None or record.get("reviewer_expertise") is not None:
                problems.append("review sample contains a fabricated judgment")
                break
    else:
        problems.append("missing review_sample.json")
    return {"ok": not problems, "problems": problems, "marker": marker, "manifest": result["manifest"]}


def assert_evaluation_ready(out_dir: str) -> Dict[str, Any]:
    """Return the marker of a verified evaluation run or raise."""
    result = verify_evaluation_outputs(out_dir)
    if not result["ok"]:
        raise EvaluationError(
            "evaluation outputs are not release-ready: " + "; ".join(result["problems"])
        )
    return result["marker"]
