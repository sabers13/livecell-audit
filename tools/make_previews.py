#!/usr/bin/env python3
"""Render the bounded illustrated previews for the review website.

BOUNDED BY DESIGN: at most ``--max-previews`` (default and maximum 40) PNG
images, each with its longest side at most ``--longest-side`` pixels (default
and maximum 704, the native LIVECell width, so a preview keeps every source
pixel), and at most ``--outline-cap`` (default and maximum 20) supplied
annotation polygons per dense image in ``polygons.json``. The COCO
annotation files are up to ~551 MB and are read once per split with the
project's streaming reader, so memory stays proportional to the curated set;
one TIFF is open at a time.

A dense image (more polygons than the cap) also gets a transparent *label
layer* PNG with every supplied outline drawn, so the website can show the
image's full labelling instead of a capped subset that would look like
missing labels.

This script runs on the server where the raw dataset lives. It is deliberately
NOT run during the local static build: the website labels missing previews
instead of inventing images.

It writes, into ``--out`` (the release run passes ``RUN/site-inputs/previews``):

- ``<preview_id>.png``          one PNG per curated example (native size by default);
- ``<preview_id>.labels.png``   dense images only: a transparent layer with
                                every supplied outline, at the preview size;
- ``guided_<split>_<id>.labels.png``  one full label layer per optional
                                ``guided`` record of the curated file (at most
                                four), drawn at the source image size;
- ``polygons.json``             the supplied annotation polygons, in original
                                TIFF coordinates, capped in source order;
- ``manifest.json``             source file, TIFF SHA-256, PNG SHA-256 and the
                                render parameters for every preview (written
                                last, so a partial run is detectable).

Example (server):
    run-job .venv/bin/python tools/make_previews.py \\
        --curated web/review/data/curated-examples.json \\
        --data-root data/raw \\
        --out "$RUN/site-inputs/previews"

Pre-flight only (no rendering, no COCO streaming):
    run-job .venv/bin/python tools/make_previews.py --validate-only \\
        --data-root data/raw --out "$RUN/site-inputs/previews"
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

#: Hard bounds. They are the contract of this script; do not raise them.
MAX_PREVIEWS = 40
MIN_PREVIEWS = 25
MAX_LONGEST_SIDE = 704
MAX_OUTLINE_CAP = 20
DEFAULT_LONGEST_SIDE = 704
DEFAULT_OUTLINE_CAP = 20
SPLITS = ("train", "val", "test")
ANNOTATION_FILES = {
    "train": "annotations/livecell_coco_train.json",
    "val": "annotations/livecell_coco_val.json",
    "test": "annotations/livecell_coco_test.json",
}
REQUIRED_ENTRY_KEYS = ("preview_id", "split", "image_id", "file_name")
MAX_GUIDED = 4
GUIDED_KEYS = ("split", "image_id", "file_name")


class PreviewError(Exception):
    """A bounded-preview run that must stop rather than guess."""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_curated(path, max_previews):
    if not os.path.isfile(path):
        raise PreviewError(f"curated example list not found: {path}")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data.get("examples")
    if not isinstance(entries, list) or not entries:
        raise PreviewError(f"{path}: 'examples' must be a non-empty list")
    if len(entries) > max_previews:
        raise PreviewError(
            f"{path}: {len(entries)} examples exceeds the {max_previews} preview bound; "
            "reduce the curated selection instead of raising the bound"
        )
    if len(entries) < MIN_PREVIEWS:
        raise PreviewError(f"{path}: {len(entries)} examples is below the {MIN_PREVIEWS} minimum")
    seen = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PreviewError(f"{path}: examples[{position}] must be an object")
        for key in REQUIRED_ENTRY_KEYS:
            if key not in entry:
                raise PreviewError(f"{path}: examples[{position}] is missing {key!r}")
        if entry["split"] not in SPLITS:
            raise PreviewError(f"{path}: examples[{position}] has unknown split {entry['split']!r}")
        if isinstance(entry["image_id"], bool) or not isinstance(entry["image_id"], int):
            raise PreviewError(f"{path}: examples[{position}] image_id must be an integer")
        expected = f"{entry['split']}_{entry['image_id']}"
        if entry["preview_id"] != expected:
            raise PreviewError(
                f"{path}: examples[{position}] preview_id {entry['preview_id']!r} "
                f"does not match '{expected}'"
            )
        if entry["preview_id"] in seen:
            raise PreviewError(f"{path}: duplicate preview_id {entry['preview_id']!r}")
        seen.add(entry["preview_id"])
    guided = data.get("guided") or []
    if not isinstance(guided, list) or len(guided) > MAX_GUIDED:
        raise PreviewError(f"{path}: 'guided' must be a list of at most {MAX_GUIDED} records")
    for position, entry in enumerate(guided):
        if not isinstance(entry, dict) or any(key not in entry for key in GUIDED_KEYS):
            raise PreviewError(f"{path}: guided[{position}] needs {', '.join(GUIDED_KEYS)}")
        if entry["split"] not in SPLITS:
            raise PreviewError(f"{path}: guided[{position}] has unknown split {entry['split']!r}")
        if isinstance(entry["image_id"], bool) or not isinstance(entry["image_id"], int):
            raise PreviewError(f"{path}: guided[{position}] image_id must be an integer")
    return data


def check_data_root(data_root, curated):
    if not data_root or not os.path.isdir(data_root):
        raise PreviewError(f"data root is not a directory: {data_root!r}")
    images_root = os.path.join(data_root, "images")
    if not os.path.isdir(images_root):
        raise PreviewError(f"images directory not found under the data root: {images_root}")
    needed = sorted({entry["split"] for entry in curated["examples"]})
    missing = []
    for split in needed:
        annotation = os.path.join(data_root, ANNOTATION_FILES[split])
        if not os.path.isfile(annotation):
            missing.append(annotation)
    if missing:
        raise PreviewError("missing annotation file(s): " + ", ".join(missing))
    return images_root, needed


def index_images(images_root):
    """filename -> path relative to the images root (one walk, no copying)."""
    index = {}
    for dirpath, _dirnames, filenames in os.walk(images_root):
        for name in filenames:
            if name in index:
                continue
            index[name] = os.path.relpath(os.path.join(dirpath, name), images_root)
    if not index:
        raise PreviewError(f"no image files found under {images_root}")
    return index


def stream_split(annotation_path, split, wanted, outline_cap):
    """Read one COCO file once and keep only what the previews need.

    Returns ``{image_id: {"polygons": [[[x, y], ...]], "all_polygons": [[x, y, ...]],
    "total": int, "width": int, "height": int}}``. ``polygons`` is capped in
    source order; ``all_polygons`` keeps every supplied polygon as a flat
    coordinate list for the label layer. Memory stays proportional to the
    curated set (at most 40 images) rather than to the file size.
    """
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from livecell_audit.coco_stream import iter_split
    except ImportError as exc:  # pragma: no cover - depends on the checkout layout
        raise PreviewError(
            f"cannot import livecell_audit.coco_stream ({exc}); run from the project "
            "checkout so that src/ is on the path"
        )

    def new_entry():
        return {"polygons": [], "all_polygons": [], "total": 0, "width": 0, "height": 0}

    found = {}
    for array, _position, record in iter_split(annotation_path, split):
        if array == "images":
            image_id = record["id"]
            if image_id in wanted:
                entry = found.setdefault(image_id, new_entry())
                entry["width"] = record["width"]
                entry["height"] = record["height"]
        else:
            image_id = record["image_id"]
            if image_id not in wanted:
                continue
            entry = found.setdefault(image_id, new_entry())
            segmentation = record.get("segmentation") or []
            entry["total"] += len(segmentation)
            for polygon in segmentation:
                if len(polygon) >= 6:
                    entry["all_polygons"].append([float(value) for value in polygon])
            if len(entry["polygons"]) >= outline_cap:
                continue
            for polygon in segmentation:
                if len(polygon) < 6:
                    continue
                points = [
                    (float(polygon[i]), float(polygon[i + 1]))
                    for i in range(0, len(polygon) - 1, 2)
                ]
                entry["polygons"].append(points)
                if len(entry["polygons"]) >= outline_cap:
                    break
    return found


def render_png(source_path, target_path, longest_side, polygons, overlay_mode):
    from PIL import Image, ImageDraw

    with Image.open(source_path) as image:
        source_width, source_height = int(image.width), int(image.height)
        frame = image.convert("L") if image.mode not in ("L", "RGB") else image.copy()
    scale = min(1.0, longest_side / max(source_width, source_height))
    rendered_width = max(1, int(round(source_width * scale)))
    rendered_height = max(1, int(round(source_height * scale)))
    if (rendered_width, rendered_height) != (source_width, source_height):
        frame = frame.resize((rendered_width, rendered_height), Image.LANCZOS)
    if overlay_mode == "burned":
        frame = frame.convert("RGB")
        draw = ImageDraw.Draw(frame)
        width = max(2, int(round(2.4 * scale)))
        for points in polygons:
            scaled = [(x * scale, y * scale) for x, y in points]
            draw.polygon(scaled, outline=(15, 23, 42), width=width + 2)
            draw.polygon(scaled, outline=(255, 237, 0), width=width)
    frame.save(target_path, format="PNG", optimize=True)
    return source_width, source_height, rendered_width, rendered_height


def render_label_layer(target_path, source_width, source_height, rendered_width,
                       rendered_height, flat_polygons):
    """Draw every supplied outline on a transparent layer at the preview size."""
    from PIL import Image, ImageDraw

    scale_x = rendered_width / source_width
    scale_y = rendered_height / source_height
    layer = Image.new("RGBA", (rendered_width, rendered_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for flat in flat_polygons:
        points = [
            (flat[i] * scale_x, flat[i + 1] * scale_y) for i in range(0, len(flat) - 1, 2)
        ]
        draw.polygon(points, fill=(255, 237, 0, 46), outline=(255, 237, 0, 255), width=1)
    layer.save(target_path, format="PNG", optimize=True)
    return len(flat_polygons)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the bounded illustrated previews for the review website."
    )
    parser.add_argument(
        "--curated", default="web/review/data/curated-examples.json",
        help="curated example list written by tools/build_review_site.py",
    )
    parser.add_argument("--data-root", required=True, help="raw data root (server side)")
    parser.add_argument("--out", required=True, help="output directory, e.g. RUN/site-inputs/previews")
    parser.add_argument("--longest-side", type=int, default=DEFAULT_LONGEST_SIDE)
    parser.add_argument("--outline-cap", type=int, default=DEFAULT_OUTLINE_CAP)
    parser.add_argument("--max-previews", type=int, default=MAX_PREVIEWS)
    parser.add_argument(
        "--overlay-mode", choices=("plain", "burned"), default="plain",
        help="plain keeps the PNG free of marks so the interface can toggle the "
             "outlines it draws from polygons.json; burned composites them in",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="check arguments, the curated list and the data root, then stop",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    args = parser.parse_args(argv)

    try:
        if args.max_previews > MAX_PREVIEWS:
            raise PreviewError(
                f"--max-previews {args.max_previews} exceeds the hard bound {MAX_PREVIEWS}"
            )
        if args.longest_side > MAX_LONGEST_SIDE or args.longest_side < 64:
            raise PreviewError(
                f"--longest-side must be between 64 and {MAX_LONGEST_SIDE}, got {args.longest_side}"
            )
        if args.outline_cap > MAX_OUTLINE_CAP or args.outline_cap < 1:
            raise PreviewError(
                f"--outline-cap must be between 1 and {MAX_OUTLINE_CAP}, got {args.outline_cap}"
            )
        curated = load_curated(args.curated, args.max_previews)
        images_root, needed_splits = check_data_root(args.data_root, curated)
        data_root = os.path.abspath(args.data_root)
        out_dir = os.path.abspath(args.out)
        if out_dir == data_root or out_dir.startswith(data_root + os.sep):
            raise PreviewError(
                "refusing to write previews inside the raw data root; choose an output "
                "directory outside the dataset"
            )
        if not args.validate_only and os.path.exists(os.path.join(out_dir, "manifest.json")):
            if not args.force:
                raise PreviewError(
                    f"{os.path.join(out_dir, 'manifest.json')} already exists; pass --force "
                    "to replace a completed preview set"
                )
    except PreviewError as exc:
        print(f"make_previews: error: {exc}", file=sys.stderr)
        return 2

    entries = curated["examples"]
    print(
        f"make_previews: {len(entries)} curated examples, splits={','.join(needed_splits)}, "
        f"longest_side={args.longest_side}, outline_cap={args.outline_cap}, "
        f"overlay_mode={args.overlay_mode}"
    )
    if args.validate_only:
        print("make_previews: validation passed; no preview rendered (--validate-only)")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    images = index_images(images_root)

    guided = curated.get("guided") or []
    wanted_by_split = {}
    for entry in list(entries) + list(guided):
        wanted_by_split.setdefault(entry["split"], set()).add(entry["image_id"])

    annotations = {}
    for split in SPLITS:
        if split not in wanted_by_split:
            continue
        annotation_path = os.path.join(data_root, ANNOTATION_FILES[split])
        print(f"make_previews: reading {ANNOTATION_FILES[split]} (streaming, bounded memory)")
        annotations[split] = stream_split(
            annotation_path, split, wanted_by_split[split], args.outline_cap
        )

    # Guided records first: each gets one full label layer at its source size,
    # drawn before the preview loop releases the per-image polygon lists.
    manifest_guided = []
    for entry in guided:
        record = annotations.get(entry["split"], {}).get(entry["image_id"])
        if record is None:
            print(f"make_previews: error: guided image id {entry['image_id']} not found in "
                  f"{entry['split']}", file=sys.stderr)
            return 2
        width, height = record["width"], record["height"]
        name = f"guided_{entry['split']}_{entry['image_id']}.labels.png"
        path = os.path.join(out_dir, name)
        drawn = render_label_layer(path, width, height, width, height, record["all_polygons"])
        manifest_guided.append({
            "split": entry["split"],
            "image_id": entry["image_id"],
            "file_name": entry["file_name"],
            "file": name,
            "width": width,
            "height": height,
            "polygons_total": record["total"],
            "outlines_drawn": drawn,
            "png_bytes": os.path.getsize(path),
            "png_sha256": sha256_file(path),
        })
        print(f"make_previews: {name} {width}x{height}, outlines {drawn}/{record['total']}")

    manifest_previews = []
    polygons_out = {}
    for entry in entries:
        preview_id = entry["preview_id"]
        split = entry["split"]
        image_id = entry["image_id"]
        file_name = entry["file_name"]
        record = annotations.get(split, {}).get(image_id)
        if record is None:
            print(f"make_previews: error: image id {image_id} not found in {split}", file=sys.stderr)
            return 2
        if file_name not in images:
            print(f"make_previews: error: missing local TIFF for {file_name}", file=sys.stderr)
            return 2
        source_path = os.path.join(images_root, images[file_name])
        target_name = f"{preview_id}.png"
        target_path = os.path.join(out_dir, target_name)

        polygons = [
            [[round(x, 4), round(y, 4)] for x, y in points] for points in record["polygons"]
        ]
        source_width, source_height, width, height = render_png(
            source_path, target_path, args.longest_side, record["polygons"], args.overlay_mode
        )
        drawn = len(polygons)
        total = record["total"]
        labels_layer = None
        if total > drawn:
            labels_name = f"{preview_id}.labels.png"
            labels_path = os.path.join(out_dir, labels_name)
            labels_drawn = render_label_layer(
                labels_path, source_width, source_height, width, height,
                record["all_polygons"],
            )
            labels_layer = {
                "file": labels_name,
                "png_bytes": os.path.getsize(labels_path),
                "png_sha256": sha256_file(labels_path),
                "outlines_drawn": labels_drawn,
                "note": "every supplied outline on a transparent layer at the preview size",
            }
        record["all_polygons"] = None  # release the full set before the next image
        polygons_out[preview_id] = {
            "split": split,
            "image_id": image_id,
            "file_name": file_name,
            "width": record["width"] or source_width,
            "height": record["height"] or source_height,
            "polygons_total": total,
            "polygons_drawn": drawn,
            "polygons": polygons,
            "labels_layer_file": labels_layer["file"] if labels_layer else None,
        }
        manifest_previews.append(
            {
                "preview_id": preview_id,
                "file": target_name,
                "split": split,
                "image_id": image_id,
                "file_name": file_name,
                "source_file": images[file_name].replace(os.sep, "/"),
                "source_bytes": os.path.getsize(source_path),
                "source_width": source_width,
                "source_height": source_height,
                "tiff_sha256": sha256_file(source_path),
                "png_bytes": os.path.getsize(target_path),
                "png_sha256": sha256_file(target_path),
                "polygons_total": total,
                "polygons_drawn": drawn,
                "labels_layer": labels_layer,
                "render_parameters": {
                    "longest_side": args.longest_side,
                    "rendered_width": width,
                    "rendered_height": height,
                    "resize": "LANCZOS" if width != source_width else "none",
                    "overlay_mode": args.overlay_mode,
                    "outline_cap": args.outline_cap,
                    "outline_cap_rule": "first N supplied polygons in COCO source order",
                },
            }
        )
        print(
            f"make_previews: {target_name} {width}x{height} from {source_width}x{source_height}, "
            f"outlines {drawn}/{total}"
            + (f", label layer {labels_layer['outlines_drawn']}" if labels_layer else "")
        )

    polygons_path = os.path.join(out_dir, "polygons.json")
    with open(polygons_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": 1,
                "outline_cap": args.outline_cap,
                "coordinate_space": "original TIFF pixels, unmodified",
                "note": (
                    "Supplied annotation polygons for the illustrated examples. The website "
                    "draws them as an overlay that can be toggled; they are labels, not cell "
                    "boundaries detected by this project."
                ),
                "examples": polygons_out,
            },
            handle,
            indent=1,
            sort_keys=True,
        )

    manifest = {
        "schema_version": 1,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": "tools/make_previews.py",
        "curated_source": os.path.relpath(args.curated).replace(os.sep, "/"),
        "curated_sha256": sha256_file(args.curated),
        "outline_cap": args.outline_cap,
        "longest_side": args.longest_side,
        "overlay_mode": args.overlay_mode,
        "count": len(manifest_previews),
        "source_data_root": "data/raw (server-side; absolute path deliberately not recorded)",
        "note": (
            "One downsampled PNG per curated example, with the source TIFF SHA-256 and the "
            "render parameters. The website draws the supplied outlines from polygons.json so "
            "a reader can switch them off; a dense image above the outline cap also has a "
            "transparent label layer with every supplied outline."
        ),
        "render": {
            "longest_side": args.longest_side,
            "outline_cap": args.outline_cap,
            "overlay_mode": args.overlay_mode,
        },
        "previews": manifest_previews,
        "guided": manifest_guided,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    temporary = manifest_path + ".partial"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    os.replace(temporary, manifest_path)

    total_bytes = sum(
        entry["png_bytes"] + ((entry["labels_layer"] or {}).get("png_bytes") or 0)
        for entry in manifest_previews
    )
    print(f"make_previews: wrote {len(manifest_previews)} previews ({total_bytes / 1e6:.1f} MB PNG)")
    print(f"make_previews: wrote {os.path.relpath(polygons_path)}")
    print(f"make_previews: wrote {os.path.relpath(manifest_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
