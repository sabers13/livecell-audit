"""Core audit: stream the COCO splits, reconcile files, analyse, and export.

The audit distinguishes three things that are easy to conflate:

- *physical image identity* -- the ``file_name`` (one microscope acquisition);
- *annotation version* -- a COCO image ``id`` plus the annotations pointing at
  it (the dataset authors kept alternate versions of some images, see
  upstream issue #14);
- *split membership* -- which of train/val/test references the record.

Two records sharing a filename with different ids are therefore reported as
*alternate annotation versions needing an explicit policy*, not as corrupt
records or automatic ID collisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import resource

    def _peak_rss_mb() -> Optional[float]:
        # Linux ru_maxrss is in kilobytes.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    _MEMORY_METHOD = (
        "resource.getrusage(RUSAGE_SELF).ru_maxrss (peak resident set size, MB)"
    )
except ImportError:  # pragma: no cover - non-POSIX platforms
    def _peak_rss_mb() -> Optional[float]:
        return None

    _MEMORY_METHOD = "unavailable on this platform"

from . import __version__
from .coco_stream import iter_split
from .errors import AuditError
from .filenames import parse_filename
from .known_cases import KNOWN_SPARSE_CASES, UPSTREAM_ISSUE

SPLITS = ("train", "val", "test")
COCO_TEMPLATE = "livecell_coco_{split}.json"


@dataclass
class AuditConfig:
    """Inputs for one audit run."""

    data_root: str
    out_dir: str
    splits: Tuple[str, ...] = SPLITS
    low_count_threshold: int = 10
    check_dimensions: bool = True


def sha256_file(path: str) -> Tuple[str, int]:
    """Return ``(hex digest, byte size)`` streaming from disk."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _dump_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _index_local_images(images_root: str) -> Tuple[Dict[str, str], int]:
    """Map basename -> full path for every file under ``images_root``."""
    index: Dict[str, str] = {}
    total = 0
    for dirpath, _dirnames, filenames in os.walk(images_root):
        for name in filenames:
            total += 1
            index.setdefault(name, os.path.join(dirpath, name))
    return index, total


def _image_dimensions(path: str) -> Optional[Tuple[int, int]]:
    from PIL import Image

    try:
        with Image.open(path) as img:
            return (int(img.width), int(img.height))
    except Exception:
        return None


def _segmentation_points(segmentation: Any) -> Optional[int]:
    if not isinstance(segmentation, list):
        return None
    total = 0
    for poly in segmentation:
        if isinstance(poly, list):
            total += len(poly) // 2
    return total


