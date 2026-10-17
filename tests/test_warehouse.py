"""Focused tests for the DuckDB warehouse (milestone 2).

These verify the delivered contract rather than mirroring implementation
branches: completion-marker enforcement, count reconciliation, join
cardinality / ambiguity / orphans, alternate-version preservation, typed
Parquet determinism, and the failed-build safety contract.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from livecell_audit.audit import AuditConfig, run_audit
from livecell_audit.errors import WarehouseError
from livecell_audit.warehouse import WarehouseConfig, run_warehouse
from tests.conftest import annotation_record, image_record, write_coco, write_tif


def _audit(data_root, out, **overrides):
    kwargs = {"data_root": str(data_root), "out_dir": str(out)}
    kwargs.update(overrides)
    return run_audit(AuditConfig(**kwargs))


def _warehouse(audit_dir, out, **overrides):
    kwargs = {"audit_dir": str(audit_dir), "out_dir": str(out)}
    kwargs.update(overrides)
    return run_warehouse(WarehouseConfig(**kwargs))


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _connect(warehouse_dir):
    import duckdb

    return duckdb.connect(os.path.join(str(warehouse_dir), "audit.duckdb"), read_only=True)


# --------------------------------------------------------------------------- #
# completion-marker enforcement


def test_warehouse_requires_completion_marker(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    os.remove(audit_out / "COMPLETED")
    with pytest.raises(WarehouseError, match="not a completed audit run"):
        _warehouse(audit_out, tmp_path / "wh")


def test_warehouse_rejects_unparsable_marker(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    (audit_out / "COMPLETED").write_text("{not json", encoding="utf-8")
    with pytest.raises(WarehouseError, match="not a completed audit run"):
        _warehouse(audit_out, tmp_path / "wh")


# --------------------------------------------------------------------------- #
# reconciliation


def test_warehouse_reconciles_mini_dataset(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    report = _audit(mini_dataset["data_root"], audit_out)
    result = _warehouse(audit_out, tmp_path / "wh")
    integrity = result["integrity"]

    assert integrity["reconciliation"]["images"]["loaded"] == report["total_image_records"]
    assert integrity["reconciliation"]["annotations"]["loaded"] == report["total_annotations"]
    assert integrity["reconciliation"]["unique_filenames"]["loaded"] == report["unique_filenames"]
    assert integrity["reconciliation"]["images"]["delta"] == 0
    assert integrity["reconciliation"]["annotations"]["delta"] == 0
    assert integrity["validation"]["checks"][0]["status"] == "pass"

    con = _connect(tmp_path / "wh")
    try:
        assert con.execute("SELECT COUNT(*) FROM lc.images").fetchone()[0] == report[
            "total_image_records"
        ]
        assert con.execute("SELECT COUNT(*) FROM lc.annotations").fetchone()[0] == report[
            "total_annotations"
        ]
        # a finding can be traced back to its image record
        lookup = con.execute(
            "SELECT image_record_key, annotation_count FROM lc.v1_finding_lookup "
            "WHERE finding_type = 'low_annotation_count' ORDER BY image_record_key"
        ).fetchall()
        assert lookup
        assert all(key is not None for key, _count in lookup)
    finally:
        con.close()


def test_warehouse_rejects_count_mismatch(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    marker = _load(audit_out / "COMPLETED")
    marker["total_annotations"] = marker["total_annotations"] + 1
    (audit_out / "COMPLETED").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(WarehouseError, match="annotation count mismatch"):
        _warehouse(audit_out, tmp_path / "wh")


def test_warehouse_rejects_marker_sources_checksum_mismatch(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    marker = _load(audit_out / "COMPLETED")
    marker["source_sha256"]["train"] = "0" * 64
    (audit_out / "COMPLETED").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(WarehouseError, match="source checksum mismatch"):
        _warehouse(audit_out, tmp_path / "wh")


def test_warehouse_rehash_detects_changed_source(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    # Change the raw COCO file after the audit: the re-hash must notice.
    coco = Path(mini_dataset["data_root"]) / "annotations" / "livecell_coco_val.json"
    payload = json.loads(coco.read_text(encoding="utf-8"))
    payload["images"] = []
    payload["annotations"] = []
    coco.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WarehouseError, match="raw COCO file changed"):
        _warehouse(
            audit_out, tmp_path / "wh", data_root=mini_dataset["data_root"]
        )


# --------------------------------------------------------------------------- #
# join cardinality: ambiguity, orphans, alternate versions


def _ambiguity_dataset(root):
    root = Path(root)
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    a = "A172_Phase_A7_1_00d00h00m_1.tif"
    b = "A172_Phase_A7_1_00d00h00m_2.tif"
    # two records share id 1 in the same split -> (train, 1) is ambiguous
    images = [image_record(1, a), image_record(1, b)]
    # annotation 101 points at the ambiguous id; 102 is an orphan (image 999)
    annotations = [annotation_record(101, 1), annotation_record(102, 999)]
    write_coco(root / "annotations" / "livecell_coco_train.json", images, annotations)
    write_coco(root / "annotations" / "livecell_coco_val.json", [], [])
    write_coco(root / "annotations" / "livecell_coco_test.json", [], [])
    write_tif(root / "images" / a)
    write_tif(root / "images" / b)
    return str(root)


def test_warehouse_detects_ambiguity_and_orphans(tmp_path):
    data = _ambiguity_dataset(tmp_path / "data")
    audit_out = tmp_path / "audit"
    _audit(data, audit_out)
    result = _warehouse(audit_out, tmp_path / "wh")
    join = result["integrity"]["join_integrity"]

    assert join["ambiguous_image_id_groups"] == [
        {
            "split": "train",
            "image_id": 1,
            "record_count": 2,
            "record_positions": [0, 1],
            "file_names": [
                "A172_Phase_A7_1_00d00h00m_1.tif",
                "A172_Phase_A7_1_00d00h00m_2.tif",
            ],
        }
    ]
    assert join["orphan_annotation_groups"] == [
        {"split": "train", "image_id": 999, "annotation_count": 1}
    ]
    assert join["orphan_annotation_records"] == 1

    con = _connect(tmp_path / "wh")
    try:
        # orphans and ambiguity are queryable, never silently joined away
        assert con.execute("SELECT COUNT(*) FROM lc.v1_orphan_annotations").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM lc.v1_image_join_ambiguity").fetchone()[0] == 1
        # annotations are preserved in the raw table
        assert con.execute("SELECT COUNT(*) FROM lc.annotations").fetchone()[0] == 2
    finally:
        con.close()


def test_warehouse_alternate_versions_preserved(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    _warehouse(audit_out, tmp_path / "wh")
    con = _connect(tmp_path / "wh")
    try:
        rows = con.execute(
            "SELECT split, file_name, version_count, image_ids, annotation_counts "
            "FROM lc.v1_alternate_versions"
        ).fetchall()
        assert len(rows) == 1
        split, file_name, count, ids, counts = rows[0]
        assert split == "train"
        assert file_name == mini_dataset["dup"]
        assert count == 2
        assert ids == [1, 2]
        # both versions are retained; each keeps its own count
        assert counts == [3, 5]
        # split overlap view
        overlap = con.execute(
            "SELECT file_name, n_splits FROM lc.v1_split_overlap WHERE n_splits > 1"
        ).fetchall()
        assert overlap == [(mini_dataset["shared"], 2)]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# failed build safety + determinism


def test_failed_rerun_clears_completed_marker(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    wh = tmp_path / "wh"
    _warehouse(audit_out, wh)
    assert (wh / "COMPLETED").is_file()
    good_integrity = _load(wh / "integrity.json")

    # Corrupt the audit export so a rerun fails validation.
    with open(audit_out / "tables" / "annotations.jsonl", "a", encoding="utf-8") as handle:
        handle.write('{"split": "train", "id": 999999, "image_id": 1}\n')
    with pytest.raises(WarehouseError):
        _warehouse(audit_out, wh)

    assert not (wh / "COMPLETED").exists()
    assert not (wh / ".staging").exists()
    # previous complete data is preserved but no longer labelled complete
    assert _load(wh / "integrity.json") == good_integrity


def test_warehouse_outputs_are_deterministic(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    out_a = tmp_path / "wh-a"
    out_b = tmp_path / "wh-b"
    _warehouse(audit_out, out_a)
    _warehouse(audit_out, out_b)

    # typed Parquet exports are byte-identical
    for name in sorted(os.listdir(out_a / "parquet")):
        with open(out_a / "parquet" / name, "rb") as handle_a, open(
            out_b / "parquet" / name, "rb"
        ) as handle_b:
            assert handle_a.read() == handle_b.read(), name

    # integrity is identical apart from the variable timings block
    integrity_a = _load(out_a / "integrity.json")
    integrity_b = _load(out_b / "integrity.json")
    integrity_a.pop("timings")
    integrity_b.pop("timings")
    assert integrity_a == integrity_b


def test_warehouse_marker_lists_contract_outputs(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    _warehouse(audit_out, tmp_path / "wh")
    marker = _load(tmp_path / "wh" / "COMPLETED")
    assert marker["status"] == "completed"
    assert marker["stage"] == "warehouse"
    assert marker["schema_version"] == "1"
    for relative in ("audit.duckdb", "parquet", "integrity.json", "run.json"):
        assert os.path.exists(tmp_path / "wh" / relative), relative


def test_warehouse_skips_rehash_without_data_root(mini_dataset, tmp_path):
    audit_out = tmp_path / "audit"
    _audit(mini_dataset["data_root"], audit_out)
    result = _warehouse(audit_out, tmp_path / "wh")
    checks = {c["check"]: c for c in result["integrity"]["validation"]["checks"]}
    assert checks["source_checksums_raw_files"]["status"] == "skipped"


def test_cli_runs_warehouse_and_features(mini_dataset, tmp_path, capsys):
    from livecell_audit.cli import main

    audit_out = tmp_path / "audit"
    report = _audit(mini_dataset["data_root"], audit_out)
    wh = tmp_path / "wh"
    fe = tmp_path / "fe"

    assert main(["warehouse", "--audit-dir", str(audit_out), "--out-dir", str(wh)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["images"] == report["total_image_records"]
    assert printed["annotations"] == report["total_annotations"]

    assert main(["features", "--warehouse-dir", str(wh), "--out-dir", str(fe)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["feature_records"] == report["total_image_records"]
    assert (fe / "features.parquet").is_file()
    assert (fe / "feature_dictionary.json").is_file()


def test_cli_reports_error_on_incomplete_input(tmp_path, capsys):
    from livecell_audit.cli import main

    assert main(
        ["warehouse", "--audit-dir", str(tmp_path / "nope"), "--out-dir", str(tmp_path / "x")]
    ) == 2
    assert "error" in capsys.readouterr().err
