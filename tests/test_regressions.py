"""Focused regressions for the reviewed audit defects.

Each test targets one reviewed failure from the audit-foundation brief:

1. COCO section ordering must not change counts, reference checks or sparse
   evidence (annotations before images must behave like images before
   annotations) with bounded-memory streaming.
2. ``--no-check-dimensions`` must report dimensions as *not checked*, distinct
   from matching / mismatching / missing / unreadable.
3. A known case is recovered only when the expected split, original image id
   **and** filename all match; a different filename with the same id never
   counts.
4. Malformed JSON (separators, trailing content, structure) is rejected
   (covered further in ``test_coco_stream.py``).
5. A failed run cannot leave a completed-looking output directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from livecell_audit.audit import AuditConfig, run_audit
from livecell_audit.errors import AuditError

COMPLETION_MARKER = "COMPLETED"


# --------------------------------------------------------------------------- #
# helpers


def _image(image_id, file_name, width=16, height=12):
    return {"id": image_id, "width": width, "height": height, "file_name": file_name}


def _ann(ann_id, image_id):
    return {
        "id": ann_id,
        "image_id": image_id,
        "segmentation": [[0.0, 0.0, 2.0, 0.0, 2.0, 2.0]],
        "area": 10.0,
        "bbox": [0.0, 0.0, 4.0, 4.0],
    }


def _write_coco(path, images, annotations, images_first=True):
    payload = {"info": {}, "categories": [], "licenses": []}
    if images_first:
        payload["images"] = images
        payload["metadata"] = {"note": "section-order regression"}
        payload["annotations"] = annotations
    else:
        payload["annotations"] = annotations
        payload["metadata"] = {"note": "section-order regression"}
        payload["images"] = images
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def _tif(path, size=(16, 12)):
    from PIL import Image

    Image.new("L", size, color=128).save(str(path), format="TIFF")


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


# --------------------------------------------------------------------------- #
# 1. JSON section ordering


def _ordered_dataset(root, images_first):
    root = Path(root)
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    dup = "A172_Phase_A7_1_00d00h00m_1.tif"
    lonely = "BV2_Phase_C4_1_01d16h00m_3.tif"
    # two records share a filename (alternate versions); one has 1 annotation.
    images = [_image(1, dup), _image(2, dup), _image(3, lonely)]
    annotations = (
        [_ann(101, 1), _ann(102, 1), _ann(103, 1)]  # id 1 -> 3 annotations
        + [_ann(201, 2)]  # id 2 -> 1 annotation (sparse)
        + [_ann(301, 3)]  # id 3 -> 1 annotation (sparse)
    )
    _write_coco(root / "annotations" / "livecell_coco_train.json", images, annotations, images_first)
    for split in ("val", "test"):
        _write_coco(root / "annotations" / f"livecell_coco_{split}.json", [], [], images_first)
    for name in (dup, lonely):
        _tif(root / "images" / name)
    return str(root)


def test_section_order_does_not_change_counts_or_evidence(tmp_path):
    first = _ordered_dataset(tmp_path / "images-first", images_first=True)
    last = _ordered_dataset(tmp_path / "annotations-first", images_first=False)
    out_first = str(tmp_path / "out-first")
    out_last = str(tmp_path / "out-last")
    report_first = run_audit(AuditConfig(data_root=first, out_dir=out_first))
    report_last = run_audit(AuditConfig(data_root=last, out_dir=out_last))

    # annotations-before-images must not invent orphans
    assert report_last["splits"]["train"]["orphan_annotations"] == 0
    assert report_first["splits"]["train"]["orphan_annotations"] == 0
    assert report_last["splits"]["train"]["annotations"] == 5
    assert report_last["total_annotations"] == 5

    # per-image counts from the images table agree
    counts_first = {r["id"]: r["annotation_count"] for r in _load_jsonl(os.path.join(out_first, "tables", "images.jsonl"))}
    counts_last = {r["id"]: r["annotation_count"] for r in _load_jsonl(os.path.join(out_last, "tables", "images.jsonl"))}
    assert counts_last == counts_first == {1: 3, 2: 1, 3: 1}

    # sparese evidence + full report agree apart from run metadata
    assert report_last["low_annotation_records"] == report_first["low_annotation_records"]
    report_first.pop("run")
    report_last.pop("run")
    assert report_last == report_first
    assert _load_json(os.path.join(out_last, "findings.json")) == _load_json(
        os.path.join(out_first, "findings.json")
    )
    assert _load_jsonl(os.path.join(out_last, "tables", "annotations.jsonl")) == _load_jsonl(
        os.path.join(out_first, "tables", "annotations.jsonl")
    )
    assert _load_json(os.path.join(out_last, "manifests", "val_membership.json")) == _load_json(
        os.path.join(out_first, "manifests", "val_membership.json")
    )


def test_section_order_tiny_chunks(tmp_path):
    """The streaming reader itself must be order-independent and chunk-safe."""
    from livecell_audit.coco_stream import iter_split

    coco = tmp_path / "coco.json"
    images = [_image(1, "a.tif"), _image(2, "b.tif")]
    annotations = [_ann(10, 1), _ann(11, 2), _ann(12, 1)]
    _write_coco(coco, images, annotations, images_first=False)
    events = list(iter_split(str(coco), "train", chunk_size=1))
    kinds = [kind for kind, _, _ in events]
    assert kinds == ["annotations", "annotations", "annotations", "images", "images"]
    assert [rec["id"] for kind, _, rec in events if kind == "annotations"] == [10, 11, 12]


# --------------------------------------------------------------------------- #
# 2. dimension states


def _dimension_dataset(root):
    root = Path(root)
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    matching = "A172_Phase_A7_1_00d00h00m_1.tif"
    mismatching = "BV2_Phase_C4_1_01d16h00m_3.tif"
    missing = "MCF7_Phase_B3_1_00d12h00m_9.tif"
    unreadable = "Huh7_Phase_A10_2_00d00h00m_3.tif"
    _tif(root / "images" / matching, size=(16, 12))
    _tif(root / "images" / mismatching, size=(16, 12))
    (root / "images" / unreadable).write_bytes(b"not a real tiff")
    images = [
        _image(1, matching, 16, 12),
        _image(2, mismatching, 704, 520),
        _image(3, missing, 16, 12),
        _image(4, unreadable, 16, 12),
    ]
    annotations = [_ann(101, 1), _ann(201, 2), _ann(301, 3), _ann(401, 4)]
    _write_coco(root / "annotations" / "livecell_coco_train.json", images, annotations)
    for split in ("val", "test"):
        _write_coco(root / "annotations" / f"livecell_coco_{split}.json", [], [])
    return str(root)


def test_no_check_dimensions_reports_not_checked(tmp_path):
    root = _dimension_dataset(tmp_path / "data")
    out = str(tmp_path / "out")
    report = run_audit(AuditConfig(data_root=root, out_dir=out, check_dimensions=False))
    rows = {r["id"]: r for r in _load_jsonl(os.path.join(out, "tables", "images.jsonl"))}
    assert all(r["dimensions_status"] == "not_checked" for r in rows.values())
    assert not any(r["dimensions_match"] for r in rows.values())
    assert report["dimension_mismatches"] == []
    assert report["dimension_status_counts"] == {
        "matching": 0,
        "mismatching": 0,
        "missing": 0,
        "unreadable": 0,
        "not_checked": 4,
    }


def test_dimension_states_are_distinct(tmp_path):
    root = _dimension_dataset(tmp_path / "data")
    out = str(tmp_path / "out")
    report = run_audit(AuditConfig(data_root=root, out_dir=out, check_dimensions=True))
    rows = {r["id"]: r for r in _load_jsonl(os.path.join(out, "tables", "images.jsonl"))}
    assert rows[1]["dimensions_status"] == "matching"
    assert rows[2]["dimensions_status"] == "mismatching"
    assert rows[3]["dimensions_status"] == "missing"
    assert rows[4]["dimensions_status"] == "unreadable"
    assert [e["image_id"] for e in report["dimension_mismatches"]] == [2]
    assert report["dimension_status_counts"] == {
        "matching": 1,
        "mismatching": 1,
        "missing": 1,
        "unreadable": 1,
        "not_checked": 0,
    }


# --------------------------------------------------------------------------- #
# 3. known-case identity


def test_known_case_requires_split_id_and_filename(tmp_path, monkeypatch):
    import livecell_audit.audit as audit_module

    monkeypatch.setattr(
        audit_module,
        "KNOWN_SPARSE_CASES",
        [
            {"file_name": "expected.tif", "split": "train", "image_id": 42},
            {"file_name": "present.tif", "split": "train", "image_id": 7},
        ],
    )
    root = tmp_path / "data"
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    # id 42 exists, but under a different filename -> must NOT be recovered.
    _write_coco(root / "annotations" / "livecell_coco_train.json",
                [_image(42, "other.tif"), _image(7, "present.tif")],
                [_ann(101, 42), _ann(201, 7)])
    for split in ("val", "test"):
        _write_coco(root / "annotations" / f"livecell_coco_{split}.json", [], [])
    report = run_audit(AuditConfig(data_root=str(root), out_dir=str(tmp_path / "out")))
    by_id = {c["image_id"]: c for c in report["known_sparse_cases"]}
    wrong = by_id[42]
    assert wrong["record_present"] is False
    assert wrong["observed_annotations"] is None
    assert wrong["recovered_by_baseline"] is False
    right = by_id[7]
    assert right["record_present"] is True
    assert right["observed_annotations"] == 1
    assert right["recovered_by_baseline"] is True


# --------------------------------------------------------------------------- #
# 5. failed runs cannot look complete


def test_failed_run_does_not_look_complete(tmp_path):

    root = tmp_path / "data"
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    _write_coco(root / "annotations" / "livecell_coco_train.json", [_image(1, "a.tif")], [_ann(1, 1)])
    for split in ("val", "test"):
        _write_coco(root / "annotations" / f"livecell_coco_{split}.json", [], [])
    out = str(tmp_path / "out")

    # First run completes and leaves an explicit completion marker.
    run_audit(AuditConfig(data_root=str(root), out_dir=out))
    assert os.path.isfile(os.path.join(out, COMPLETION_MARKER))
    good_report = _load_json(os.path.join(out, "report.json"))

    # Corrupt a split and rerun into the same directory.
    (root / "annotations" / "livecell_coco_val.json").write_text(
        '{"images": [{"id": 1}], "annotations": []}', encoding="utf-8"
    )
    try:
        run_audit(AuditConfig(data_root=str(root), out_dir=out))
    except AuditError:
        pass
    else:
        raise AssertionError("expected AuditError")
    # The completed marker is gone: the directory is no longer a completed run.
    assert not os.path.exists(os.path.join(out, COMPLETION_MARKER))
    # Staging is cleaned up; no partial data is promoted.
    assert not os.path.exists(os.path.join(out, ".staging"))
    # The stale report.json still describes the *previous* run, not partial data.
    assert _load_json(os.path.join(out, "report.json")) == good_report


def test_failed_first_run_leaves_no_manifest(tmp_path):
    root = tmp_path / "data"
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    (root / "annotations" / "livecell_coco_train.json").write_text(
        '{"images": [], "annotations": []', encoding="utf-8"
    )
    for split in ("val", "test"):
        (root / "annotations" / f"livecell_coco_{split}.json").write_text(
            '{"images": [], "annotations": []}', encoding="utf-8"
        )
    out = str(tmp_path / "out")
    try:
        run_audit(AuditConfig(data_root=str(root), out_dir=out))
    except AuditError:
        pass
    else:
        raise AssertionError("expected AuditError")
    assert not os.path.exists(os.path.join(out, "report.json"))
    assert not os.path.exists(os.path.join(out, COMPLETION_MARKER))


def _minimal_dataset(root):
    root = Path(root)
    (root / "annotations").mkdir(parents=True)
    (root / "images").mkdir(parents=True)
    _write_coco(
        root / "annotations" / "livecell_coco_train.json",
        [_image(1, "a.tif")],
        [_ann(1, 1)],
    )
    for split in ("val", "test"):
        _write_coco(root / "annotations" / f"livecell_coco_{split}.json", [], [])
    return str(root)


def test_preflight_failure_on_reused_dir_clears_old_marker(tmp_path):
    """A missing input must not leave a previous run's success marker behind."""
    root = Path(_minimal_dataset(tmp_path / "data"))
    out = str(tmp_path / "out")
    run_audit(AuditConfig(data_root=str(root), out_dir=out))
    assert os.path.isfile(os.path.join(out, COMPLETION_MARKER))
    good_report = _load_json(os.path.join(out, "report.json"))

    # Remove an input and rerun into the SAME output directory.
    os.remove(root / "annotations" / "livecell_coco_val.json")
    try:
        run_audit(AuditConfig(data_root=str(root), out_dir=out))
    except AuditError:
        pass
    else:
        raise AssertionError("expected AuditError")

    assert not os.path.exists(os.path.join(out, COMPLETION_MARKER))
    # Previous valid data is preserved, but is no longer labelled complete.
    assert _load_json(os.path.join(out, "report.json")) == good_report


