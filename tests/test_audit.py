"""Audit behaviour on a miniature dataset: every finding type + determinism."""

from __future__ import annotations

import hashlib
import json
import os

from livecell_audit.audit import AuditConfig, run_audit
from livecell_audit.errors import AuditError


def _run(mini_dataset, tmp_path, dirname="out", **overrides):
    out = str(tmp_path / dirname)
    kwargs = {
        "data_root": mini_dataset["data_root"],
        "out_dir": out,
        "low_count_threshold": 10,
        "check_dimensions": True,
    }
    kwargs.update(overrides)
    return out, run_audit(AuditConfig(**kwargs))


def _load(out, name):
    with open(os.path.join(out, name), encoding="utf-8") as handle:
        return json.load(handle)


def _image_rows(out):
    with open(os.path.join(out, "tables", "images.jsonl"), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def test_cross_split_overlap_and_alternate_versions(mini_dataset, tmp_path):
    out, report = _run(mini_dataset, tmp_path)
    assert report["cross_split_filename_overlap"]["train_val"]["files"] == [
        mini_dataset["shared"]
    ]
    assert report["cross_split_filename_overlap"]["train_test"]["filenames"] == 0
    assert report["cross_split_filename_overlap"]["val_test"]["filenames"] == 0
    assert report["splits"]["train"]["repeated_filenames"] == [mini_dataset["dup"]]
    assert report["splits"]["train"]["extra_filename_records"] == 1
    findings = {f["finding_id"]: f for f in _load(out, "findings.json")}
    overlap = findings[f"cross-split-overlap:{mini_dataset['shared']}"]
    assert overlap["status"] == "Confirmed overlap"
    assert overlap["records"]["train"]["image_ids"] == [3]
    assert overlap["records"]["val"]["image_ids"] == [11]
    alt = findings[f"alternate-versions:train:{mini_dataset['dup']}"]
    assert alt["status"] == "Alternative annotation versions"
    assert alt["records"]["train"]["image_ids"] == [1, 2]
    # Alternate versions are review items, not corruption verdicts.
    assert "not" in alt["evidence"]["note"]


def test_missing_file_and_unparseable_name(mini_dataset, tmp_path):
    out, report = _run(mini_dataset, tmp_path)
    assert report["missing_image_filenames"] == [mini_dataset["ghost"]]
    assert [e["file_name"] for e in report["unparseable_filenames"]] == [
        mini_dataset["bad"]
    ]
    rows = _image_rows(out)
    dup_rows = [r for r in rows if r["file_name"] == mini_dataset["dup"]]
    assert {r["id"] for r in dup_rows} == {1, 2}
    assert all(r["cell_line"] == "A172" and r["well"] == "A7" for r in dup_rows)
    bad_rows = [r for r in rows if r["file_name"] == mini_dataset["bad"]]
    assert bad_rows and bad_rows[0]["cell_line"] is None
    ghost_rows = [r for r in rows if r["file_name"] == mini_dataset["ghost"]]
    assert ghost_rows and not ghost_rows[0]["local_exists"]


def test_low_count_baseline_and_threshold(mini_dataset, tmp_path):
    _out, report = _run(mini_dataset, tmp_path, low_count_threshold=2)
    flagged = {
        (e["file_name"], e["image_id"]): e["count"]
        for e in report["low_annotation_records"]
    }
    # Every 1-annotation record is flagged at threshold 2 ...
    assert flagged[("BV2_Phase_C4_1_01d16h00m_3.tif", 4)] == 1
    assert flagged[("Huh7_Phase_A10_2_00d00h00m_3.tif", 3)] == 1
    # ... while records above the threshold are not.
    assert ("A172_Phase_A7_1_00d00h00m_1.tif", 1) not in flagged  # 3 annotations
    assert ("A172_Phase_A7_1_00d00h00m_1.tif", 2) not in flagged  # 5 annotations
    assert report["low_count_threshold"] == 2


def test_derived_val_membership_excludes_train_shared(mini_dataset, tmp_path):
    out, _report = _run(mini_dataset, tmp_path)
    manifest = _load(out, "manifests/val_membership.json")
    by_id = {m["image_id"]: m for m in manifest["membership"]}
    assert by_id[11]["decision"] == "excluded"  # shared.tif is in train
    assert "train" in by_id[11]["reason"]
    assert by_id[12]["decision"] == "included"
    assert manifest["excluded_records"] == 1
    assert manifest["included_records"] == 1
    assert "not an official correction" in manifest["policy"]


def test_dimension_mismatch_flagged(tmp_path):
    from tests.conftest import annotation_record, image_record, write_coco, write_tif

    root = tmp_path / "dim"
    (root / "annotations").mkdir(parents=True)
    img_dir = root / "images" / "livecell_train_val_images"
    img_dir.mkdir(parents=True)
    name = "A172_Phase_A7_1_00d00h00m_1.tif"
    write_tif(str(img_dir / name), size=(16, 12))
    write_coco(
        str(root / "annotations" / "livecell_coco_train.json"),
        [image_record(1, name, width=704, height=520)],
        [annotation_record(101, 1)],
    )
    write_coco(str(root / "annotations" / "livecell_coco_val.json"), [], [])
    write_coco(str(root / "annotations" / "livecell_coco_test.json"), [], [])
    out = str(tmp_path / "dim-out")
    report = run_audit(AuditConfig(data_root=str(root), out_dir=out))
    assert len(report["dimension_mismatches"]) == 1
    entry = report["dimension_mismatches"][0]
    assert entry["record_width"] == 704 and entry["local_width"] == 16


def test_malformed_split_aborts_with_location(tmp_path):
    from tests.conftest import write_coco

    root = tmp_path / "bad"
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    write_coco(str(root / "annotations" / "livecell_coco_train.json"), [], [])
    broken = root / "annotations" / "livecell_coco_val.json"
    broken.write_text('{"images": [{"id": 1}], "annotations": []}', encoding="utf-8")
    write_coco(str(root / "annotations" / "livecell_coco_test.json"), [], [])
    try:
        run_audit(AuditConfig(data_root=str(root), out_dir=str(tmp_path / "bad-out")))
    except AuditError as exc:
        assert "livecell_coco_val.json" in str(exc)
        assert "images[0]" in str(exc)
    else:
        raise AssertionError("expected AuditError")


def test_missing_coco_file_aborts(tmp_path):
    root = tmp_path / "empty"
    (root / "images").mkdir(parents=True)
    try:
        run_audit(AuditConfig(data_root=str(root), out_dir=str(tmp_path / "empty-out")))
    except AuditError as exc:
        assert "livecell_coco_train.json" in str(exc)
    else:
        raise AssertionError("expected AuditError")


def _hash_tree(out):
    digests = {}
    for dirpath, _dirnames, filenames in os.walk(out):
        for name in filenames:
            if name in ("run.json", "report.json"):
                continue  # run metadata (paths, timings) differs between runs
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                digests[os.path.relpath(path, out)] = hashlib.sha256(handle.read()).hexdigest()
    return digests


def _report_without_run(out):
    payload = _load(out, "report.json")
    payload.pop("run", None)
    return json.dumps(payload, sort_keys=True)


def test_stable_reruns_are_byte_identical(mini_dataset, tmp_path):
    out_a, _ = _run(mini_dataset, tmp_path, dirname="out-a")
    out_b, _ = _run(mini_dataset, tmp_path, dirname="out-b")
    assert _hash_tree(out_a) == _hash_tree(out_b)
    # report.json is byte-identical apart from its embedded run metadata,
    # which records the distinct output directories and timings.
    assert _report_without_run(out_a) == _report_without_run(out_b)
    run_info = _load(out_a, "run.json")
    assert run_info["runtime_seconds"] >= 0
    assert run_info["peak_memory_method"].startswith("resource.getrusage")
