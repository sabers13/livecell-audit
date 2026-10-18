"""Preview render: dense images get a full label layer, capped images never look sparse."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from tests.conftest import write_coco, write_tif

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_previews", ROOT / "tools/make_previews.py")
make_previews = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_previews)

N_IMAGES = 25  # the renderer's minimum curated set
DENSE_ID = 1
DENSE_POLYGONS = 5
CAP = 2


def square(x, y, side=2.0):
    return [x, y, x + side, y, x + side, y + side, x, y + side]


def _dataset(tmp_path):
    root = tmp_path / "data"
    (root / "annotations").mkdir(parents=True)
    images_dir = root / "images" / "livecell_train_val_images"
    images_dir.mkdir(parents=True)
    images, annotations, curated = [], [], []
    for image_id in range(1, N_IMAGES + 1):
        name = f"A172_Phase_A7_1_00d00h00m_{image_id}.tif"
        write_tif(str(images_dir / name), size=(16, 12))
        images.append({"id": image_id, "width": 16, "height": 12, "file_name": name})
        count = DENSE_POLYGONS if image_id == DENSE_ID else 1
        for k in range(count):
            annotations.append({
                "id": image_id * 100 + k, "image_id": image_id, "category_id": 1,
                "segmentation": [square(1.0 + 3 * k, 1.0)], "area": 4.0,
                "bbox": [1.0 + 3 * k, 1.0, 2.0, 2.0], "iscrowd": 0,
            })
        curated.append({"preview_id": f"train_{image_id}", "split": "train",
                        "image_id": image_id, "file_name": name})
    write_coco(str(root / "annotations" / "livecell_coco_train.json"), images, annotations)
    curated_path = tmp_path / "curated.json"
    curated_path.write_text(json.dumps({"examples": curated}))
    return root, curated_path


def test_dense_image_gets_a_full_label_layer(tmp_path):
    from PIL import Image

    data_root, curated = _dataset(tmp_path)
    out = tmp_path / "previews"
    assert make_previews.main([
        "--curated", str(curated), "--data-root", str(data_root), "--out", str(out),
        "--outline-cap", str(CAP),
    ]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    polygons = json.loads((out / "polygons.json").read_text())["examples"]
    by_id = {entry["preview_id"]: entry for entry in manifest["previews"]}

    dense = by_id[f"train_{DENSE_ID}"]
    assert dense["polygons_total"] == DENSE_POLYGONS
    assert dense["polygons_drawn"] == CAP  # polygons.json keeps its cap
    assert len(polygons[f"train_{DENSE_ID}"]["polygons"]) == CAP
    layer = dense["labels_layer"]
    assert layer["outlines_drawn"] == DENSE_POLYGONS
    layer_path = out / layer["file"]
    assert hashlib.sha256(layer_path.read_bytes()).hexdigest() == layer["png_sha256"]
    with Image.open(layer_path) as image:
        assert image.mode == "RGBA"
        assert image.size == (dense["render_parameters"]["rendered_width"],
                              dense["render_parameters"]["rendered_height"])
        assert image.getchannel("A").getbbox() is not None  # outlines were drawn

    sparse = by_id["train_2"]
    assert sparse["polygons_total"] == sparse["polygons_drawn"] == 1
    assert sparse["labels_layer"] is None
    assert not (out / "train_2.labels.png").exists()


def test_guided_records_get_full_label_layers_at_source_size(tmp_path):
    from PIL import Image

    data_root, curated = _dataset(tmp_path)
    payload = json.loads(curated.read_text())
    payload["guided"] = [
        {"split": "train", "image_id": DENSE_ID, "file_name": f"A172_Phase_A7_1_00d00h00m_{DENSE_ID}.tif"},
        {"split": "train", "image_id": 2, "file_name": "A172_Phase_A7_1_00d00h00m_2.tif"},
    ]
    curated.write_text(json.dumps(payload))
    out = tmp_path / "previews"
    assert make_previews.main([
        "--curated", str(curated), "--data-root", str(data_root), "--out", str(out),
        "--outline-cap", str(CAP),
    ]) == 0
    guided = json.loads((out / "manifest.json").read_text())["guided"]
    assert [(g["image_id"], g["outlines_drawn"]) for g in guided] == [(DENSE_ID, DENSE_POLYGONS), (2, 1)]
    for entry in guided:
        path = out / entry["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["png_sha256"]
        with Image.open(path) as image:
            assert image.size == (entry["width"], entry["height"]) == (16, 12)
    # guided layers are drawn in addition to, not instead of, the capped preview data
    dense = next(p for p in json.loads((out / "manifest.json").read_text())["previews"]
                 if p["preview_id"] == f"train_{DENSE_ID}")
    assert dense["labels_layer"]["outlines_drawn"] == DENSE_POLYGONS


def test_guided_list_is_bounded(tmp_path, capsys):
    data_root, curated = _dataset(tmp_path)
    payload = json.loads(curated.read_text())
    payload["guided"] = [
        {"split": "train", "image_id": i, "file_name": f"A172_Phase_A7_1_00d00h00m_{i}.tif"}
        for i in range(1, make_previews.MAX_GUIDED + 2)
    ]
    curated.write_text(json.dumps(payload))
    assert make_previews.main([
        "--curated", str(curated), "--data-root", str(data_root), "--out", str(tmp_path / "out"),
    ]) == 2
    assert "at most" in capsys.readouterr().err
