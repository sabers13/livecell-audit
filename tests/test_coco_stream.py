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


INVALID_JSON = [
    '{"images":[],"annotations":[],}',          # trailing comma in header
    '{"images":[] "annotations":[]}',            # missing separator
    '{"images":[],,"annotations":[]}',           # doubled separator
    '{"images":[],"annotations":[]}{}',          # trailing content
    '{"images":[],"annotations":[]}false',       # trailing scalar
    '{"images":[],"annotations":[],"m":NaN}',        # non-standard constant
    '{"images":[],"annotations":[],"m":Infinity}',   # non-standard constant
    '{"images":[],"annotations":[],"m":-Infinity}',  # non-standard constant
    '{"images":[],"annotations":[],"m":01}',         # leading zero
    '{"images":[],"annotations":[],"m":1.}',         # trailing dot
    '{"images":[],"annotations":[],"m":1e}',         # dangling exponent
    '{"images":[],"annotations":[],"m":[1,]}',       # trailing comma in array
    '{"images":[],"annotations":[],"m":{"x":1,}}',   # trailing comma in object
    '{"images":[],"annotations":[],"m":"a\nb"}',     # raw control char
    '{"images":[],"annotations":[],"m":tru}',        # truncated literal
    '{"images":[{"id":1,"file_name":"x","width":1,"height":1},],"annotations":[]}',
    '{"images":[],"annotations":[{"id":2,"image_id":1},]}',
    '{"images": [{"id": 1}], "annotations": []}',    # incomplete image record
]


@pytest.mark.parametrize("index", range(len(INVALID_JSON)))
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 64, 1 << 20])
def test_malformed_json_is_rejected(tmp_path, index, chunk_size):
    coco = tmp_path / "coco.json"
    _write(coco, INVALID_JSON[index])
    with pytest.raises(AuditError):
        _events(coco, chunk_size=chunk_size)


VALID_JSON = [
    '{"images":[{"id":1,"file_name":"microscope-\u03bc.tif","width":3,"height":2}],'
    '"annotations":[{"id":2,"image_id":1,"area":1.25,"bbox":[0,0,1,1]}]}',
    '{"metadata":-1.25e-2,"annotations":[{"id":2,"image_id":1}],'
    '"images":[{"id":1,"file_name":"a.tif","width":1,"height":1}]}',
    json.dumps(
        {
            "images": [],
            "metadata": {"text": "a\\b\n\u03bc", "value": [True, None, 10000]},
            "annotations": [],
        },
        ensure_ascii=False,
    ),
    '{"images":[],"metadata":12345678901234567890,"annotations":[]}',
    '{"images":[],"metadata":-1.25e-2,"annotations":[]}',
    '{"images":[],"metadata":true,"annotations":[]}\n\t',
]


@pytest.mark.parametrize("index", range(len(VALID_JSON)))
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 64, 1 << 20])
def test_valid_json_survives_any_chunk_size(tmp_path, index, chunk_size):
    """Valid scalar/Unicode metadata must never be truncated at a chunk edge."""
    coco = tmp_path / "coco.json"
    text = VALID_JSON[index]
    _write(coco, text)
    events = _events(coco, chunk_size=chunk_size)
    loaded = json.loads(text)
    expected = [
        (key, i, row)
        for key, values in loaded.items()
        if key in {"images", "annotations"}
        for i, row in enumerate(values)
    ]
    assert events == expected


def test_unicode_split_across_single_byte_chunks(tmp_path):
    coco = tmp_path / "coco.json"
    name = "A172_Phase_A7_1_00d00h00m_1-\u03bc\u00e9.tif"
    _write(coco, json.dumps({"images": [{"id": 1, "file_name": name,
                                         "width": 1, "height": 1}],
                             "annotations": []}, ensure_ascii=False))
    events = _events(coco, chunk_size=1)
    assert events[0][2]["file_name"] == name
