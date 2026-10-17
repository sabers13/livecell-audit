"""Full-dataset integration check: reproduce the snapshot observations.

This test reads the real source data read-only (it never writes into the
source tree) and is skipped when the data root is absent. It asserts the
observations from the brief as *checks on derived output*, never as
implementation constants: the audit computes every number from the raw files.

Run directly (outside the lightweight fixture suite) with e.g.::

    LIVECELL_DATA_ROOT=/home/saber/projects/livecell-audit/data/raw \\
        PYTHONPATH=src python3 -m pytest tests/test_integration.py -q
"""

from __future__ import annotations

import os

import pytest

DATA_ROOT = os.environ.get(
    "LIVECELL_DATA_ROOT", "/home/saber/projects/livecell-audit/data/raw"
)

pytestmark = pytest.mark.skipif(
    not os.path.isfile(os.path.join(DATA_ROOT, "annotations", "livecell_coco_train.json")),
    reason=f"real dataset not present at {DATA_ROOT}",
)


def test_real_dataset_snapshot(tmp_path):
    from livecell_audit.audit import AuditConfig, run_audit

    out = str(tmp_path / "integration-out")
    report = run_audit(AuditConfig(data_root=DATA_ROOT, out_dir=out))

    expected_splits = {
        "train": {"image_records": 3253, "unique_filenames": 3188,
                  "extra_filename_records": 65, "annotations": 1018576},
        "val": {"image_records": 570, "unique_filenames": 569,
                "extra_filename_records": 1, "annotations": 181610},
        "test": {"image_records": 1564, "unique_filenames": 1512,
                 "extra_filename_records": 52, "annotations": 462261},
    }
    for split, expected in expected_splits.items():
        for key, value in expected.items():
            assert report["splits"][split][key] == value, (split, key)

    assert report["total_image_records"] == 5387
    assert report["unique_filenames"] == 5239
    assert report["local_image_files"] == 5239
    assert report["total_annotations"] == 1662447
    assert report["missing_image_filenames"] == []
    assert report["orphan_local_files"] == []
    assert report["dimension_mismatches"] == []

    overlap = report["cross_split_filename_overlap"]
    assert overlap["train_val"]["filenames"] == 30
    assert overlap["train_test"]["filenames"] == 0
    assert overlap["val_test"]["filenames"] == 0

    assert report["known_sparse_cases_total"] == 9
    assert report["known_sparse_cases_recovered"] == 9
    for case in report["known_sparse_cases"]:
        assert case["record_present"], case
        assert case["observed_annotations"] is not None
        assert case["observed_annotations"] <= 10, case

    assert report["derived_val_membership"]["excluded_records"] >= 30
    run = report["run"]
    assert run["runtime_seconds"] > 0
    print(
        f"\nintegration: runtime={run['runtime_seconds']}s "
        f"peak_memory={run['peak_memory_mb']}MB ({run['peak_memory_method']})"
    )