def test_completion_marker_write_is_atomic(tmp_path, monkeypatch):
    """A marker serialization failure must not leave a partial marker file."""
    from livecell_audit import audit as audit_module

    marker = str(tmp_path / COMPLETION_MARKER)

    def flaky_dump(obj, fp, **kwargs):
        fp.write("{")  # a partial write ...
        raise OSError("disk full")

    monkeypatch.setattr(audit_module.json, "dump", flaky_dump)
    try:
        audit_module._write_completion_marker(marker, {"status": "completed"})
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert not os.path.exists(marker)
    assert not os.path.exists(marker + ".tmp")


def test_marker_failure_leaves_no_completed_marker(tmp_path, monkeypatch):
    """A failure while publishing the marker must leave the run incomplete."""
    from livecell_audit import audit as audit_module

    root = _minimal_dataset(tmp_path / "data")
    out = str(tmp_path / "out")

    def failing_marker(marker_path, payload):
        # Simulate a writer that starts writing the final marker then fails.
        with open(marker_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        raise OSError("marker publish failed")

    monkeypatch.setattr(audit_module, "_write_completion_marker", failing_marker)
    try:
        run_audit(AuditConfig(data_root=root, out_dir=out))
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError")
    assert not os.path.exists(os.path.join(out, COMPLETION_MARKER))
    assert not os.path.exists(os.path.join(out, COMPLETION_MARKER + ".tmp"))
    assert not os.path.exists(os.path.join(out, ".staging"))


def test_marker_timestamp_does_not_change_data_exports(mini_dataset, tmp_path, monkeypatch):
    """The marker is run metadata: differing timestamps keep identical data.

    Two fast runs can share a wall-clock second, which would hide marker
    nondeterminism. This injects distinct completion timestamps and checks the
    data exports are still byte-identical.
    """
    from livecell_audit import audit as audit_module

    class _FakeTime:
        def __init__(self, stamps):
            self._stamps = iter(stamps)

        def strftime(self, _fmt, _tm):
            return next(self._stamps)

        def gmtime(self):
            return None

        def perf_counter(self):
            return 0.0

    monkeypatch.setattr(audit_module, "time", _FakeTime(["2020-01-01T00:00:00Z", "2031-12-31T23:59:59Z"]))
    out_a = str(tmp_path / "out-a")
    out_b = str(tmp_path / "out-b")
    run_audit(AuditConfig(data_root=mini_dataset["data_root"], out_dir=out_a))
    run_audit(AuditConfig(data_root=mini_dataset["data_root"], out_dir=out_b))

    marker_a = _load_json(os.path.join(out_a, COMPLETION_MARKER))
    marker_b = _load_json(os.path.join(out_b, COMPLETION_MARKER))
    assert marker_a["completed_at"] == "2020-01-01T00:00:00Z"
    assert marker_b["completed_at"] == "2031-12-31T23:59:59Z"
    marker_a.pop("completed_at")
    marker_b.pop("completed_at")
    assert marker_a == marker_b

    for relative in (
        "findings.json",
        "tables/images.jsonl",
        "tables/annotations.jsonl",
        "manifests/sources.json",
        "manifests/val_membership.json",
    ):
        with open(os.path.join(out_a, relative), "rb") as handle_a, open(
            os.path.join(out_b, relative), "rb"
        ) as handle_b:
            assert handle_a.read() == handle_b.read(), relative

    report_a = _load_json(os.path.join(out_a, "report.json"))
    report_b = _load_json(os.path.join(out_b, "report.json"))
    report_a.pop("run")
    report_b.pop("run")
    assert report_a == report_b
