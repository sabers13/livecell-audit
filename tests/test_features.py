"""Focused tests for the pandas feature stage (milestone 3).

The dataset is hand-computable where it matters: a 2x2 image whose pixel
statistics are exact, two annotations with known areas/bboxes/polygon sizes,
plus zero-annotation, missing-file, unreadable-file and alternate-version
records. Ambiguity and orphan cases use a separate small dataset.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from livecell_audit.audit import AuditConfig, run_audit
from livecell_audit.errors import FeatureError
from livecell_audit.features import FeatureConfig, run_features
from livecell_audit.warehouse import WarehouseConfig, run_warehouse
from tests.conftest import image_record, write_coco, write_tif


def _ann(ann_id, image_id, area, bbox, n_points):
    poly = [float(v) for v in range(n_points * 2)]
    return {
        "id": ann_id,
        "image_id": image_id,
        "category_id": 1,
        "segmentation": [poly],
        "area": area,
        "bbox": bbox,
        "iscrowd": 0,
    }


def _write_tif_array(path, array):
    from PIL import Image

    Image.fromarray(np.asarray(array, dtype=np.uint8), mode="L").save(
        str(path), format="TIFF"
    )


def _pipeline(data_root, tmp_path, name="run"):
    audit_out = tmp_path / f"{name}-audit"
    wh_out = tmp_path / f"{name}-wh"
    fe_out = tmp_path / f"{name}-fe"
    run_audit(AuditConfig(data_root=str(data_root), out_dir=str(audit_out)))
    run_warehouse(WarehouseConfig(audit_dir=str(audit_out), out_dir=str(wh_out)))
    return audit_out, wh_out, fe_out


def _rows(out_dir):
    rows = {}
    with open(Path(out_dir) / "features.jsonl", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            rows[record["record_key"]] = record
    return rows


# --------------------------------------------------------------------------- #
# the hand-computable dataset


@pytest.fixture
def feature_dataset(tmp_path):
    root = tmp_path / "data"
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)

    a = "A172_Phase_A7_1_00d00h00m_1.tif"          # 2x2, known pixels
    b = "BV2_Phase_C4_1_01d16h00m_3.tif"           # zero annotations
    missing = "MCF7_Phase_B3_1_00d12h00m_9.tif"    # referenced, absent
    unreadable = "Huh7_Phase_A10_2_00d00h00m_3.tif"  # present, not a TIFF
    dup = "SKOV3_Phase_G4_1_00d20h00m_3.tif"       # two annotation versions

    images = [
        image_record(1, a, width=2, height=2),
        image_record(2, b),
        image_record(3, missing),
        image_record(4, unreadable),
        image_record(5, dup),
        image_record(6, dup),
    ]
    annotations = [
        _ann(101, 1, area=10.0, bbox=[0.0, 0.0, 4.0, 4.0], n_points=4),
        _ann(102, 1, area=20.0, bbox=[1.0, 1.0, 2.0, 6.0], n_points=6),
        _ann(301, 3, area=5.0, bbox=[0.0, 0.0, 3.0, 3.0], n_points=4),
        _ann(401, 4, area=5.0, bbox=[0.0, 0.0, 3.0, 3.0], n_points=4),
        _ann(501, 5, area=7.0, bbox=[0.0, 0.0, 3.0, 3.0], n_points=4),
        _ann(601, 6, area=9.0, bbox=[0.0, 0.0, 3.0, 3.0], n_points=4),
    ]
    write_coco(root / "annotations" / "livecell_coco_train.json", images, annotations)
    write_coco(root / "annotations" / "livecell_coco_val.json", [], [])
    write_coco(root / "annotations" / "livecell_coco_test.json", [], [])

    _write_tif_array(root / "images" / a, [[0, 2], [4, 6]])
    write_tif(root / "images" / b)
    (root / "images" / unreadable).write_bytes(b"not a real tiff")
    write_tif(root / "images" / dup)
    return {"data_root": str(root), "a": a, "missing": missing, "unreadable": unreadable, "dup": dup}


def test_feature_formulas_hand_computable(feature_dataset, tmp_path):
    _audit, _wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(_wh), out_dir=str(fe_out)))
    row = _rows(fe_out)["train:0"]

    # image-only statistics: [[0,2],[4,6]] -> mean 3, std sqrt(5),
    # gradient magnitude mean = sqrt(2^2 + 4^2) = sqrt(20)
    assert row["pixel_mean"] == pytest.approx(3.0)
    assert row["pixel_std"] == pytest.approx(math.sqrt(5.0))
    assert row["gradient_mean"] == pytest.approx(math.sqrt(20.0))
    assert row["pixel_min"] == 0.0 and row["pixel_max"] == 6.0
    assert row["image_features_status"] == "ok"
    assert row["image_features_available"] is True

    # annotation aggregates computed in SQL
    assert row["n_annotations"] == 2
    assert row["area_mean"] == pytest.approx(15.0)
    assert row["area_median"] == pytest.approx(15.0)
    assert row["area_min"] == 10.0 and row["area_max"] == 20.0
    assert row["bbox_width_mean"] == pytest.approx(3.0)   # (4 + 2) / 2
    assert row["bbox_height_mean"] == pytest.approx(5.0)  # (4 + 6) / 2
    assert row["n_points_mean"] == pytest.approx(5.0)
    assert row["n_points_sum"] == 10
    assert row["annotation_join_status"] == "ok"
    assert row["has_annotations"] is True and row["zero_annotation"] is False


def test_zero_annotations_are_measured_not_missing(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    row = _rows(fe_out)["train:1"]

    # zero annotations is a real measurement, not a fabricated or missing value
    assert row["n_annotations"] == 0
    assert row["zero_annotation"] is True
    assert row["has_annotations"] is False
    assert row["annotation_join_status"] == "ok"
    # undefined summaries stay null rather than becoming 0
    for name in ("area_mean", "area_median", "bbox_width_mean", "n_points_mean"):
        assert row[name] is None, name


def test_missing_and_unreadable_tiff_are_nulls_with_flags(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    rows = _rows(fe_out)

    missing = rows["train:2"]
    assert missing["image_features_status"] == "missing_file"
    assert missing["image_features_available"] is False
    assert missing["has_image_file"] is False
    assert missing["pixel_mean"] is None and missing["pixel_std"] is None
    # annotation features are still available for the missing image
    assert missing["n_annotations"] == 1

    unreadable = rows["train:3"]
    assert unreadable["image_features_status"] == "unreadable"
    assert unreadable["has_image_file"] is True
    assert unreadable["pixel_mean"] is None and unreadable["gradient_mean"] is None


def test_alternate_versions_reuse_image_features(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    rows = _rows(fe_out)
    first, second = rows["train:4"], rows["train:5"]

    assert first["file_name"] == second["file_name"]
    assert first["versions_in_split"] == 2
    assert first["is_alternate_version"] is True and second["is_alternate_version"] is True
    # image-only statistics are read once per filename and reused
    assert first["pixel_mean"] == second["pixel_mean"]
    assert first["pixel_std"] == second["pixel_std"]
    # the versions keep their own annotation evidence
    assert first["n_annotations"] == 1 and second["n_annotations"] == 1
    assert first["area_mean"] == 7.0 and second["area_mean"] == 9.0


# --------------------------------------------------------------------------- #
# ambiguity and orphans


def _ambiguous_dataset(root):
    root = Path(root)
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    a = "A172_Phase_A7_1_00d00h00m_1.tif"
    b = "A172_Phase_A7_1_00d00h00m_2.tif"
    images = [image_record(1, a), image_record(1, b)]
    annotations = [_ann(101, 1, 10.0, [0, 0, 4, 4], 4), _ann(102, 999, 10.0, [0, 0, 4, 4], 4)]
    write_coco(root / "annotations" / "livecell_coco_train.json", images, annotations)
    write_coco(root / "annotations" / "livecell_coco_val.json", [], [])
    write_coco(root / "annotations" / "livecell_coco_test.json", [], [])
    write_tif(root / "images" / a)
    write_tif(root / "images" / b)
    return str(root)


def test_ambiguous_join_is_quarantined(tmp_path):
    data = _ambiguous_dataset(tmp_path / "data")
    _audit, wh, fe_out = _pipeline(data, tmp_path)
    result = run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    rows = _rows(fe_out)

    assert result["diagnostics"]["ambiguous_records"] == 2
    for key in ("train:0", "train:1"):
        row = rows[key]
        assert row["annotation_join_status"] == "ambiguous_image_id"
        # never guessed and never multiplied: counts are null, not 1 or 2
        assert row["n_annotations"] is None
        assert row["has_annotations"] is None
        assert row["zero_annotation"] is None
        assert row["area_mean"] is None
    # image-only features are unaffected by the ambiguous annotation join
    assert rows["train:0"]["image_features_status"] == "ok"


def test_orphan_annotations_reported_not_attached(tmp_path):
    data = _ambiguous_dataset(tmp_path / "data")
    _audit, wh, fe_out = _pipeline(data, tmp_path)
    result = run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    diagnostics = result["diagnostics"]

    assert diagnostics["orphan_annotation_records"] == 1
    assert diagnostics["orphan_annotation_groups"] == 1
    # the attached total excludes the orphan
    assert diagnostics["annotation_total_attached"] == 0
    assert diagnostics["annotation_total_orphan"] == 1


# --------------------------------------------------------------------------- #
# dictionary, determinism, safety


def test_feature_dictionary_matches_columns(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    dictionary = json.loads((fe_out / "feature_dictionary.json").read_text(encoding="utf-8"))
    names = [entry["name"] for entry in dictionary["features"]]

    import duckdb

    con = duckdb.connect()
    columns = [
        row[0]
        for row in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{fe_out / 'features.parquet'}')"
        ).fetchall()
    ]
    con.close()
    assert columns == names
    by_name = {entry["name"]: entry for entry in dictionary["features"]}
    assert by_name["pixel_std"]["depends_on_annotations"] is False
    assert by_name["n_annotations"]["depends_on_annotations"] is True
    assert all("formula" in entry and "missing_behavior" in entry for entry in dictionary["features"])
    assert dictionary["feature_schema_version"] == "1"
    assert dictionary["source_manifest"]["source_sha256"]


def test_features_deterministic(feature_dataset, tmp_path):
    _audit, wh, fe_a = _pipeline(feature_dataset["data_root"], tmp_path, name="a")
    _audit2, wh2, fe_b = _pipeline(feature_dataset["data_root"], tmp_path, name="b")
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_a)))
    run_features(FeatureConfig(warehouse_dir=str(wh2), out_dir=str(fe_b)))

    for name in (
        "features.jsonl",
        "features.parquet",
        "cell_line_summary.json",
        "cell_line_summary.csv",
        "feature_dictionary.json",
        "feature_distributions.json",
    ):
        with open(fe_a / name, "rb") as handle_a, open(fe_b / name, "rb") as handle_b:
            assert handle_a.read() == handle_b.read(), name


def test_features_requires_warehouse_marker(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    os.remove(wh / "COMPLETED")
    with pytest.raises(FeatureError, match="not a completed warehouse run"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))


def test_failed_features_rerun_clears_marker(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    assert (fe_out / "COMPLETED").is_file()
    good = (fe_out / "features.jsonl").read_bytes()

    # Remove the warehouse database so the rerun fails after invalidating.
    os.remove(wh / "audit.duckdb")
    with pytest.raises(FeatureError, match="warehouse manifest verification failed"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))

    assert not (fe_out / "COMPLETED").exists()
    assert not (fe_out / ".staging").exists()
    assert (fe_out / "features.jsonl").read_bytes() == good


def test_features_derive_data_root_from_warehouse(feature_dataset, tmp_path):
    """With no --data-root, the raw root is recovered from the sources table."""
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    rows = _rows(fe_out)
    assert rows["train:0"]["image_features_status"] == "ok"
    assert rows["train:0"]["pixel_mean"] == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# provenance: marker/manifest/DB verification (review item 1)


def test_features_rejects_tampered_marker_source_hashes(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    marker = json.loads((wh / "COMPLETED").read_text(encoding="utf-8"))
    marker["source_sha256"] = {"train": "wrong-source"}
    (wh / "COMPLETED").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(FeatureError, match="provenance verification failed"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))


def test_features_rejects_modified_database(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    with open(wh / "audit.duckdb", "ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(FeatureError, match="manifest verification failed"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))


def test_features_rejects_mixed_database_and_marker(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    # a second warehouse built from different source data
    other_root = _ambiguous_dataset(tmp_path / "other")
    _a2, wh2, _f2 = _pipeline(other_root, tmp_path, name="other")
    shutil.copyfile(wh2 / "audit.duckdb", wh / "audit.duckdb")
    with pytest.raises(FeatureError, match="manifest verification failed"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))


def test_features_rejects_marker_schema_mismatch(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    marker = json.loads((wh / "COMPLETED").read_text(encoding="utf-8"))
    marker["schema_version"] = "999"
    (wh / "COMPLETED").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(FeatureError, match="schema_version mismatch"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))


def test_features_rejects_missing_manifest(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    os.remove(wh / "manifest.json")
    with pytest.raises(FeatureError, match="manifest"):
        run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))


def test_release_ready_rejects_modified_feature_outputs(feature_dataset, tmp_path):
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    assert_release_ready(str(fe_out))  # a full run is release-ready
    assert verify_features_outputs(str(fe_out))["ok"] is True

    with open(fe_out / "features.parquet", "ab") as handle:
        handle.write(b"tampered")
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    assert any("features.parquet" in problem for problem in result["problems"])
    with pytest.raises(FeatureError, match="not release-ready"):
        assert_release_ready(str(fe_out))


# --------------------------------------------------------------------------- #
# partial mode (review item 1)

def test_partial_mode_marks_not_attempted_and_is_not_release_ready(feature_dataset, tmp_path):
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    result = run_features(
        FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out), max_filenames=1)
    )
    assert result["run"]["partial"] is True
    assert result["run"]["mode"] == "partial"
    marker = json.loads((fe_out / "COMPLETED").read_text(encoding="utf-8"))
    assert marker["partial"] is True and marker["mode"] == "partial"

    rows = _rows(fe_out)
    assert rows["train:0"]["image_features_status"] == "ok"
    # every unread record has an explicit status, never a silent null
    for key in ("train:1", "train:2", "train:3", "train:4", "train:5"):
        assert rows[key]["image_features_status"] == "not_attempted"
        assert rows[key]["image_features_available"] is False

    assert verify_features_outputs(str(fe_out))["ok"] is False
    with pytest.raises(FeatureError, match="partial"):
        assert_release_ready(str(fe_out))


# --------------------------------------------------------------------------- #
# item 4: intensity contract and apostrophe-safe paths


def test_feature_dictionary_states_intensity_contract(feature_dataset, tmp_path):
    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    dictionary = json.loads((fe_out / "feature_dictionary.json").read_text(encoding="utf-8"))
    contract = dictionary["image_statistics"]
    assert "none" in contract["normalization"]
    assert "8-bit" in contract["units"] or "0-255" in contract["units"]
    assert "not rescaled" in contract["bit_depth"]
    by_name = {entry["name"]: entry for entry in dictionary["features"]}
    assert "no normalization" in by_name["pixel_mean"]["description"]


def test_apostrophe_in_output_paths_is_supported(feature_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    run_audit(AuditConfig(data_root=feature_dataset["data_root"], out_dir=str(audit_out)))
    wh = tmp_path / "wh'out"
    fe_out = tmp_path / "fe'out"
    run_warehouse(WarehouseConfig(audit_dir=str(audit_out), out_dir=str(wh)))
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    assert (fe_out / "features.parquet").is_file()
    assert (fe_out / "manifest.json").is_file()
    assert _rows(fe_out)["train:0"]["image_features_status"] == "ok"


# --------------------------------------------------------------------------- #
# input-validator prerequisite: reconcile a marker against its protected outputs


def _edit_marker(fe_out, **fields):
    marker_path = Path(fe_out) / "COMPLETED"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.update(fields)
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8")


def test_release_ready_rejects_flipped_partial_marker(feature_dataset, tmp_path):
    """A partial run whose marker alone claims full/other hashes is rejected.

    This is the supervisor probe: the protected manifest, dictionary, run
    diagnostics and the real feature table all still describe the partial run.
    """
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out), max_filenames=1))

    _edit_marker(
        fe_out,
        partial=False,
        mode="full",
        source_sha256={"train": "incorrect"},
    )
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    joined = " | ".join(result["problems"])
    assert "manifest mode" in joined
    assert "manifest partial" in joined
    assert "source_sha256" in joined
    with pytest.raises(FeatureError, match="not release-ready"):
        assert_release_ready(str(fe_out))


def test_release_ready_rejects_marker_source_hash_mismatch(feature_dataset, tmp_path):
    """A full run with only the marker's source hash altered is rejected."""
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    good = json.loads((Path(fe_out) / "COMPLETED").read_text(encoding="utf-8"))["source_sha256"]
    _edit_marker(fe_out, source_sha256={**good, "train": "deadbeef"})
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    assert any("source_sha256" in problem for problem in result["problems"])
    with pytest.raises(FeatureError, match="not release-ready"):
        assert_release_ready(str(fe_out))


