"""Shared pytest helpers: build tiny COCO splits and tiny TIFFs in tmp dirs."""

from __future__ import annotations

import gc
import json
import os

import pytest


@pytest.fixture(autouse=True)
def _release_memory_after_each_test():
    """Return freed heap pages to the OS after every test.

    The full pipeline (warehouse/features/ranking/evaluation) is exercised on
    fixtures; on a memory-capped runner glibc can hold freed pages, so the test
    process's RSS creeps up across a module and trips a cgroup high-water mark.
    ``malloc_trim`` releases them. It is a no-op where the symbol is absent.
    """
    yield
    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # pragma: no cover - non-glibc platforms
        pass


def pytest_configure(config):
    """Arm a faulthandler dump when ``LIVECELL_TEST_FAULTHANDLER_TIMEOUT`` is set.

    A diagnostic aid for locating a stalled test on a memory-capped runner; it
    has no effect unless the environment variable is set.
    """
    timeout = os.environ.get("LIVECELL_TEST_FAULTHANDLER_TIMEOUT")
    if timeout:
        import faulthandler

        faulthandler.dump_traceback_later(float(timeout), repeat=True)


def write_tif(path, size=(16, 12)):
    from PIL import Image

    img = Image.new("L", size, color=128)
    img.save(path, format="TIFF")


def write_coco(path, images, annotations):
    payload = {
        "info": {},
        "licenses": [],
        "categories": [{"supercategory": "cell", "id": 1, "name": "cell"}],
        "images": images,
        "annotations": annotations,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def image_record(image_id, file_name, width=16, height=12):
    return {"id": image_id, "width": width, "height": height, "file_name": file_name}


def annotation_record(ann_id, image_id, n_points=4):
    poly = [float(v) for v in range(n_points * 2)]
    return {
        "id": ann_id,
        "image_id": image_id,
        "category_id": 1,
        "segmentation": [poly],
        "area": 10.0,
        "bbox": [0.0, 0.0, 4.0, 4.0],
        "iscrowd": 0,
    }


@pytest.fixture
def mini_dataset(tmp_path):
    """A small three-split dataset exercising every finding type.

    - train: ``A172_..._1.tif`` twice (alternate annotation versions),
      ``Huh7_..._3.tif`` (shared with val), ``BV2_..._3.tif`` (1 annotation),
      ``MCF7_..._9.tif`` (referenced but missing locally), ``BADNAME.tif``
      (unparseable filename).
    - val: ``Huh7_..._3.tif`` (cross-split overlap), ``SKOV3_..._3.tif``.
    - test: ``SHSY5Y_..._4.tif``.
    """
    from pathlib import Path

    root = Path(str(tmp_path)) / "data"
    ann_dir = root / "annotations"
    ann_dir.mkdir(parents=True)
    img_dir = root / "images" / "livecell_train_val_images"
    img_dir.mkdir(parents=True)
    (root / "images" / "livecell_test_images").mkdir(parents=True)

    dup = "A172_Phase_A7_1_00d00h00m_1.tif"
    shared = "Huh7_Phase_A10_2_00d00h00m_3.tif"
    lonely = "BV2_Phase_C4_1_01d16h00m_3.tif"
    ghost = "MCF7_Phase_B3_1_00d12h00m_9.tif"
    bad = "BADNAME.tif"

    train_images = [
        image_record(1, dup),
        image_record(2, dup),  # alternate annotation version
        image_record(3, shared),
        image_record(4, lonely),
        image_record(5, ghost),
        image_record(6, bad),
    ]
    train_anns = (
        [annotation_record(101 + i, 1) for i in range(3)]
        + [annotation_record(201 + i, 2) for i in range(5)]
        + [annotation_record(301, 3)]
        + [annotation_record(401, 4)]
        + [annotation_record(501, 5)]
        + [annotation_record(601, 6)]
    )
    val_images = [
        image_record(11, shared),
        image_record(12, "SKOV3_Phase_G4_1_00d20h00m_3.tif"),
    ]
    val_anns = [annotation_record(1101, 11), annotation_record(1201, 12)]
    test_images = [image_record(21, "SHSY5Y_Phase_D10_1_01d16h00m_4.tif")]
    test_anns = [annotation_record(2101, 21)]

    write_coco(str(ann_dir / "livecell_coco_train.json"), train_images, train_anns)
    write_coco(str(ann_dir / "livecell_coco_val.json"), val_images, val_anns)
    write_coco(str(ann_dir / "livecell_coco_test.json"), test_images, test_anns)

    for name in (dup, shared, lonely, bad, "SKOV3_Phase_G4_1_00d20h00m_3.tif"):
        write_tif(str(img_dir / name))
    write_tif(str(root / "images" / "livecell_test_images" / "SHSY5Y_Phase_D10_1_01d16h00m_4.tif"))
    # ghost (MCF7_..._9.tif) is deliberately absent: a missing-file fixture.

    return {
        "data_root": str(root),
        "dup": dup,
        "shared": shared,
        "lonely": lonely,
        "ghost": ghost,
        "bad": bad,
    }