def run_audit(config: AuditConfig) -> Dict[str, Any]:
    """Run the full audit and write deterministic outputs to ``out_dir``.

    Returns the report payload (the same object written to ``report.json``).
    """
    started = time.perf_counter()
    os.makedirs(config.out_dir, exist_ok=True)
    tables_dir = os.path.join(config.out_dir, "tables")
    manifests_dir = os.path.join(config.out_dir, "manifests")
    os.makedirs(tables_dir, exist_ok=True)
    os.makedirs(manifests_dir, exist_ok=True)

    annotations_path = os.path.join(config.data_root, "annotations")
    images_root = os.path.join(config.data_root, "images")
    for split in config.splits:
        coco_path = os.path.join(annotations_path, COCO_TEMPLATE.format(split=split))
        if not os.path.isfile(coco_path):
            raise AuditError(f"missing COCO file for split {split!r}: {coco_path}")
    if not os.path.isdir(images_root):
        raise AuditError(f"missing images directory: {images_root}")

    # Source checksum manifest first: it identifies exactly what was read.
    sources = {"coco_files": {}, "images_root": images_root}
    for split in config.splits:
        coco_path = os.path.join(annotations_path, COCO_TEMPLATE.format(split=split))
        digest, size = sha256_file(coco_path)
        sources["coco_files"][split] = {
            "path": os.path.relpath(coco_path, config.data_root),
            "sha256": digest,
            "bytes": size,
        }

    local_index, local_total = _index_local_images(images_root)
    local_dimensions: Dict[str, Optional[Tuple[int, int]]] = {}
    if config.check_dimensions:
        for name in sorted(local_index):
            local_dimensions[name] = _image_dimensions(local_index[name])
    sources["local_image_files"] = local_total

    split_data: Dict[str, Dict[str, Any]] = {}
    annotations_table = open(os.path.join(tables_dir, "annotations.jsonl"), "w", encoding="utf-8")
    try:
        for split in config.splits:
            coco_path = os.path.join(annotations_path, COCO_TEMPLATE.format(split=split))
            images: List[Dict[str, Any]] = []
            id_to_positions: Dict[int, List[int]] = {}
            counts: Dict[int, int] = {}
            ann_ids_for_image: Dict[int, List[int]] = {}
            sample_seg_for_image: Dict[int, Dict[str, Any]] = {}
            seen_ann_ids = set()
            duplicate_ann_ids = 0
            orphan_annotations: List[int] = []
            total_annotations = 0
            for array, index, record in iter_split(coco_path, split):
                if array == "images":
                    image_id = record["id"]
                    counts[image_id] = 0
                    ann_ids_for_image[image_id] = []
                    id_to_positions.setdefault(image_id, []).append(len(images))
                    images.append(record)
                else:
                    total_annotations += 1
                    image_id = record["image_id"]
                    ann_id = record["id"]
                    if ann_id in seen_ann_ids:
                        duplicate_ann_ids += 1
                    else:
                        seen_ann_ids.add(ann_id)
                    if image_id in counts:
                        counts[image_id] += 1
                        if counts[image_id] <= max(config.low_count_threshold, 1) + 40:
                            ann_ids_for_image[image_id].append(ann_id)
                            if len(ann_ids_for_image[image_id]) == 1:
                                sample_seg_for_image[image_id] = {
                                    "annotation_id": ann_id,
                                    "segmentation": record.get("segmentation"),
                                    "bbox": record.get("bbox"),
                                    "area": record.get("area"),
                                }
                    else:
                        if len(orphan_annotations) < 1000:
                            orphan_annotations.append(ann_id)
                    annotations_table.write(
                        json.dumps(
                            {
                                "split": split,
                                "id": ann_id,
                                "image_id": image_id,
                                "area": record.get("area"),
                                "bbox": record.get("bbox"),
                                "n_points": _segmentation_points(record.get("segmentation")),
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
            split_data[split] = {
                "images": images,
                "id_to_positions": id_to_positions,
                "counts": counts,
                "ann_ids_for_image": ann_ids_for_image,
                "sample_seg_for_image": sample_seg_for_image,
                "total_annotations": total_annotations,
                "duplicate_ann_ids": duplicate_ann_ids,
                "orphan_annotation_ids": orphan_annotations,
            }
    finally:
        annotations_table.close()

    # ---- normalised image table -------------------------------------------
    images_rows: List[Dict[str, Any]] = []
    referenced_names: Dict[str, int] = {}
    for split in config.splits:
        data = split_data[split]
        for position, record in enumerate(data["images"]):
            file_name = record["file_name"]
            referenced_names[file_name] = referenced_names.get(file_name, 0) + 1
            parsed = parse_filename(file_name)
            local_path = local_index.get(file_name)
            actual = local_dimensions.get(file_name) if local_path else None
            images_rows.append(
                {
                    "split": split,
                    "position": position,
                    "id": record["id"],
                    "file_name": file_name,
                    "original_filename": record.get("original_filename"),
                    "cell_line": parsed["cell_line"] if parsed else None,
                    "well": parsed["well"] if parsed else None,
                    "well_row": parsed["well_row"] if parsed else None,
                    "well_col": parsed["well_col"] if parsed else None,
                    "field": parsed["field"] if parsed else None,
                    "timestamp": parsed["timestamp"] if parsed else None,
                    "time_hours": parsed["time_hours"] if parsed else None,
                    "crop": parsed["crop"] if parsed else None,
                    "filename_parsed": parsed is not None,
                    "width": record["width"],
                    "height": record["height"],
                    "local_path": (
                        os.path.relpath(local_path, config.data_root)
                        if local_path
                        else None
                    ),
                    "local_exists": local_path is not None,
                    "local_width": actual[0] if actual else None,
                    "local_height": actual[1] if actual else None,
                    "dimensions_match": (
                        bool(actual)
                        and actual[0] == record["width"]
                        and actual[1] == record["height"]
                    ),
                    "annotation_count": data["counts"].get(record["id"], 0),
                }
            )
    images_rows.sort(key=lambda row: (SPLITS.index(row["split"]), row["id"]))
    with open(os.path.join(tables_dir, "images.jsonl"), "w", encoding="utf-8") as handle:
        for row in images_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    # ---- filename -> splits / records --------------------------------------
    file_to_splits: Dict[str, List[str]] = {}
    file_to_records: Dict[str, Dict[str, List[int]]] = {}
    for row in images_rows:
        file_to_splits.setdefault(row["file_name"], [])
        if row["split"] not in file_to_splits[row["file_name"]]:
            file_to_splits[row["file_name"]].append(row["split"])
        file_to_records.setdefault(row["file_name"], {}).setdefault(row["split"], []).append(
            row["id"]
        )
    for file_name in file_to_splits:
        file_to_splits[file_name] = sorted(
            file_to_splits[file_name], key=SPLITS.index
        )

    # ---- per-split summary ---------------------------------------------------
    splits_summary: Dict[str, Any] = {}
    for split in config.splits:
        data = split_data[split]
        names = [r["file_name"] for r in data["images"]]
        unique_names = sorted(set(names))
        repeated = sorted(n for n in unique_names if names.count(n) > 1)
        at_most = sum(
            1 for r in data["images"] if data["counts"].get(r["id"], 0) <= config.low_count_threshold
        )
        duplicate_ids = sorted(i for i, pos in data["id_to_positions"].items() if len(pos) > 1)
        splits_summary[split] = {
            "image_records": len(data["images"]),
            "unique_ids": len(data["id_to_positions"]),
            "duplicate_ids": duplicate_ids,
            "unique_filenames": len(unique_names),
            "extra_filename_records": len(names) - len(unique_names),
            "repeated_filenames": repeated,
            "annotations": data["total_annotations"],
            "images_at_most_10_annotations": at_most,
            "duplicate_annotation_ids": data["duplicate_ann_ids"],
            "orphan_annotations": len(data["orphan_annotation_ids"]),
        }

    overlap: Dict[str, Any] = {}
    for pair in (("train", "val"), ("train", "test"), ("val", "test")):
        key = f"{pair[0]}_{pair[1]}"
        shared = sorted(
            n
            for n, splits in file_to_splits.items()
            if pair[0] in splits and pair[1] in splits
        )
        overlap[key] = {"filenames": len(shared), "files": shared}

    missing = sorted(n for n in file_to_splits if n not in local_index)
    orphans = sorted(n for n in local_index if n not in file_to_splits)
    dim_mismatches = sorted(
        [
            {
                "file_name": r["file_name"],
                "split": r["split"],
                "image_id": r["id"],
                "record_width": r["width"],
                "record_height": r["height"],
                "local_width": r["local_width"],
                "local_height": r["local_height"],
            }
            for r in images_rows
            if r["local_exists"] and not r["dimensions_match"]
        ],
        key=lambda d: (d["file_name"], d["split"], d["image_id"]),
    )
    unparseable = sorted(
        [
            {"file_name": r["file_name"], "split": r["split"], "image_id": r["id"]}
            for r in images_rows
            if not r["filename_parsed"]
        ],
        key=lambda d: (d["file_name"], d["split"], d["image_id"]),
    )

    # ---- low-count baseline + known cases ------------------------------------
    low_records = sorted(
        [
            {
                "file_name": r["file_name"],
                "split": r["split"],
                "image_id": r["id"],
                "count": r["annotation_count"],
                "annotation_ids": split_data[r["split"]]["ann_ids_for_image"].get(r["id"], []),
            }
            for r in images_rows
            if r["annotation_count"] <= config.low_count_threshold
        ],
        key=lambda d: (d["split"], d["file_name"], d["image_id"]),
    )
    record_lookup = {(r["split"], r["id"]): r for r in images_rows}
    known_cases = []
    for case in KNOWN_SPARSE_CASES:
        key = (str(case["split"]), int(case["image_id"]))
        row = record_lookup.get(key)
        observed = row["annotation_count"] if row else None
        known_cases.append(
            {
                "file_name": case["file_name"],
                "split": case["split"],
                "image_id": case["image_id"],
                "observed_annotations": observed,
                "record_present": row is not None
                and (row["file_name"] == case["file_name"]),
                "recovered_by_baseline": observed is not None
                and observed <= config.low_count_threshold,
                "upstream_ref": UPSTREAM_ISSUE,
            }
        )

    # ---- findings -------------------------------------------------------------
    findings: List[Dict[str, Any]] = []
    for file_name in overlap["train_val"]["files"]:
        per_split = {
            split: {
                "image_ids": sorted(file_to_records[file_name].get(split, [])),
                "counts": [
                    split_data[split]["counts"].get(i, 0)
                    for i in sorted(file_to_records[file_name].get(split, []))
                ],
            }
            for split in ("train", "val")
        }
        findings.append(
            {
                "finding_id": f"cross-split-overlap:{file_name}",
                "type": "cross_split_overlap",
                "status": "Confirmed overlap",
                "title": f"{file_name} is used in both the learning and checking groups",
                "file_name": file_name,
                "splits": ["train", "val"],
                "records": per_split,
                "recommendation": (
                    "Keep each source image in one group for this evaluation, "
                    "and record how the groups were chosen."
                ),
                "evidence": {
                    "train_records": per_split["train"],
                    "val_records": per_split["val"],
                },
            }
        )
    for split in config.splits:
        for file_name in splits_summary[split]["repeated_filenames"]:
            ids = sorted(file_to_records[file_name][split])
            findings.append(
                {
                    "finding_id": f"alternate-versions:{split}:{file_name}",
                    "type": "alternate_annotation_version",
                    "status": "Alternative annotation versions",
                    "title": f"{file_name} has {len(ids)} records in {split}",
                    "file_name": file_name,
                    "splits": [split],
                    "records": {
                        split: {
                            "image_ids": ids,
                            "counts": [split_data[split]["counts"].get(i, 0) for i in ids],
                        }
                    },
                    "recommendation": (
                        "Choose which annotation version to use and record the "
                        "choice; the original records are preserved."
                    ),
                    "evidence": {
                        "image_ids": ids,
                        "note": (
                            "Different ids for the same filename are alternate "
                            "annotation versions (upstream issue #14), not "
                            "automatically corrupt records."
                        ),
                    },
                }
            )
    for entry in low_records:
        sample = split_data[entry["split"]]["sample_seg_for_image"].get(entry["image_id"], {})
        findings.append(
            {
                "finding_id": (
                    f"low-annotations:{entry['split']}:{entry['file_name']}:{entry['image_id']}"
                ),
                "type": "low_annotation_count",
                "status": "Needs label review",
                "title": (
                    f"{entry['file_name']} marks {entry['count']} "
                    f"{'cell' if entry['count'] == 1 else 'cells'}"
                ),
                "file_name": entry["file_name"],
                "splits": [entry["split"]],
                "records": {
                    entry["split"]: {
                        "image_ids": [entry["image_id"]],
                        "counts": [entry["count"]],
                    }
                },
                "recommendation": (
                    "Review the annotation before using this image. A low label "
                    "count alone is not proof of an error."
                ),
                "evidence": {
                    "image_id": entry["image_id"],
                    "annotation_ids": entry["annotation_ids"],
                    "sample_annotation": sample,
                },
            }
        )
    for file_name in missing:
        findings.append(
            {
                "finding_id": f"missing-file:{file_name}",
                "type": "missing_image_file",
                "status": "Needs review",
                "title": f"{file_name} is referenced but has no local image file",
                "file_name": file_name,
                "splits": file_to_splits[file_name],
                "records": {
                    split: {
                        "image_ids": sorted(file_to_records[file_name].get(split, [])),
                        "counts": [],
                    }
                    for split in file_to_splits[file_name]
                },
                "recommendation": "Locate the missing file or exclude the record, and record the decision.",
                "evidence": {"referenced_by": file_to_records[file_name]},
            }
        )
    for entry in dim_mismatches:
        findings.append(
            {
                "finding_id": (
                    f"dimension-mismatch:{entry['split']}:{entry['file_name']}:{entry['image_id']}"
                ),
                "type": "dimension_mismatch",
                "status": "Needs review",
                "title": f"{entry['file_name']} local size differs from its record",
                "file_name": entry["file_name"],
                "splits": [entry["split"]],
                "records": {},
                "recommendation": "Check whether the image file or the record is out of date.",
                "evidence": entry,
            }
        )
    for entry in unparseable:
        findings.append(
            {
                "finding_id": (
                    f"unparseable-filename:{entry['split']}:{entry['file_name']}:{entry['image_id']}"
                ),
                "type": "unparseable_filename",
                "status": "Needs review",
                "title": f"{entry['file_name']} does not match the naming scheme",
                "file_name": entry["file_name"],
                "splits": [entry["split"]],
                "records": {},
                "recommendation": "Check the filename against the acquisition naming scheme.",
                "evidence": entry,
            }
        )
    findings.sort(key=lambda f: (f["type"], f["file_name"], f["finding_id"]))
    _dump_json(os.path.join(config.out_dir, "findings.json"), findings)

    # ---- derived validation membership ----------------------------------------
    train_names = {
        r["file_name"] for r in images_rows if r["split"] == "train"
    }
    membership = []
    exclusions = []
    for row in images_rows:
        if row["split"] != "val":
            continue
        if row["file_name"] in train_names:
            membership.append(
                {
                    "split": "val",
                    "image_id": row["id"],
                    "file_name": row["file_name"],
                    "decision": "excluded",
                    "reason": (
                        "filename also appears in the train split "
                        f"({len(file_to_records[row['file_name']].get('train', []))} "
                        "train record(s)); excluded to avoid learning/checking overlap"
                    ),
                }
            )
            exclusions.append(
                {"file_name": row["file_name"], "image_id": row["id"]}
            )
        else:
            membership.append(
                {
                    "split": "val",
                    "image_id": row["id"],
                    "file_name": row["file_name"],
                    "decision": "included",
                    "reason": "filename does not appear in the train split",
                }
            )
    membership.sort(key=lambda m: m["image_id"])
    exclusions.sort(key=lambda m: (m["file_name"], m["image_id"]))
    derived_policy = (
        "Derived validation membership for evaluation use: exclude every "
        "validation record whose physical image (filename) also appears in the "
        "train split. This is a documented evaluation policy, not an official "
        "correction of the dataset. Source files are left intact; original ids "
        "and alternate annotation versions are preserved; no polygon sets were "
        "merged."
    )
    _dump_json(
        os.path.join(manifests_dir, "val_membership.json"),
        {
            "policy": derived_policy,
            "val_records": len(membership),
            "included_records": sum(1 for m in membership if m["decision"] == "included"),
            "excluded_records": sum(1 for m in membership if m["decision"] == "excluded"),
            "exclusions": exclusions,
            "membership": membership,
        },
    )

    # ---- report ----------------------------------------------------------------
    shared_example_name = (
        overlap["train_val"]["files"][0] if overlap["train_val"]["files"] else None
    )
    sparse_candidates = [e for e in low_records if e["split"] == "train" and e["count"] <= 1]
    sparse_example = sparse_candidates[0] if sparse_candidates else None
    sparse_sample = (
        split_data[sparse_example["split"]]["sample_seg_for_image"].get(
            sparse_example["image_id"], {}
        )
        if sparse_example
        else {}
    )
    report = {
        "tool": {"name": "livecell-audit", "version": __version__},
        "snapshot_date": "2026-10-17",
        "splits": splits_summary,
        "total_image_records": sum(v["image_records"] for v in splits_summary.values()),
        "unique_filenames": len(file_to_splits),
        "total_annotations": sum(v["annotations"] for v in splits_summary.values()),
        "local_image_files": local_total,
        "missing_image_filenames": missing,
        "orphan_local_files": orphans,
        "dimension_mismatches": dim_mismatches,
        "unparseable_filenames": unparseable,
        "cross_split_filename_overlap": overlap,
        "low_annotation_records": low_records,
        "low_count_threshold": config.low_count_threshold,
        "known_sparse_cases": known_cases,
        "known_sparse_cases_recovered": sum(1 for k in known_cases if k["recovered_by_baseline"]),
        "known_sparse_cases_total": len(known_cases),
        "derived_val_membership": {
            "policy": derived_policy,
            "val_records": len(membership),
            "included_records": sum(1 for m in membership if m["decision"] == "included"),
            "excluded_records": sum(1 for m in membership if m["decision"] == "excluded"),
            "exclusions": exclusions,
        },
        "examples": {
            "shared_image": (
                {
                    "file_name": shared_example_name,
                    "train_image_ids": sorted(
                        file_to_records[shared_example_name].get("train", [])
                    ),
                    "val_image_ids": sorted(
                        file_to_records[shared_example_name].get("val", [])
                    ),
                }
                if shared_example_name
                else None
            ),
            "sparse_labels": (
                {
                    "file_name": sparse_example["file_name"],
                    "split": sparse_example["split"],
                    "image_id": sparse_example["image_id"],
                    "count": sparse_example["count"],
                    "sample_annotation": sparse_sample,
                }
                if sparse_example
                else None
            ),
        },
        "outputs": {
            "tables": ["tables/images.jsonl", "tables/annotations.jsonl"],
            "findings": "findings.json",
            "manifests": ["manifests/sources.json", "manifests/val_membership.json"],
        },
    }
    _dump_json(os.path.join(manifests_dir, "sources.json"), sources)

    elapsed = time.perf_counter() - started
    run_info = {
        "tool": {"name": "livecell-audit", "version": __version__},
        "command": ["audit"],
        "data_root": os.path.abspath(config.data_root),
        "out_dir": os.path.abspath(config.out_dir),
        "splits": list(config.splits),
        "low_count_threshold": config.low_count_threshold,
        "check_dimensions": config.check_dimensions,
        "runtime_seconds": round(elapsed, 3),
        "runtime_method": (
            "time.perf_counter() around streaming, analysis and table writes "
            "(excludes only the final small JSON dumps)"
        ),
        "peak_memory_mb": (
            round(_peak_rss_mb(), 3) if _peak_rss_mb() is not None else None
        ),
        "peak_memory_method": _MEMORY_METHOD,
    }
    report["extra_within_split_records"] = sum(
        v["extra_filename_records"] for v in splits_summary.values()
    )
    report["run"] = run_info
    _dump_json(os.path.join(config.out_dir, "report.json"), report)
    _dump_json(os.path.join(config.out_dir, "run.json"), run_info)
    return report
