"""Streaming reader: yields records without full loads; rejects bad structure."""

from __future__ import annotations

import json

import pytest

from livecell_audit.coco_stream import iter_split
from livecell_audit.errors import AuditError


def _write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _events(path, **kwargs):
    return list(iter_split(str(path), "train", **kwargs))


def test_streams_images_then_annotations(tmp_path):
    from tests.conftest import annotation_record, image_record, write_coco

    coco = tmp_path / "coco.json"
    write_coco(
        str(coco),
        [image_record(1, "A172_Phase_A7_1_00d00h00m_1.tif")],
        [annotation_record(5, 1), annotation_record(6, 1)],
    )
    events = _events(coco)
    assert [kind for kind, _, _ in events] == ["images", "annotations", "annotations"]
    assert events[0][2]["file_name"] == "A172_Phase_A7_1_00d00h00m_1.tif"
    assert [rec["id"] for _, _, rec in events[1:]] == [5, 6]


def test_small_chunks_give_same_result(tmp_path):
    from tests.conftest import annotation_record, image_record, write_coco

    coco = tmp_path / "coco.json"
    images = [image_record(i, f"A172_Phase_A7_1_00d00h00m_{i % 4}.tif") for i in range(1, 30)]
    anns = [annotation_record(i, (i % 29) + 1) for i in range(1, 120)]
    write_coco(str(coco), images, anns)
    full = _events(coco)
    tiny = _events(coco, chunk_size=64)
    assert tiny == full


def test_missing_top_level_key_rejected(tmp_path):
    coco = tmp_path / "coco.json"
    _write(coco, '{"images": []}')
    with pytest.raises(AuditError, match="missing required top-level key 'annotations'"):
        _events(coco)


def test_image_missing_field_rejected(tmp_path):
    coco = tmp_path / "coco.json"
    _write(
        coco,
        '{"images": [{"id": 1, "width": 4, "height": 4}], "annotations": []}',
    )
    with pytest.raises(AuditError, match="images\\[0\\].*file_name"):
        _events(coco)


def test_annotation_missing_image_id_rejected(tmp_path):
    coco = tmp_path / "coco.json"
    _write(
        coco,
        '{"images": [], "annotations": [{"id": 9}]}',
    )
    with pytest.raises(AuditError, match="annotations\\[0\\].*image_id"):
        _events(coco)


def test_truncated_file_rejected(tmp_path):
    coco = tmp_path / "coco.json"
    _write(coco, '{"images": [{"id": 1, "file_name": "a.tif", "width": 1')
    with pytest.raises(AuditError):
        _events(coco)


def test_non_object_top_level_rejected(tmp_path):
    coco = tmp_path / "coco.json"
    _write(coco, '["images"]')
    with pytest.raises(AuditError):
        _events(coco)


def test_empty_arrays_are_valid(tmp_path):
    coco = tmp_path / "coco.json"
    _write(coco, '{"images": [], "annotations": []}')
    assert _events(coco) == []


def test_wrong_field_type_rejected(tmp_path):
    coco = tmp_path / "coco.json"
    _write(
        coco,
        json.dumps(
            {
                "images": [{"id": True, "file_name": "a.tif", "width": 1, "height": 1}],
                "annotations": [],
            }
        ),
    )
    with pytest.raises(AuditError, match="'id' must be an integer"):
        _events(coco)
