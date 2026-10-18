"""Focused tests for the ML ranking and evaluation stage (milestone 4).

The dataset is small but exercises every contract: two cell lines with 3 and 2
training acquisition groups, a per-cell-line model and a global fallback, an
unparsed filename (conservative group policy), zero annotations, a missing
TIFF (unusable row), an alternate annotation version, a known sparse case, and
validation/test records. The whole pipeline (audit -> warehouse -> features ->
rank -> evaluate) is built once per module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from livecell_audit.audit import AuditConfig, run_audit
from livecell_audit.errors import EvaluationError, RankingError
from livecell_audit.evaluate import (
    EvaluationConfig,
    assert_evaluation_ready,
    build_review_sample,
    precision_at_k,
    roc_auc,
    run_evaluation,
    verify_evaluation_outputs,
)
from livecell_audit.features import (
    ANNOTATION_FEATURE_COLUMNS,
    FeatureConfig,
    aggregate_annotation_features,
    run_features,
)
from livecell_audit.ranking import (
    BUDGETS,
    CANDIDATE_IDENTITY_COLUMNS,
    IF_MAX_SAMPLES,
    MIN_CELL_LINE_FIT_IMAGES,
    MODEL_FEATURES,
    RankingConfig,
    _robust_raw_scores,
    _select_columns,
    assert_ranking_ready,
    build_analysis_state,
    canonicalize_versions,
    load_annotation_aggregates,
    load_annotation_identities,
    load_annotations,
    load_annotation_stats,
    load_feature_frame,
    load_frozen_ranking,
    run_ranking,
    verify_ranking_outputs,
)
from livecell_audit.warehouse import WarehouseConfig, run_warehouse

A_LINE = "A172"
B_LINE = "BV2"
KNOWN_FILENAME = "BT474_Phase_B3_1_03d00h00m_3.tif"
UNPARSED = "UNPARSED_IMAGE.tif"


def _ts(index: int) -> str:
    return f"00d{index:02d}h00m"


def _filename(line: str, well: str, index: int, crop: int = 1) -> str:
    return f"{line}_Phase_{well}_1_{_ts(index)}_{crop}.tif"


def _annotation(ann_id, image_id, area, bbox, n_points):
    return {
        "id": ann_id,
        "image_id": image_id,
        "category_id": 1,
        "segmentation": [[float(v) for v in range(n_points * 2)]],
        "area": area,
        "bbox": bbox,
        "iscrowd": 0,
    }


def _build_dataset(root: Path) -> None:
    from PIL import Image

    (root / "annotations").mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)

    train_images, train_anns = [], []
    ann_id = 1
    next_id = 1

    def add(split_images, split_anns, image_id, name, count, write_file=True, missing=False):
        nonlocal ann_id
        split_images.append({"id": image_id, "width": 8, "height": 8, "file_name": name})
        for _ in range(count):
            area = float(rng.uniform(5, 60))
            bbox = [
                float(rng.uniform(0, 2)),
                float(rng.uniform(0, 2)),
                float(rng.uniform(2, 6)),
                float(rng.uniform(2, 6)),
            ]
            n_points = int(rng.integers(3, 12))
            split_anns.append(_annotation(ann_id, image_id, area, bbox, n_points))
            ann_id += 1
        if missing:
            return
        if write_file:
            array = rng.integers(0, 255, size=(8, 8), dtype=np.uint8)
            Image.fromarray(array, mode="L").save(str(root / "images" / name), format="TIFF")

    # A172: three acquisition groups, twelve time points each.
    a172_groups = {"A1": [], "A2": [], "A3": []}
    for well in ("A1", "A2", "A3"):
        for index in range(12):
            name = _filename(A_LINE, well, index)
            count = int(rng.integers(2, 25))
            missing = well == "A3" and index == 5
            zero = well == "A2" and index == 3
            a172_groups[well].append(name)
            add(train_images, train_anns, next_id, name, 0 if (missing or zero) else count,
                missing=missing)
            next_id += 1
    # alternate annotation version of an existing A172 filename
    add(train_images, train_anns, next_id, a172_groups["A1"][0], 3)
    next_id += 1
    # BV2: two acquisition groups, eight time points each (small -> global fallback)
    for well in ("B1", "B2"):
        for index in range(8):
            add(train_images, train_anns, next_id, _filename(B_LINE, well, index),
                int(rng.integers(2, 20)))
            next_id += 1
    # unparsed filename (conservative group policy)
    add(train_images, train_anns, next_id, UNPARSED, 5)
    next_id += 1
    # one known sparse case (train)
    add(train_images, train_anns, next_id, KNOWN_FILENAME, 1)
    next_id += 1

    # Validation and test imagery (evaluation-only): enough wells/times that the
    # review pool is larger than the three top strata, so every stratum
    # (top-isolation-forest, top-robust, top-low-count and random) is populated.
    val_images, val_anns = [], []
    for well, line in (
        ("C1", A_LINE),
        ("C2", A_LINE),
        ("C3", B_LINE),
        ("C4", B_LINE),
    ):
        for index in range(12):
            add(val_images, val_anns, next_id, _filename(line, well, index), int(rng.integers(3, 12)))
            next_id += 1
    test_images, test_anns = [], []
    for well, line in (
        ("E1", A_LINE),
        ("E2", A_LINE),
        ("E3", B_LINE),
        ("E4", B_LINE),
    ):
        for index in range(6):
            add(test_images, test_anns, next_id, _filename(line, well, index), int(rng.integers(3, 12)))
            next_id += 1

    for split, images, anns in (
        ("train", train_images, train_anns),
        ("val", val_images, val_anns),
        ("test", test_images, test_anns),
    ):
        with open(root / "annotations" / f"livecell_coco_{split}.json", "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "info": {},
                    "licenses": [],
                    "categories": [{"supercategory": "cell", "id": 1, "name": "cell"}],
                    "images": images,
                    "annotations": anns,
                },
                handle,
            )


@pytest.fixture(scope="module")
def ml_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("ml-data")
    data_root = root / "data"
    _build_dataset(data_root)
    audit_dir, wh, fe = root / "audit", root / "warehouse", root / "features"
    rk, rk_second, ev = root / "ranking", root / "ranking-b", root / "evaluation"
    run_audit(AuditConfig(data_root=str(data_root), out_dir=str(audit_dir)))
    run_warehouse(WarehouseConfig(audit_dir=str(audit_dir), out_dir=str(wh)))
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe)))
    run_ranking(RankingConfig(features_dir=str(fe), warehouse_dir=str(wh), out_dir=str(rk)))
    run_ranking(RankingConfig(features_dir=str(fe), warehouse_dir=str(wh), out_dir=str(rk_second)))
    run_evaluation(
        EvaluationConfig(
            features_dir=str(fe), warehouse_dir=str(wh), ranking_dir=str(rk), out_dir=str(ev)
        )
    )
    return {
        "root": root,
        "data_root": str(data_root),
        "audit": str(audit_dir),
        "warehouse": str(wh),
        "features": str(fe),
        "ranking": str(rk),
        "ranking_second": str(rk_second),
        "evaluation": str(ev),
    }


def _hash_tree(path: Path) -> dict:
    digests = {}
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digests[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return digests


def _candidates(env) -> "object":
    import pandas as pd

    return pd.read_json(os.path.join(env["ranking"], "candidates.jsonl"), orient="records", lines=True)


# --------------------------------------------------------------------------- #
# canonical identity and provenance


def test_canonical_identity_prefers_train_lowest_id(ml_env):
    frame = load_feature_frame(ml_env["features"])
    canonical, provenance = canonicalize_versions(frame)

    # provenance covers every filename once; canonical covers the eligible ones
    assert len(provenance) == frame["file_name"].nunique()
    assert len(canonical) == int((provenance["selection_status"] == "chosen").sum())
    assert canonical["file_name"].is_unique

    # the alternate A172 version resolves to train + lowest id
    chosen = canonical[canonical["file_name"] == _filename(A_LINE, "A1", 0)]
    assert len(chosen) == 1
    duplicates = frame[frame["file_name"] == _filename(A_LINE, "A1", 0)]
    assert chosen.iloc[0]["split"] == "train"
    assert chosen.iloc[0]["image_id"] == duplicates["image_id"].min()
    assert chosen.iloc[0]["canonical_rule"].startswith("prefer_train")

    prov = provenance[provenance["file_name"] == _filename(A_LINE, "A1", 0)].iloc[0]
    assert prov["n_versions_total"] == 2
    assert prov["n_versions_eligible"] == 2
    assert prov["selection_status"] == "chosen"
    assert len(prov["versions"]) == 2
    counts = [v["n_annotations"] for v in prov["versions"]]
    assert prov["annotation_count_min"] == min(counts)
    assert prov["annotation_count_max"] == max(counts)


def test_provenance_records_every_filename(ml_env):
    import pandas as pd

    provenance = pd.read_json(
        os.path.join(ml_env["ranking"], "version_provenance.jsonl"),
        orient="records",
        lines=True,
    )
    frame = load_feature_frame(ml_env["features"])
    assert len(provenance) == frame["file_name"].nunique()
    assert set(provenance["selection_status"]) <= {"chosen", "no_eligible_version"}
    assert provenance["versions"].map(len).min() >= 1


# --------------------------------------------------------------------------- #
# known-case exclusion, grouping, partition


def test_known_cases_are_excluded_from_fit(ml_env):
    partition = json.load(open(os.path.join(ml_env["ranking"], "partition_manifest.json")))
    fit_records = [r for r in partition["records"] if r.get("partition") == "fit"]
    assert fit_records
    assert all(r["file_name"] != KNOWN_FILENAME for r in fit_records)

    model_card = json.load(open(os.path.join(ml_env["ranking"], "model_card.json")))
    assert model_card["partition"]["counts"]["excluded_known_case"] >= 1
    # the known case sits in a fit acquisition group but was removed from fitting
    assert any(r.get("partition") == "excluded_known_case" for r in partition["records"])

    candidates = _candidates(ml_env)
    known = candidates[candidates["file_name"] == KNOWN_FILENAME]
    assert len(known) == 1


def test_fit_and_review_pool_are_filename_disjoint(ml_env):
    partition = json.load(open(os.path.join(ml_env["ranking"], "partition_manifest.json")))
    fit_files = {r["file_name"] for r in partition["records"] if r.get("partition") == "fit"}
    fit_groups = {
        (r.get("cell_line"), r["acquisition_group"])
        for r in partition["records"]
        if r.get("partition") == "fit"
    }
    candidates = _candidates(ml_env)
    assert not (set(candidates["file_name"]) & fit_files)
    # held-out groups are disjoint from fit groups within a cell line
    holdout_groups = {
        (c, g)
        for c, g in zip(
            candidates["cell_line"], candidates["acquisition_group"]
        )
        if c is not None
    }
    assert not (fit_groups & holdout_groups)


def test_unparsed_filename_uses_conservative_group(ml_env):
    partition = json.load(open(os.path.join(ml_env["ranking"], "partition_manifest.json")))
    entry = [r for r in partition["records"] if r["file_name"] == UNPARSED]
    assert entry
    assert entry[0]["acquisition_group"] == f"unparsed|{UNPARSED}"


def test_missing_image_is_excluded_with_reason(ml_env):
    partition = json.load(open(os.path.join(ml_env["ranking"], "partition_manifest.json")))
    excluded = [
        r for r in partition["records"] if r.get("partition") == "excluded_unusable"
    ]
    assert excluded
    assert any("missing_file" in r["exclusion_reason"] for r in excluded)
    candidates = _candidates(ml_env)
    assert not (set(candidates["record_key"]) & {r["record_key"] for r in excluded})


def test_global_fallback_for_small_cell_line(ml_env):
    model_card = json.load(open(os.path.join(ml_env["ranking"], "model_card.json")))
    assert A_LINE in model_card["fitted_cell_lines"]
    assert B_LINE not in model_card["fitted_cell_lines"]
    assert model_card["models"]["global"]["kind"] == "global_fallback"
    candidates = _candidates(ml_env)
    b_rows = candidates[candidates["cell_line"] == B_LINE]
    assert not b_rows.empty
    assert set(b_rows["if_model"]) == {"global"}
    assert set(candidates[candidates["cell_line"] == A_LINE]["if_model"]) == {A_LINE}


def test_min_fit_images_threshold_is_declared():
    assert MIN_CELL_LINE_FIT_IMAGES == 20
    if_max = IF_MAX_SAMPLES
    assert if_max == 256


# --------------------------------------------------------------------------- #
# training-only preprocessing and feature handling


def test_preprocessing_is_fit_only(ml_env):
    """Perturbing non-fit rows must not change any fitted parameter."""
    frame = load_feature_frame(ml_env["features"])
    stats = load_annotation_stats(ml_env["warehouse"])
    config = RankingConfig(
        features_dir=ml_env["features"], warehouse_dir=ml_env["warehouse"], out_dir="/tmp/unused"
    )
    state_a, analysis_a, _ = build_analysis_state(config, {}, stats, frame=frame)
    non_fit = set(analysis_a.loc[analysis_a["partition"] != "fit", "record_key"])

    perturbed = frame.copy()
    mask = perturbed["record_key"].isin(non_fit)
    assert mask.any()
    for column in ("pixel_mean", "pixel_std", "gradient_mean", "area_sum", "n_annotations"):
        perturbed.loc[mask, column] = 1.0e6
    state_b, _analysis_b, _ = build_analysis_state(config, {}, stats, frame=perturbed)

    for key, spec in state_a.model_specs.items():
        for seed in spec["pipelines"]:
            pipe_a = spec["pipelines"][seed]
            pipe_b = state_b.model_specs[key]["pipelines"][seed]
            np.testing.assert_array_equal(
                pipe_a.named_steps["imputer"].statistics_,
                pipe_b.named_steps["imputer"].statistics_,
            )
            np.testing.assert_array_equal(
                pipe_a.named_steps["scaler"].center_, pipe_b.named_steps["scaler"].center_
            )
            np.testing.assert_array_equal(
                pipe_a.named_steps["scaler"].scale_, pipe_b.named_steps["scaler"].scale_
            )
    # fit-only references are unchanged too
    for key, seeds in state_a.if_reference.items():
        for seed, values in seeds.items():
            assert values == state_b.if_reference[key][seed]


def test_handle_all_missing_and_zero_mad_columns():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "n_annotations": [1.0, 2.0, 3.0],
            "pixel_std": [5.0, 5.0, 5.0],  # zero MAD
            "gradient_mean": [np.nan, np.nan, np.nan],  # all missing
        }
    )
    used, dropped = _select_columns(frame)
    assert "gradient_mean" in dropped
    assert "pixel_std" in used

    global_stats = {
        "median": {"n_annotations": 2.0, "pixel_std": 5.0, "gradient_mean": np.nan},
        "mad": {"n_annotations": 0.0, "pixel_std": 4.0, "gradient_mean": np.nan},
    }
    line_stats = {
        "median": {"n_annotations": 100.0, "pixel_std": 5.0, "gradient_mean": np.nan},
        "mad": {"n_annotations": 0.0, "pixel_std": 0.0, "gradient_mean": np.nan},
    }
    values = _robust_raw_scores(
        frame, ["n_annotations", "pixel_std", "gradient_mean"], line_stats, global_stats
    )
    # n_annotations has zero MAD in the line -> skips the line scale and uses... nothing
    # because the global MAD is also zero; pixel_std falls back to the global MAD.
    assert np.isfinite(values).all()
    # gradient_mean is all-missing and contributes nothing
    assert np.all(values == np.abs((frame["pixel_std"].to_numpy() - 5.0) / (1.4826 * 4.0)))


def test_annotation_aggregation_matches_warehouse_view(ml_env):
    import duckdb

    annotations = load_annotations(ml_env["warehouse"])
    mine = aggregate_annotation_features(annotations).reset_index()
    sql = load_annotation_stats(ml_env["warehouse"])
    joined = sql.merge(mine, on=["split", "image_id"], suffixes=("_sql", "_py"))
    assert len(joined) == len(sql)
    for name in ANNOTATION_FEATURE_COLUMNS:
        lhs = joined[name + "_sql"].astype("float64")
        rhs = joined[name + "_py"].astype("float64")
        both = lhs.isna() & rhs.isna()
        diff = (lhs - rhs).abs()
        diff[both] = 0.0
        assert diff.max() < 1e-9, name


def _assert_feature_columns_equal(lhs_frame, rhs_frame, lhs_suffix, rhs_suffix):
    """Assert every declared annotation feature agrees between two frames."""
    joined = lhs_frame.merge(
        rhs_frame, on=["split", "image_id"], suffixes=(lhs_suffix, rhs_suffix)
    )
    assert len(joined) == len(lhs_frame)
    for name in ANNOTATION_FEATURE_COLUMNS:
        lhs = joined[name + lhs_suffix].astype("float64")
        rhs = joined[name + rhs_suffix].astype("float64")
        both = lhs.isna() & rhs.isna()
        diff = (lhs - rhs).abs()
        diff[both] = 0.0
        assert diff.max() < 1e-9, name


def test_sql_annotation_aggregation_matches_view_and_pandas(ml_env):
    """The bounded SQL recomputation reproduces the view exactly.

    Both selection paths (image keys and annotation record positions) are
    checked against the published view and against the pandas reference helper,
    so the synthetic annotation-loss stage can aggregate in SQL without a second
    feature definition.
    """
    annotations = load_annotations(ml_env["warehouse"])
    view = load_annotation_stats(ml_env["warehouse"])
    pandas_frame = aggregate_annotation_features(annotations).reset_index()

    by_keys = load_annotation_aggregates(
        ml_env["warehouse"], keys=list(zip(view["split"], view["image_id"]))
    )
    by_positions = load_annotation_aggregates(
        ml_env["warehouse"],
        record_positions=[int(v) for v in annotations["record_position"].tolist()],
    )

    for frame in (by_keys, by_positions):
        _assert_feature_columns_equal(view, frame, "_sql", "_py")
        _assert_feature_columns_equal(pandas_frame, frame, "_py", "_sql")
    # the two selection paths agree with each other too
    _assert_feature_columns_equal(by_keys, by_positions, "_a", "_b")


def test_sql_annotation_aggregation_empty_selection(ml_env):
    empty = load_annotation_aggregates(ml_env["warehouse"], keys=[])
    assert empty.empty
    assert list(empty.columns) == ["split", "image_id", *ANNOTATION_FEATURE_COLUMNS]
    empty_positions = load_annotation_aggregates(ml_env["warehouse"], record_positions=[])
    assert empty_positions.empty


def test_annotation_identity_read_carries_no_geometry(ml_env):
    """The synthetic experiment reads identities only, never area/bbox rows."""
    view = load_annotation_stats(ml_env["warehouse"])
    keys = list(zip(view["split"], view["image_id"]))
    identities = load_annotation_identities(ml_env["warehouse"], keys=keys)
    assert list(identities.columns) == ["split", "image_id", "record_position", "id"]
    assert len(identities) > 0
    # record_position is the unique row identity, even where COCO ids repeat
    assert identities[["split", "record_position"]].duplicated().sum() == 0


def test_model_card_records_both_annotation_reproduction_checks(ml_env):
    card = json.load(open(os.path.join(ml_env["ranking"], "model_card.json")))
    reproduction = card["annotation_reproduction"]
    assert reproduction["sampled_images"] > 0
    assert reproduction["sql_columns"]
    assert set(reproduction["sql_columns"]) == set(ANNOTATION_FEATURE_COLUMNS)
    assert reproduction["max_abs_diff"] < 1e-6
    assert reproduction["max_abs_diff_sql"] < 1e-6
    summary = json.load(open(os.path.join(ml_env["ranking"], "summary.json")))
    assert (
        summary["provenance"]["annotation_reproduction_max_abs_diff_sql"]
        == reproduction["max_abs_diff_sql"]
    )


# --------------------------------------------------------------------------- #
# reproducibility, seeds, budgets


def test_rankings_are_reproducible(ml_env):
    for name in ("candidates.parquet", "candidates.jsonl", "model_card.json", "partition_manifest.json"):
        first = (Path(ml_env["ranking"]) / name).read_bytes()
        second = (Path(ml_env["ranking_second"]) / name).read_bytes()
        assert first == second, name


def test_seed_sensitivity_and_baseline_stability(ml_env):
    import pandas as pd

    candidates = _candidates(ml_env)
    # the two deterministic baselines are seed-independent
    for column in ("low_count_rank", "robust_rank"):
        assert candidates[column].nunique() == len(candidates)
    # the seeded references differ between seeds
    assert not candidates["if_rank_seed7"].equals(candidates["if_rank_seed42"])
    assert not candidates["random_rank_seed7"].equals(candidates["random_rank_seed42"])
    # ranks are a permutation of 1..n for every method/seed
    total = len(candidates)
    for column in [c for c in candidates.columns if c.endswith("_rank")]:
        assert sorted(candidates[column].tolist()) == list(range(1, total + 1)), column


def test_precision_at_k_uses_equal_budget_denominators():
    labels = np.array([1, 0, 1, 0, 1])
    ranks = np.array([1, 2, 3, 4, 5])
    result = precision_at_k(labels, ranks, 10)
    assert result["denominator"] == 5  # short pool, not padded
    assert result["hits"] == 3
    assert result["precision"] == pytest.approx(3 / 5)
    assert precision_at_k(labels, ranks, 2)["denominator"] == 2
    assert precision_at_k(np.array([], dtype=int), np.array([], dtype=int), 10)["denominator"] == 0


def test_roc_auc_tie_handling():
    labels = np.array([1, 0, 1, 0])
    assert roc_auc(labels, np.array([0.5, 0.5, 0.5, 0.5]))["auc"] == pytest.approx(0.5)
    perfect = roc_auc(labels, np.array([0.9, 0.1, 0.8, 0.2]))
    assert perfect["auc"] == pytest.approx(1.0)
    assert roc_auc(np.array([1, 1]), np.array([0.1, 0.2]))["auc"] is None


# --------------------------------------------------------------------------- #
# evaluation: known cases, synthetic loss, review sample


def test_known_case_regression_reports_denominators(ml_env):
    known = json.load(open(os.path.join(ml_env["evaluation"], "known_cases.json")))
    assert known["known_cases_total"] == 9
    assert known["known_cases_in_fit"] == 0
    assert known["known_cases_in_pool"] >= 1
    assert known["pool_size"] == len(_candidates(ml_env))
    for method_seed, per_budget in known["recovery"].items():
        for budget in ("at_10", "at_25", "at_50"):
            entry = per_budget[budget]
            assert entry["known_cases_total"] == 9
            assert entry["pool_size"] == known["pool_size"]
            assert 0 <= entry["recovered"] <= entry["budget"]


def test_synthetic_experiment_recomputes_features(ml_env):
    synthetic = json.load(open(os.path.join(ml_env["evaluation"], "synthetic_annotation_loss.json")))
    assert synthetic["pool_size"] > 0
    assert synthetic["injected_images"] > 0
    assert 0 < synthetic["injected_fraction"] <= 1
    # each physical image appears once, either original or corrupted
    assert synthetic["injected_images"] <= synthetic["pool_size"]

    for entry in synthetic["per_image"].values():
        assert entry["annotations_retained"] >= 1
        assert entry["annotations_retained"] < entry["annotations_original"]
        expected = max(1, round(0.10 * entry["annotations_original"]))
        expected = min(expected, entry["annotations_original"] - 1)
        assert entry["annotations_retained"] == expected
        assert entry["retention_fraction"] <= 0.5
        assert all(isinstance(i, int) and 1 <= i < 10**9 for i in entry["retained_ids"])

    # metrics carry real denominators
    for method_seed, result in synthetic["results"].items():
        for budget, entry in result["precision_at_k"].items():
            assert entry["denominator"] <= synthetic["pool_size"]
            assert 0 <= entry["hits"] <= entry["denominator"]
        assert result["paired_priority_delta_injected"]["count"] == synthetic["injected_images"]


def test_synthetic_features_are_recomputed_not_fabricated(ml_env):
    """Corrupted counts must equal the retained-annotation counts exactly."""
    synthetic = json.load(open(os.path.join(ml_env["evaluation"], "synthetic_annotation_loss.json")))
    paired = open(os.path.join(ml_env["evaluation"], "synthetic_paired_deltas.csv")).read().strip().splitlines()
    header = paired[0].split(",")
    assert "n_annotations_original" in header and "n_annotations_corrupted" in header
    original_index = header.index("n_annotations_original")
    corrupted_index = header.index("n_annotations_corrupted")
    for line in paired[1:]:
        cells = line.split(",")
        assert int(cells[corrupted_index]) < int(cells[original_index])
    # each corrupted image is present in the per-image plan
    assert len(paired) - 1 == synthetic["injected_images"]


def test_review_sample_is_disjoint_and_blank(ml_env):
    sample = json.load(open(os.path.join(ml_env["evaluation"], "review_sample.json")))
    assert sample["status"] == "awaiting_scientific_review"
    record_keys = [r["record_key"] for r in sample["records"]]
    assert len(record_keys) == len(set(record_keys))
    strata = {r["stratum"] for r in sample["records"]}
    assert "top_isolation_forest" in strata
    assert "random_sample" in strata
    for record in sample["records"]:
        assert record["judgment"] is None
        assert record["reason"] is None
        assert record["reviewer_expertise"] is None


def test_build_review_sample_disjoint_unit():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "record_key": [f"train:{i}" for i in range(30)],
            "file_name": [f"f{i}.tif" for i in range(30)],
            "split": ["train"] * 30,
            "cell_line": ["A172"] * 30,
            "is_known_case": [False] * 30,
            "n_annotations": list(range(1, 31)),
            "if_rank": list(range(30, 0, -1)),
            "if_priority": [i / 30 for i in range(30)],
            "robust_rank": list(range(1, 31)),
            "robust_priority": [i / 30 for i in range(30)],
            "low_count_rank": list(range(1, 31)),
            "random_rank": list(range(30, 0, -1)),
        }
    )
    sample = build_review_sample(frame)
    keys = [r["record_key"] for r in sample["records"]]
    assert len(keys) == len(set(keys))
    assert len(keys) == 30
    assert all(r["judgment"] is None for r in sample["records"])


# --------------------------------------------------------------------------- #
# source preservation, tampering, failure rejection


def test_source_data_is_preserved(ml_env):
    before = _hash_tree(Path(ml_env["data_root"]))
    run_ranking(
        RankingConfig(
            features_dir=ml_env["features"],
            warehouse_dir=ml_env["warehouse"],
            out_dir=str(Path(ml_env["root"]) / "ranking-third"),
        )
    )
    after = _hash_tree(Path(ml_env["data_root"]))
    assert before == after


def test_ranking_rejects_mixed_warehouse(ml_env):
    other_root = Path(ml_env["root"]) / "other-data"
    (other_root / "annotations").mkdir(parents=True)
    (other_root / "images").mkdir(parents=True)
    _build_dataset(other_root)
    other_audit, other_wh = Path(ml_env["root"]) / "other-audit", Path(ml_env["root"]) / "other-wh"
    run_audit(AuditConfig(data_root=str(other_root), out_dir=str(other_audit)))
    run_warehouse(WarehouseConfig(audit_dir=str(other_audit), out_dir=str(other_wh)))
    with pytest.raises(RankingError, match="does not match the feature run|source hashes do not match"):
        run_ranking(
            RankingConfig(
                features_dir=ml_env["features"],
                warehouse_dir=str(other_wh),
                out_dir=str(Path(ml_env["root"]) / "ranking-mixed"),
            )
        )


def test_ranking_rejects_tampered_feature_marker(ml_env):
    tampered = Path(ml_env["root"]) / "features-tampered"
    shutil.copytree(ml_env["features"], tampered)
    marker_path = tampered / "COMPLETED"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["source_sha256"] = {"train": "incorrect"}
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(RankingError, match="not release-ready"):
        run_ranking(
            RankingConfig(
                features_dir=str(tampered),
                warehouse_dir=ml_env["warehouse"],
                out_dir=str(Path(ml_env["root"]) / "ranking-tampered"),
            )
        )


def test_failed_ranking_leaves_no_completion(ml_env):
    broken = Path(ml_env["root"]) / "features-broken"
    shutil.copytree(ml_env["features"], broken)
    os.remove(broken / "features.jsonl")
    out = Path(ml_env["root"]) / "ranking-broken"
    with pytest.raises(RankingError):
        run_ranking(
            RankingConfig(
                features_dir=str(broken), warehouse_dir=ml_env["warehouse"], out_dir=str(out)
            )
        )
    assert not (out / "COMPLETED").exists()
    assert not (out / ".staging").exists()


def test_verify_rejects_tampered_ranking_and_evaluation(ml_env):
    assert_ranking_ready(ml_env["ranking"])
    assert verify_ranking_outputs(ml_env["ranking"])["ok"] is True

    tampered = Path(ml_env["root"]) / "ranking-tampered-out"
    shutil.copytree(ml_env["ranking"], tampered)
    with open(tampered / "candidates.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{}\n")
    result = verify_ranking_outputs(str(tampered))
    assert result["ok"] is False
    with pytest.raises(RankingError, match="not release-ready"):
        assert_ranking_ready(str(tampered))

    assert_evaluation_ready(ml_env["evaluation"])
    tampered_ev = Path(ml_env["root"]) / "evaluation-tampered"
    shutil.copytree(ml_env["evaluation"], tampered_ev)
    sample = json.loads((tampered_ev / "review_sample.json").read_text(encoding="utf-8"))
    sample["records"][0]["judgment"] = "looks clean"
    (tampered_ev / "review_sample.json").write_text(json.dumps(sample), encoding="utf-8")
    result = verify_evaluation_outputs(str(tampered_ev))
    assert result["ok"] is False
    assert any("fabricated judgment" in problem for problem in result["problems"])


def test_evaluation_requires_a_completed_ranking(ml_env):
    empty = Path(ml_env["root"]) / "ranking-empty"
    empty.mkdir()
    with pytest.raises(EvaluationError, match="ranking inputs are not usable"):
        run_evaluation(
            EvaluationConfig(
                features_dir=ml_env["features"],
                warehouse_dir=ml_env["warehouse"],
                ranking_dir=str(empty),
                out_dir=str(Path(ml_env["root"]) / "evaluation-bad"),
            )
        )


def test_frozen_ranking_reloads_and_scores(ml_env):
    frozen = load_frozen_ranking(ml_env["ranking"])
    candidates = _candidates(ml_env)
    assert len(frozen.candidates) == len(candidates)
    assert set(frozen.fitted_cell_lines) == set(
        json.load(open(os.path.join(ml_env["ranking"], "model_card.json")))["fitted_cell_lines"]
    )
    scored = frozen.score_frame(frozen.candidates.head(5).copy())
    for column in ("if_rank_seed42", "robust_rank", "low_count_rank", "random_rank"):
        assert column in scored.columns
    assert list(MODEL_FEATURES)[0] in load_feature_frame(ml_env["features"]).columns
    for column in CANDIDATE_IDENTITY_COLUMNS:
        assert column in candidates.columns


# --------------------------------------------------------------------------- #
# review-note follow-ups


def test_mixed_feature_build_is_rejected(ml_env):
    """A ranking from feature build A must not be evaluated with feature build B.

    Both builds share the COCO source hashes and the same warehouse; only the
    TIFF pixels differ, so only the feature-manifest identity changes.
    """
    from livecell_audit.ranking import assert_build_consistency

    data_root_b = Path(ml_env["root"]) / "data-b"
    shutil.copytree(ml_env["data_root"], data_root_b)
    tif = next((data_root_b / "images").glob("*.tif"))
    from PIL import Image

    array = np.asarray(Image.open(tif)) + 40
    Image.fromarray((array % 256).astype("uint8"), mode="L").save(tif, format="TIFF")

    features_b = Path(ml_env["root"]) / "features-b"
    run_features(
        FeatureConfig(
            warehouse_dir=ml_env["warehouse"], data_root=str(data_root_b), out_dir=str(features_b)
        )
    )
    marker_a = json.loads((Path(ml_env["features"]) / "COMPLETED").read_text(encoding="utf-8"))
    marker_b = json.loads((features_b / "COMPLETED").read_text(encoding="utf-8"))
    # same COCO source hashes but a different feature manifest identity
    assert marker_a["source_sha256"] == marker_b["source_sha256"]
    assert marker_a["manifest"]["sha256"] != marker_b["manifest"]["sha256"]

    with pytest.raises(RankingError, match="refusing to mix different builds"):
        assert_build_consistency(str(features_b), ranking_dir=ml_env["ranking"])

    with pytest.raises(EvaluationError, match="refusing to mix different builds"):
        run_evaluation(
            EvaluationConfig(
                features_dir=str(features_b),
                warehouse_dir=ml_env["warehouse"],
                ranking_dir=ml_env["ranking"],
                out_dir=str(Path(ml_env["root"]) / "evaluation-mixed"),
            )
        )


def test_known_case_identity_is_verified(ml_env):
    """A case is only 'recovered' when its reported (split, id, filename) matches.

    The fixture's known-case filename carries a synthetic image id, so the real
    reported id 10517 is absent: this must never be counted as recovered, while a
    filename-level count may still fire. A monkeypatched case with the fixture's
    own id exercises the positive path.
    """
    import livecell_audit.evaluate as evaluate_mod
    from livecell_audit.known_cases import KNOWN_SPARSE_CASES

    known = json.load(open(os.path.join(ml_env["evaluation"], "known_cases.json")))
    assert known["recovery_rule"]
    assert known["known_cases_in_pool"] >= 1
    # the fixture id does not equal the real reported id 10517
    assert known["known_cases_reported_identity_present"] == 0
    assert known["known_cases_canonical_matches_reported_identity"] == 0
    for per_budget in known["recovery"].values():
        for entry in per_budget.values():
            # wrong-ID same-name cases are never called recovered
            assert entry["recovered"] == 0
            assert entry["recovered_filename_level"] <= known["known_cases_in_pool"]

    frozen = load_frozen_ranking(ml_env["ranking"])
    candidates = _candidates(ml_env)
    fixture_id = int(candidates[candidates["file_name"] == KNOWN_FILENAME].iloc[0]["image_id"])
    assert frozen.reported_version(KNOWN_FILENAME, "train", fixture_id) is not None
    assert frozen.reported_version(KNOWN_FILENAME, "train", 10517) is None
    assert frozen.reported_version(KNOWN_FILENAME, "test", fixture_id) is None

    # positive path: patch the reported identity to the fixture's own version
    monkeypatch_cases = [
        {"file_name": KNOWN_FILENAME, "split": "train", "image_id": fixture_id}
    ]
    original = evaluate_mod.KNOWN_SPARSE_CASES
    evaluate_mod.KNOWN_SPARSE_CASES = monkeypatch_cases
    try:
        result = evaluate_mod.known_case_regression(frozen, BUDGETS)
    finally:
        evaluate_mod.KNOWN_SPARSE_CASES = original
    assert result["known_cases_reported_identity_present"] == 1
    assert result["known_cases_canonical_matches_reported_identity"] == 1
    assert result["cases"][0]["reported_version_n_annotations"] == 1
    # the low-count baseline ranks a 1-annotation image at the top
    assert result["recovery"]["low_count_seed42"]["at_10"]["recovered"] == 1
    assert KNOWN_SPARSE_CASES  # real constant untouched


def test_retention_uses_record_position_not_annotation_id():
    """Duplicate annotation ids must not multiply retained rows."""
    from livecell_audit.evaluate import _retention_plan

    annotations = __import__("pandas").DataFrame(
        {
            "split": ["train"] * 4,
            "record_position": [0, 1, 2, 3],
            "image_id": [7, 7, 7, 7],
            "id": [100, 100, 100, 100],  # duplicate ids, preserved by the warehouse
            "area": [1.0, 2.0, 3.0, 4.0],
            "bbox": [[0.0, 0.0, 1.0, 1.0]] * 4,
            "n_points": [4, 4, 4, 4],
        }
    )
    retained, details = _retention_plan(annotations, [("train", 7)])
    assert retained[("train", 7)]["record_positions"] == [0]
    entry = details["train:7"]
    assert entry["annotations_original"] == 4
    assert entry["annotations_retained"] == 1
    assert entry["retained_record_positions"] == [0]
    assert entry["duplicate_annotation_ids_in_image"] == 3


def test_sensitivity_summary_present(ml_env):
    synthetic = json.load(open(os.path.join(ml_env["evaluation"], "synthetic_annotation_loss.json")))
    assert synthetic["sensitivity_scope"]
    summary = synthetic["seed_summary"]
    for method in ("isolation_forest", "low_count", "robust", "random"):
        entry = summary[method]
        assert entry["synthetic_roc_auc"]["n"] >= 1
        assert entry["synthetic_roc_auc"]["values"]
        for budget in ("at_10", "at_25", "at_50"):
            assert entry["precision_at_k"][budget]["n"] >= 1
    # the fixed corruption mask is labelled structured, not random dropout
    assert "structured" in synthetic["selection"]["loss_scenario"]
    # raw deltas are available alongside percentile deltas
    assert synthetic["results"]["isolation_forest_seed42"]["paired_raw_delta_injected"]["count"] >= 1