def test_release_ready_rejects_marker_schema_and_count_mismatch(feature_dataset, tmp_path):
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))

    _edit_marker(fe_out, feature_schema_version="999")
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    assert any("feature_schema_version" in problem for problem in result["problems"])

    _edit_marker(fe_out, feature_records=9999)
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    joined = " | ".join(result["problems"])
    assert "features.jsonl rows" in joined
    assert "diagnostics" in joined
    with pytest.raises(FeatureError, match="not release-ready"):
        assert_release_ready(str(fe_out))


def test_release_ready_rejects_missing_required_outputs(feature_dataset, tmp_path):
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))

    os.remove(fe_out / "features.jsonl")
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    assert any("features.jsonl" in problem for problem in result["problems"])

    # A required output absent from the protected manifest is also rejected.
    os.remove(fe_out / "run.json")
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    assert any("run.json" in problem for problem in result["problems"])
    with pytest.raises(FeatureError, match="not release-ready"):
        assert_release_ready(str(fe_out))


def test_release_ready_rejects_tampered_feature_table(feature_dataset, tmp_path):
    from livecell_audit.features import assert_release_ready, verify_features_outputs

    _audit, wh, fe_out = _pipeline(feature_dataset["data_root"], tmp_path)
    run_features(FeatureConfig(warehouse_dir=str(wh), out_dir=str(fe_out)))
    with open(fe_out / "features.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{}\n")
    result = verify_features_outputs(str(fe_out))
    assert result["ok"] is False
    joined = " | ".join(result["problems"])
    assert "features.jsonl" in joined
    with pytest.raises(FeatureError, match="not release-ready"):
        assert_release_ready(str(fe_out))
