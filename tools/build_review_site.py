#!/usr/bin/env python3
"""Build the static review website in ``web/review`` from verified outputs.

The website is a *projection* of already-verified files. Nothing is measured,
fitted or inferred here: the script reads the completed audit outputs, the
completed ranking run and the completed evaluation run, selects a bounded
illustrated example set with a documented deterministic rule, and writes the
static site (HTML is hand-written; this script writes only ``data/``,
``downloads/``, the copied guided-overview images and the copied preview PNGs).

Reads, from one completed release run ``RUN`` (all required unless noted):

- ``RUN/audit/report.json``             audit counts, examples
- ``RUN/audit/findings.json``           the 157 audit findings
- ``RUN/audit/manifests/sources.json``  COCO source path + SHA-256 per split
- ``RUN/site-inputs/ranking-fixed-mem/candidates.jsonl``  review pool rows
- ``RUN/site-inputs/ranking-fixed-mem/summary.json``      ranking provenance
- ``RUN/site-inputs/ranking-fixed-mem/model_card.json``   frozen configuration
- ``RUN/site-inputs/evaluation-fixed-mem/evaluation_summary.json``
- ``RUN/site-inputs/evaluation-fixed-mem/known_cases.json``
- ``RUN/site-inputs/evaluation-fixed-mem/review_sample.json``
- ``RUN/site-inputs/previews/manifest.json``   (optional, server render)
- ``RUN/site-inputs/previews/polygons.json``   (optional, server render)

Writes ``data/site-data.js`` (``window.LIVECELL_REVIEW``),
``data/rankings.js`` (``window.LIVECELL_RANKINGS``), ``downloads/`` and, when
the server previews are present, ``previews/``.

Example (normally run by ``tools/run_release.sh``):
    python3 tools/build_review_site.py \\
        --audit-dir "$RUN/audit" \\
        --inputs "$RUN/site-inputs" \\
        --out web/review
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics

RANK_DIR = "ranking-fixed-mem"
EVAL_DIR = "evaluation-fixed-mem"

#: Upper bound on illustrated examples (one PNG preview each). The task allows
#: 25-40; the server renderer enforces the same cap.
MAX_EXAMPLES = 40
MIN_EXAMPLES = 25

#: Outlines drawn per dense image. Matches tools/make_previews.py.
OUTLINE_CAP = 20

#: The deterministic selection rule. Order matters: earlier steps win, a row is
#: never selected twice, and every step is a stable sort over the pool.
SELECTION_STEPS = (
    ("known_case", 9, "upstream-reported sparse-label cases (issue #21)"),
    ("top_isolation_forest", 10, "highest Isolation Forest priority (seed 42)"),
    ("top_robust_baseline", 5, "highest robust statistical-baseline priority"),
    ("top_low_count_baseline", 5, "fewest supplied annotations after the known cases"),
    ("review_sample_random", 6, "the frozen review sample's random stratum, in sample order"),
    ("alternate_annotation_version", 2, "repeated filenames with alternative annotation versions"),
    ("confirmed_cross_split_overlap", 2, "filenames present in both learning and checking groups"),
    ("cell_line_coverage", 8, "top Isolation Forest row for any cell line not yet covered"),
)

#: Feature units, copied from the authoritative ``FEATURE_DICTIONARY`` in
#: ``src/livecell_audit/features.py``. ``check_review_site.py`` asserts each
#: string still occurs in that file.
FEATURE_UNITS = {
    "n_annotations": "count",
    "area_sum": "pixels^2",
    "area_mean": "pixels^2",
    "area_median": "pixels^2",
    "area_min": "pixels^2",
    "area_max": "pixels^2",
    "area_std": "pixels^2",
    "bbox_width_mean": "pixels",
    "bbox_width_median": "pixels",
    "bbox_height_mean": "pixels",
    "bbox_height_median": "pixels",
    "n_points_mean": "points",
    "n_points_median": "points",
    "n_points_min": "points",
    "n_points_max": "points",
    "n_points_sum": "points",
    "pixel_mean": "intensity (0-255)",
    "pixel_std": "intensity (0-255)",
    "pixel_min": "intensity (0-255)",
    "pixel_max": "intensity (0-255)",
    "gradient_mean": "intensity/pixel",
    "width": "pixels",
    "height": "pixels",
    "image_area_pixels": "pixels",
    "time_hours": "hours",
    "versions_total": "count",
    "versions_in_split": "count",
    "records_for_filename": "count",
    "splits_for_filename": "count",
}

#: Feature values shown per example, in display order.
EVIDENCE_FEATURES = (
    ("n_annotations", "Supplied cell markings"),
    ("area_median", "Median marking area"),
    ("area_mean", "Mean marking area"),
    ("area_std", "Marking area spread (sample std)"),
    ("bbox_width_median", "Median marking width"),
    ("bbox_height_median", "Median marking height"),
    ("n_points_median", "Median polygon points per marking"),
    ("pixel_mean", "Mean pixel intensity"),
    ("pixel_std", "Pixel intensity spread"),
    ("gradient_mean", "Mean gradient magnitude"),
    ("width", "Recorded image width"),
    ("height", "Recorded image height"),
    ("time_hours", "Acquisition time after plating"),
    ("records_for_filename", "Records sharing this filename"),
    ("splits_for_filename", "Distinct splits using this filename"),
)

#: Secondary statistics used to describe an observable review reason.
REASON_FEATURES = (
    "area_median",
    "area_mean",
    "area_std",
    "bbox_width_median",
    "bbox_height_median",
    "n_points_median",
    "pixel_mean",
    "pixel_std",
    "gradient_mean",
)

#: Columns of the full ranking projection (browser table + CSV download).
RANKING_COLUMNS = (
    "record_key",
    "file_name",
    "split",
    "cell_line",
    "partition",
    "image_id",
    "n_annotations",
    "is_known_case",
    "is_alternate_version",
    "is_cross_split_overlap",
    "low_count_rank",
    "low_count_raw",
    "robust_rank",
    "robust_priority",
    "robust_raw",
    "if_rank_seed7",
    "if_rank_seed42",
    "if_rank_seed101",
    "if_priority_seed7",
    "if_priority_seed42",
    "if_priority_seed101",
    "if_raw_seed42",
    "random_rank_seed7",
    "random_rank_seed42",
    "random_rank_seed101",
    "width",
    "height",
    "versions_total",
    "records_for_filename",
    "splits_for_filename",
    "annotation_join_status",
)

CSV_HEADER_LABELS = {
    "record_key": "record_key (split:record_position)",
    "file_name": "file_name",
    "split": "split",
    "cell_line": "cell_line",
    "partition": "partition",
    "image_id": "image_id (COCO)",
    "n_annotations": "n_annotations (count)",
    "is_known_case": "is_known_case",
    "is_alternate_version": "is_alternate_version",
    "is_cross_split_overlap": "is_cross_split_overlap",
    "low_count_rank": "low_count_rank (1 = fewest annotations)",
    "low_count_raw": "low_count_raw (-n_annotations)",
    "robust_rank": "robust_rank",
    "robust_priority": "robust_priority (fit-only percentile)",
    "robust_raw": "robust_raw (mean absolute robust z)",
    "if_rank_seed7": "if_rank_seed7",
    "if_rank_seed42": "if_rank_seed42",
    "if_rank_seed101": "if_rank_seed101",
    "if_priority_seed7": "if_priority_seed7",
    "if_priority_seed42": "if_priority_seed42 (default seed)",
    "if_priority_seed101": "if_priority_seed101",
    "if_raw_seed42": "if_raw_seed42 (-score_samples)",
    "random_rank_seed7": "random_rank_seed7",
    "random_rank_seed42": "random_rank_seed42",
    "random_rank_seed101": "random_rank_seed101",
    "width": "width (pixels)",
    "height": "height (pixels)",
    "versions_total": "versions_total (count)",
    "records_for_filename": "records_for_filename (count)",
    "splits_for_filename": "splits_for_filename (count)",
    "annotation_join_status": "annotation_join_status",
}

FINDING_LABELS = {
    "low_annotation_count": "Needs label review",
    "cross_split_overlap": "Confirmed overlap",
    "alternate_annotation_version": "Alternative annotation versions",
    "review_priority": "Review priority (no upstream report)",
}

REVIEW_PRIORITY_ACTION = (
    "Review the image and its labels against the four published review criteria, "
    "then record a judgment and reason. No judgment is supplied: the frozen review "
    "sample is awaiting a scientific reviewer."
)


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def median_or_none(values):
    clean = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return statistics.median(clean) if clean else None


def mad_or_none(values):
    clean = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not clean:
        return None
    med = statistics.median(clean)
    return statistics.median([abs(v - med) for v in clean])


def round_or_none(value, digits=4):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return round(value, digits)


class Inputs:
    """Verified inputs, with the provenance recorded for the site footer."""

    def __init__(self, audit_dir, inputs_dir):
        self.audit_dir = audit_dir
        self.inputs_dir = inputs_dir
        self.rank_dir = os.path.join(inputs_dir, RANK_DIR)
        self.eval_dir = os.path.join(inputs_dir, EVAL_DIR)
        self.files = []  # (label, path, role)

    def _read(self, path, label, role):
        if not os.path.isfile(path):
            raise SystemExit(f"build_review_site: error: missing input {path} ({label})")
        self.files.append((label, path, role))
        return load_json(path)

    def load(self):
        self.report = self._read(
            os.path.join(self.audit_dir, "report.json"), "Audit report", "audit"
        )
        self.findings = self._read(
            os.path.join(self.audit_dir, "findings.json"), "Audit findings", "audit"
        )
        self.sources = self._read(
            os.path.join(self.audit_dir, "manifests", "sources.json"),
            "Audit source manifest",
            "audit",
        )
        self.ranking_summary = self._read(
            os.path.join(self.rank_dir, "summary.json"), "Ranking summary", "ranking"
        )
        self.model_card = self._read(
            os.path.join(self.rank_dir, "model_card.json"), "Ranking model card", "ranking"
        )
        self.evaluation = self._read(
            os.path.join(self.eval_dir, "evaluation_summary.json"),
            "Evaluation summary",
            "evaluation",
        )
        self.known_cases = self._read(
            os.path.join(self.eval_dir, "known_cases.json"), "Known cases", "evaluation"
        )
        self.review_sample = self._read(
            os.path.join(self.eval_dir, "review_sample.json"), "Review sample", "evaluation"
        )
        candidates = os.path.join(self.rank_dir, "candidates.jsonl")
        if not os.path.isfile(candidates):
            raise SystemExit(f"build_review_site: error: missing input {candidates}")
        self.files.append(("Review-pool candidates (JSONL)", candidates, "ranking"))
        rows = []
        with open(candidates, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        self.candidates = rows
        return self

    def validate(self):
        """Checks that make a mixed or stale input set fail loudly."""
        report, summary, card = self.report, self.ranking_summary, self.model_card
        problems = []
        if report.get("unique_filenames") != summary.get("counts", {}).get(
            "canonical_filenames"
        ):
            problems.append(
                "audit unique_filenames != ranking canonical_filenames "
                f"({report.get('unique_filenames')} != "
                f"{summary.get('counts', {}).get('canonical_filenames')})"
            )
        source_sha = {
            split: entry.get("sha256")
            for split, entry in (self.sources.get("coco_files") or {}).items()
        }
        ranking_sha = summary.get("provenance", {}).get("source_sha256") or {}
        if source_sha != ranking_sha:
            problems.append("audit COCO source hashes != ranking provenance source hashes")
        if card.get("inputs", {}).get("source_sha256") != ranking_sha:
            problems.append("model card source hashes != ranking provenance source hashes")
        if self.known_cases.get("pool_size") != summary.get("counts", {}).get(
            "review_pool_candidates"
        ):
            problems.append("known-case pool size != ranking review_pool_candidates")
        if len(self.candidates) != self.known_cases.get("pool_size"):
            problems.append(
                f"candidates.jsonl rows ({len(self.candidates)}) != known-case pool size "
                f"({self.known_cases.get('pool_size')})"
            )
        known = sum(1 for row in self.candidates if row.get("is_known_case"))
        if known != self.known_cases.get("known_cases_in_pool"):
            problems.append("known-case rows in candidates != known_cases_in_pool")
        if self.evaluation.get("human_review", {}).get("real_world_precision") is not None:
            problems.append("evaluation claims a real-world precision value")
        for entry in self.review_sample.get("records", []):
            if entry.get("judgment") is not None:
                problems.append("review sample contains a fabricated judgment")
                break
        if problems:
            raise SystemExit(
                "build_review_site: error: inconsistent verified inputs:\n  - "
                + "\n  - ".join(problems)
            )
        return self


def select_examples(candidates, review_sample):
    """Deterministic illustrated-example selection (see SELECTION_STEPS)."""
    by_key = {row["record_key"]: row for row in candidates}
    sample_random = [
        by_key[rec["record_key"]]
        for rec in review_sample["records"]
        if rec.get("stratum") == "random_sample" and rec.get("record_key") in by_key
    ]

    def by_rank(column):
        return sorted(
            candidates,
            key=lambda row: (
                row.get(column) if isinstance(row.get(column), (int, float)) else 10**9,
                row["file_name"],
                row["record_key"],
            ),
        )

    orders = {
        "known_case": [row for row in candidates if row.get("is_known_case")],
        "top_isolation_forest": by_rank("if_rank"),
        "top_robust_baseline": by_rank("robust_rank"),
        "top_low_count_baseline": by_rank("low_count_rank"),
        "review_sample_random": sample_random,
        "alternate_annotation_version": [
            row for row in by_rank("if_rank") if row.get("is_alternate_version")
        ],
        "confirmed_cross_split_overlap": [
            row for row in by_rank("if_rank") if row.get("is_cross_split_overlap")
        ],
    }

    selected = []
    reasons = {}
    for name, limit, _label in SELECTION_STEPS:
        if name == "cell_line_coverage":
            continue
        taken = 0
        for row in orders[name]:
            if taken >= limit:
                break
            if row["record_key"] in reasons:
                continue
            reasons[row["record_key"]] = name
            selected.append(row)
            taken += 1

    covered = {row["cell_line"] for row in selected}
    for cell_line in sorted({row["cell_line"] for row in candidates}):
        if cell_line in covered:
            continue
        for row in by_rank("if_rank"):
            if row["cell_line"] == cell_line:
                reasons[row["record_key"]] = "cell_line_coverage"
                selected.append(row)
                covered.add(cell_line)
                break

    if not MIN_EXAMPLES <= len(selected) <= MAX_EXAMPLES:
        raise SystemExit(
            f"build_review_site: error: selection produced {len(selected)} examples, "
            f"outside the allowed {MIN_EXAMPLES}-{MAX_EXAMPLES}"
        )
    return selected, reasons


def build_finding_index(findings):
    index = {}
    for finding in findings:
        index.setdefault(finding["file_name"], finding)
    return index


def observable_reason(row, stats):
    """Largest robust deviation from the pool median, as a factual statement."""
    best = None
    for name in REASON_FEATURES:
        value = row.get(name)
        entry = stats.get(name) or {}
        med = entry.get("median")
        mad = entry.get("mad")
        if not isinstance(value, (int, float)) or med is None or not mad:
            continue
        z = abs(value - med) / (1.4826 * mad)
        if best is None or z > best[0]:
            best = (z, name, value, med)
    if best is None:
        return None, []
    z, name, value, med = best
    return (
        {
            "feature": name,
            "value": round(value, 4),
            "pool_median": round(med, 4),
            "robust_z": round(z, 3),
            "unit": FEATURE_UNITS.get(name, ""),
        },
        [
            f"Largest observable difference: {name} = {round(value, 4)} "
            f"({FEATURE_UNITS.get(name, '')}) against a review-pool median of "
            f"{round(med, 4)}.",
            f"Robust deviation {round(z, 2)}x MAD (1.4826 x median absolute deviation). "
            "This is a measured difference, not a proven cause.",
        ],
    )


def uncertainty_for(kind, row, finding, recovery):
    lines = []
    if kind == "low_annotation_count":
        lines.append(
            "A low label count alone is not proof of an error: the image may be "
            "partly confluent, or an annotator may have marked a representative subset."
        )
        lines.append(
            "These nine cases are the images the upstream reporters flagged, so "
            "recovering them is a regression check, not a measurement of detector quality."
        )
    elif kind == "cross_split_overlap":
        lines.append(
            "The same source image appears in both the learning and checking groups. "
            "The effect on model scores has not been measured in this project."
        )
        lines.append(
            "Overlap was found by matching filenames; identical pixels were not compared."
        )
    elif kind == "alternate_annotation_version":
        lines.append(
            "Two records share this filename with different COCO image ids and "
            "different annotation counts. Upstream retains alternative annotation "
            "versions, so this is not automatically a corrupt record."
        )
        lines.append(
            "This project does not decide which version is authoritative; the choice "
            "and its policy are left to a reviewer."
        )
    else:
        lines.append(
            "Priority is a fit-only empirical percentile: a relative review order, "
            "not the probability that a label is wrong."
        )
        lines.append(
            "The Isolation Forest score is an unusualness indicator. A feature "
            "difference is not a proven causal explanation of the score."
        )
    if row.get("is_known_case"):
        lines.append(
            "This image is one of the nine upstream-reported cases, so its label "
            "count is already known to be low. Do not read it as new evidence."
        )
    if row.get("is_alternate_version"):
        lines.append(
            "This filename has more than one record in its split, so the annotation "
            "count shown belongs to the canonical version only."
        )
    if recovery:
        lines.append(
            f"At this budget the selected method recovered {recovery['recovered']} of "
            f"{recovery['total']} known cases in the 3,067-row review pool "
            f"(seed spread {recovery['spread']})."
        )
    return lines


def finding_summary(finding):
    if not finding:
        return None
    return {
        "type": finding["type"],
        "status": finding["status"],
        "title": finding["title"],
        "recommendation": finding["recommendation"],
        "note": (finding.get("evidence") or {}).get("note"),
        "records": [
            {"split": split, "counts": rec.get("counts"), "image_ids": rec.get("image_ids")}
            for split, rec in sorted((finding.get("records") or {}).items())
        ],
        "finding_id": finding["finding_id"],
    }


def build_example(row, reason, finding, preview, stats, recovery):
    kind = (
        "cross_split_overlap"
        if row.get("is_cross_split_overlap")
        else "alternate_annotation_version"
        if row.get("is_alternate_version")
        else "low_annotation_count"
        if row.get("is_known_case")
        else "review_priority"
    )
    evidence_note, reason_lines = observable_reason(row, stats)
    features = {}
    for name, _label in EVIDENCE_FEATURES:
        value = row.get(name)
        if isinstance(value, bool):
            value = int(value)
        features[name] = round(value, 4) if isinstance(value, float) else value
    ranks = {
        "low_count": {
            "seed42": row.get("low_count_rank"),
            "priority_seed42": round_or_none(row.get("low_count_raw")),
        },
        "robust": {
            "seed42": row.get("robust_rank"),
            "priority_seed42": round_or_none(row.get("robust_priority")),
        },
        "isolation_forest": {
            "seed7": row.get("if_rank_seed7"),
            "seed42": row.get("if_rank_seed42"),
            "seed101": row.get("if_rank_seed101"),
            "priority_seed42": round_or_none(row.get("if_priority_seed42")),
        },
        "random": {
            "seed7": row.get("random_rank_seed7"),
            "seed42": row.get("random_rank_seed42"),
            "seed101": row.get("random_rank_seed101"),
            "priority_seed42": round_or_none(row.get("random_priority_seed42")),
        },
    }
    finding_out = finding_summary(finding)
    return {
        "id": preview["preview_id"],
        "record_key": row["record_key"],
        "file_name": row["file_name"],
        "split": row["split"],
        "image_id": row["image_id"],
        "cell_line": row["cell_line"],
        "partition": row["partition"],
        "acquisition_group": row.get("acquisition_group"),
        "is_known_case": bool(row.get("is_known_case")),
        "is_alternate_version": bool(row.get("is_alternate_version")),
        "is_cross_split_overlap": bool(row.get("is_cross_split_overlap")),
        "finding_type": kind,
        "finding_label": FINDING_LABELS[kind],
        "selection_reason": reason,
        "ranks": ranks,
        "n_annotations": row.get("n_annotations"),
        "features": features,
        "evidence_features": [
            {"name": name, "label": label, "value": features.get(name),
             "unit": FEATURE_UNITS.get(name, "")}
            for name, label in EVIDENCE_FEATURES
        ],
        "finding": finding_out,
        "observable_reason": evidence_note,
        "observable_reason_text": reason_lines,
        "uncertainty": uncertainty_for(kind, row, finding_out, recovery),
        "next_action": (finding_out or {}).get("recommendation") or REVIEW_PRIORITY_ACTION,
        "preview": preview,
    }


def preview_id(split, image_id):
    return f"{split}_{image_id}"


def build_previews(inputs, out_dir):
    """Copy the server-rendered previews when present; otherwise say so."""
    preview_dir = os.path.join(inputs.inputs_dir, "previews")
    manifest_path = os.path.join(preview_dir, "manifest.json")
    polygons_path = os.path.join(preview_dir, "polygons.json")
    when = None
    manifest = None
    polygons = {"examples": {}}
    if os.path.isfile(manifest_path):
        manifest = load_json(manifest_path)
        inputs.files.append(("Preview manifest (server render)", manifest_path, "preview"))
        when = manifest.get("generated_utc") or manifest.get("generated_at")
    if os.path.isfile(polygons_path):
        polygons = load_json(polygons_path)
        inputs.files.append(("Preview polygons (server render)", polygons_path, "preview"))
    entries = {}
    png_provenance = []
    if manifest:
        for entry in manifest.get("previews", []):
            entries[entry["preview_id"]] = entry
        target = os.path.join(out_dir, "previews")
        os.makedirs(target, exist_ok=True)
        for entry in manifest.get("previews", []):
            name = entry["file"]
            source = os.path.join(preview_dir, name)
            if not os.path.isfile(source):
                raise SystemExit(
                    f"build_review_site: error: preview manifest names {name} but it is missing"
                )
            destination = os.path.join(target, name)
            shutil.copyfile(source, destination)
            digest = sha256_file(destination)
            declared = entry.get("png_sha256")
            if declared and declared != digest:
                raise SystemExit(
                    f"build_review_site: error: copied preview {name} does not match the "
                    f"manifest SHA-256 ({declared} != {digest})"
                )
            png_provenance.append(
                {
                    "label": f"Preview PNG ({entry['preview_id']})",
                    "role": "preview-png",
                    "path": os.path.relpath(destination, os.getcwd()).replace(os.sep, "/"),
                    "bytes": os.path.getsize(destination),
                    "sha256": digest,
                    "preview_id": entry["preview_id"],
                    "source_file": entry.get("source_file"),
                    "tiff_sha256": entry.get("tiff_sha256"),
                }
            )
            layer = entry.get("labels_layer")
            if layer:
                layer_source = os.path.join(preview_dir, layer["file"])
                if not os.path.isfile(layer_source):
                    raise SystemExit(
                        f"build_review_site: error: preview manifest names {layer['file']} "
                        "but it is missing"
                    )
                layer_destination = os.path.join(target, layer["file"])
                shutil.copyfile(layer_source, layer_destination)
                layer_digest = sha256_file(layer_destination)
                if layer.get("png_sha256") and layer["png_sha256"] != layer_digest:
                    raise SystemExit(
                        f"build_review_site: error: copied label layer {layer['file']} does not "
                        f"match the manifest SHA-256 ({layer['png_sha256']} != {layer_digest})"
                    )
                png_provenance.append(
                    {
                        "label": f"Label layer PNG ({entry['preview_id']})",
                        "role": "preview-labels-png",
                        "path": os.path.relpath(layer_destination, os.getcwd()).replace(os.sep, "/"),
                        "bytes": os.path.getsize(layer_destination),
                        "sha256": layer_digest,
                        "preview_id": entry["preview_id"],
                        "source_file": entry.get("source_file"),
                        "tiff_sha256": entry.get("tiff_sha256"),
                    }
                )
    guided_layers = []
    if manifest:
        target = os.path.join(out_dir, "previews")
        for entry in manifest.get("guided", []):
            source = os.path.join(preview_dir, entry["file"])
            if not os.path.isfile(source):
                raise SystemExit(
                    f"build_review_site: error: preview manifest names {entry['file']} but it is missing"
                )
            destination = os.path.join(target, entry["file"])
            shutil.copyfile(source, destination)
            digest = sha256_file(destination)
            if digest != entry.get("png_sha256"):
                raise SystemExit(
                    f"build_review_site: error: copied guided layer {entry['file']} does not match "
                    f"the manifest SHA-256 ({entry.get('png_sha256')} != {digest})"
                )
            guided_layers.append(
                {
                    "split": entry["split"],
                    "image_id": entry["image_id"],
                    "file_name": entry["file_name"],
                    "src": f"previews/{entry['file']}",
                    "labels": entry["outlines_drawn"],
                    "png_sha256": digest,
                }
            )
            png_provenance.append(
                {
                    "label": f"Guided label layer ({entry['split']} {entry['image_id']})",
                    "role": "guided-labels-png",
                    "path": os.path.relpath(destination, os.getcwd()).replace(os.sep, "/"),
                    "bytes": os.path.getsize(destination),
                    "sha256": digest,
                    "preview_id": f"{entry['split']}_{entry['image_id']}",
                }
            )
    status = "delivered" if manifest else "pending"
    return {
        "status": status,
        "guided_layers": guided_layers,
        "generated": when,
        "outline_cap": (manifest or {}).get("outline_cap", OUTLINE_CAP),
        "render": (manifest or {}).get("render"),
        "entries": entries,
        "polygons": polygons.get("examples", {}),
        "manifest_note": (manifest or {}).get("note"),
        "manifest_sha256": sha256_file(manifest_path) if manifest else None,
        "curated_sha256": (manifest or {}).get("curated_sha256"),
        "png_provenance": png_provenance,
        "source_dir": os.path.relpath(preview_dir, os.getcwd()).replace(os.sep, "/"),
    }


def build_distribution(candidates):
    stats = {}
    for name in REASON_FEATURES + ("n_annotations",):
        values = [row.get(name) for row in candidates]
        stats[name] = {
            "median": median_or_none(values),
            "mad": mad_or_none(values),
            "min": min([v for v in values if isinstance(v, (int, float))], default=None),
            "max": max([v for v in values if isinstance(v, (int, float))], default=None),
        }
    return stats


def build_methods(inputs):
    summary = inputs.ranking_summary
    card = inputs.model_card
    evaluation = inputs.evaluation
    known = inputs.known_cases
    synthetic = evaluation["synthetic_annotation_loss"]
    regression = evaluation["known_case_regression"]
    sample = inputs.review_sample
    definitions = [
        {
            "key": "low_count",
            "name": "Low annotation count",
            "plain": "Show the images whose label file contains the fewest cell markings.",
            "technical": (
                "Ranks by ascending n_annotations (raw score = -n_annotations), ties "
                "broken by filename then record key. No fitting and no seed: the same "
                "order every run."
            ),
            "default": True,
            "seeded": False,
        },
        {
            "key": "robust",
            "name": "Robust statistical baseline",
            "plain": (
                "Show the images whose label shape and size statistics differ most from "
                "the rest of the same cell line."
            ),
            "technical": (
                "Mean absolute robust z-score over 20 numeric features, standardised by "
                "the fit-only median and 1.4826 x MAD of the row's cell line; a zero MAD "
                "falls back to the fit-only global MAD. No seed."
            ),
            "default": False,
            "seeded": False,
        },
        {
            "key": "isolation_forest",
            "name": "Isolation Forest (ML)",
            "plain": "A machine-learning model scores how unusual each image looks overall.",
            "technical": (
                "sklearn Pipeline(median imputation, RobustScaler, IsolationForest "
                "n_estimators=200, contamination='auto', max_samples=min(256, n_fit), "
                "n_jobs=1) fitted per cell line on fit rows only, with a documented global "
                "fallback. Priority = fit-only empirical percentile of -score_samples."
            ),
            "default": False,
            "seeded": True,
        },
        {
            "key": "random",
            "name": "Seeded random reference",
            "plain": "A control: a repeatable random order with no information in it.",
            "technical": "Uniform random priority from the configured seed; a reference floor.",
            "default": False,
            "seeded": True,
        },
    ]
    method_labels = {
        "low_count": "Low annotation count",
        "robust": "Robust statistical baseline",
        "isolation_forest": "Isolation Forest (ML)",
        "random": "Seeded random reference",
    }
    recovery_table = []
    for method in summary["methods"]:
        row_method = {"method": method, "label": method_labels[method], "seeds": {}}
        for seed in ("7", "42", "101"):
            key = f"{method}_seed{seed}"
            if key in regression["recovery"]:
                row_method["seeds"][seed] = {
                    budget: regression["recovery"][key][f"at_{budget}"]
                    for budget in summary["protocol"]["budgets"]
                }
        recovery_table.append(row_method)
    return {
        "definitions": definitions,
        "method_labels": method_labels,
        "question": card["question"],
        "score_semantics": card["score_semantics"],
        "methods": list(summary["methods"]),
        "budgets": summary["protocol"]["budgets"],
        "default_seed": summary["protocol"]["default_seed"],
        "sensitivity_seeds": summary["protocol"]["sensitivity_seeds"],
        "pools": {
            "review": {
                "size": known["pool_size"],
                "definition": known["pool_definition"],
                "use": "The browsable example browser and the full ranking table use this pool.",
                "ranking_file": "ranking-fixed-mem/candidates.jsonl",
                "counts": summary["counts"],
            },
            "synthetic": {
                "size": synthetic["pool_size"],
                "definition": (
                    "untouched held-out training groups only, known cases excluded, each "
                    "physical image once"
                ),
                "use": "Only the simulated annotation-loss comparison uses this pool.",
            },
        },
        "partition": {
            "caveat": card["partition"]["caveat"],
            "n_fit_images": sum(
                entry["n_fit_images"] for entry in card["partition"]["cell_lines"].values()
            ),
            "cells": [
                {
                    "cell_line": name,
                    "n_fit_images": entry["n_fit_images"],
                    "n_holdout_images": entry["n_holdout_images"],
                    "n_groups": entry["n_groups"],
                    "n_known_case_records_excluded": entry["n_known_case_records_excluded"],
                }
                for name, entry in sorted(card["partition"]["cell_lines"].items())
            ],
        },
        "knownCase": {
            "total": known["known_cases_total"],
            "pool_size": known["pool_size"],
            "evidence_type": known["evidence_type"],
            "recovery_rule": known["recovery_rule"],
            "upstream_issue": known["upstream_issue"],
            "upstream_reporter": known["upstream_reporter"],
            "reported_identity_present": known["known_cases_reported_identity_present"],
            "canonical_matches_reported": known[
                "known_cases_canonical_matches_reported_identity"
            ],
            "in_fit": known["known_cases_in_fit"],
            "in_pool": known["known_cases_in_pool"],
            "recovery_table": recovery_table,
            "seed_spread": regression["recovery_seed_spread"],
            "baseline_counts": known["baseline_counts"],
            "per_case": [
                {
                    "file_name": case["file_name"],
                    "split": case["canonical_split"],
                    "reported_image_id": case["reported_image_id"],
                    "canonical_image_id": case["canonical_image_id"],
                    "observed_annotations": case["observed_annotations"],
                    "record_key": case["record_key"],
                    "ranks": {
                        "low_count_seed42": case["ranks"].get("low_count_seed42"),
                        "robust_seed42": case["ranks"].get("robust_seed42"),
                        "isolation_forest_seed42": case["ranks"].get(
                            "isolation_forest_seed42"
                        ),
                        "random_seed42": case["ranks"].get("random_seed42"),
                    },
                }
                for case in sorted(known["cases"], key=lambda c: c["file_name"])
            ],
        },
        "synthetic": {
            "pool_size": synthetic["pool_size"],
            "injected_images": synthetic["injected_images"],
            "injected_fraction": synthetic["injected_fraction"],
            "retained_fraction": synthetic.get("retained_fraction"),
            "annotations_before": synthetic.get("annotations_before"),
            "annotations_after": synthetic.get("annotations_after"),
            "scenario": synthetic["loss_scenario"],
            "budgets": synthetic["budgets"],
            "metrics": synthetic["metrics"],
            "seed_summary": synthetic["seed_summary"],
        },
        "humanReview": {
            "status": sample["status"],
            "records": len(sample["records"]),
            "records_note": evaluation["human_review"]["records"],
            "real_world_precision": evaluation["human_review"]["real_world_precision"],
            "strata": sample["strata"],
            "sampling_method": sample["sampling_method"],
            "sampling_caveat": sample["sampling_caveat"],
            "reviewer_note": sample["reviewer_note"],
            "review_criteria": sample["review_criteria"],
            "reviewers": sample["reviewers"],
        },
        "evidenceNotes": evaluation["evidence_notes"],
        "limitations": card["limitations"],
        "model": {
            "fitted_cell_lines": card["fitted_cell_lines"],
            "global_fallback_rule": card["global_fallback_rule"],
            "protocol": card["protocol"],
            "feature_selection": card["feature_selection"],
            "annotation_reproduction": card["annotation_reproduction"],
        },
    }


def overlap_count_differences(overlap_findings):
    """How many cross-split overlap findings carry different label counts per split."""
    differing = 0
    for finding in overlap_findings:
        counts = {
            tuple(record.get("counts") or [])
            for record in (finding.get("records") or {}).values()
        }
        if len(counts) > 1:
            differing += 1
    return differing


def build_audit(inputs):
    report = inputs.report
    findings = inputs.findings
    by_type = {}
    for finding in findings:
        by_type.setdefault(finding["type"], []).append(finding)
    overlap = report["cross_split_filename_overlap"]
    return {
        "snapshot_date": report["snapshot_date"],
        "tool": report["tool"],
        "counts": {
            "distinct_filenames": report["unique_filenames"],
            "image_records": report["total_image_records"],
            "annotations": report["total_annotations"],
            "local_image_files": report["local_image_files"],
            "missing_image_filenames": len(report["missing_image_filenames"]),
            "extra_within_split_records": report["extra_within_split_records"],
            "low_count_threshold": report["low_count_threshold"],
        },
        "splits": [
            {
                "split": split,
                "image_records": data["image_records"],
                "distinct_filenames": data["image_records"] - len(data["repeated_filenames"]),
                "extra_records": data["extra_filename_records"],
                "annotations": data["annotations"],
                "records_at_most_10_annotations": data["images_at_most_10_annotations"],
            }
            for split, data in sorted(report["splits"].items())
        ],
        "overlap": {
            "train_val": overlap["train_val"]["filenames"],
            "train_test": overlap["train_test"]["filenames"],
            "val_test": overlap["val_test"]["filenames"],
            "files_sample": overlap["train_val"]["files"][:6],
            # Overlap findings whose train and validation records mark different
            # numbers of cells: the two groups hold different label versions.
            "differing_label_counts": overlap_count_differences(
                by_type.get("cross_split_overlap", [])
            ),
        },
        "findings": {
            "total": len(findings),
            "by_type": {
                kind: {"count": len(items), "status": items[0]["status"]}
                for kind, items in sorted(by_type.items())
            },
        },
        "examples": {
            "shared_image": report["examples"]["shared_image"],
            "sparse_labels": {
                key: value
                for key, value in report["examples"]["sparse_labels"].items()
            },
        },
        "known_sparse_cases": {
            "total": report["known_sparse_cases_total"],
            "recovered": report["known_sparse_cases_recovered"],
        },
        "source_files": {
            split: {
                "path": entry["path"],
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
            }
            for split, entry in sorted(inputs.sources["coco_files"].items())
        },
    }


def build_ranking_table(candidates):
    rows = []
    for row in sorted(candidates, key=lambda r: (r["low_count_rank"], r["file_name"])):
        rows.append([row.get(column) for column in RANKING_COLUMNS])
    return {"columns": list(RANKING_COLUMNS), "rows": rows}


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_js(path, global_name, payload):
    write_text(
        path,
        "window." + global_name + " = " + json.dumps(payload, sort_keys=True, separators=(",", ":")) + ";\n",
    )


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if any(ch in text for ch in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_rankings_csv(path, table):
    lines = [",".join(CSV_HEADER_LABELS[column] for column in table["columns"])]
    for row in table["rows"]:
        lines.append(",".join(csv_cell(cell) for cell in row))
    write_text(path, "\n".join(lines) + "\n")


def copy_guided_assets(out_dir, inputs, report):
    """Guided-overview images: the committed display copies in web/review/assets."""
    assets = os.path.join(out_dir, "assets")
    os.makedirs(assets, exist_ok=True)
    committed = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "web", "review", "assets")
    copied = {}
    mapping = [
        ("guided-shared.png", "Huh7_Phase_A10_2_00d00h00m_3.tif"),
        ("guided-sparse.png", "A172_Phase_A7_1_01d04h00m_3.tif"),
    ]
    for name, file_name in mapping:
        source = os.path.join(committed, name)
        if not os.path.isfile(source):
            raise SystemExit(
                f"build_review_site: error: missing committed display asset {source}"
            )
        destination = os.path.join(assets, name)
        if not (os.path.exists(destination) and os.path.samefile(source, destination)):
            shutil.copyfile(source, destination)
        copied[name] = {
            "file_name": file_name,
            "source": f"web/review/assets/{name}",
            "sha256": sha256_file(source),
            "note": (
                "Display copy of the original LIVECell TIFF, converted to PNG. It is a "
                "plain image with no overlay drawn in it."
            ),
        }
    return copied


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the static review website.")
    parser.add_argument("--audit-dir", required=True, help="a completed audit run (RUN/audit)")
    parser.add_argument("--inputs", required=True, help="the run's site inputs (RUN/site-inputs)")
    parser.add_argument("--out", default="web/review")
    args = parser.parse_args(argv)

    inputs = Inputs(args.audit_dir, args.inputs).load().validate()
    stats = build_distribution(inputs.candidates)
    selected, reasons = select_examples(inputs.candidates, inputs.review_sample)
    findings_index = build_finding_index(inputs.findings)
    previews = build_previews(inputs, args.out)

    recovery_lookup = {}
    regression = inputs.evaluation["known_case_regression"]
    known_recovery = inputs.known_cases["recovery"]
    for method in inputs.ranking_summary["methods"]:
        key = f"{method}_seed42"
        if key not in known_recovery:
            continue
        for budget in inputs.ranking_summary["protocol"]["budgets"]:
            entry = known_recovery[key][f"at_{budget}"]
            spread = regression["recovery_seed_spread"][method][f"at_{budget}"]
            recovery_lookup[(method, budget)] = {
                "recovered": entry["recovered"],
                "total": entry["known_cases_total"],
                "spread": f"{spread['min']:.0f}-{spread['max']:.0f}",
            }

    examples = []
    for row in selected:
        pid = preview_id(row["split"], row["image_id"])
        entry = previews["entries"].get(pid)
        polygons = previews["polygons"].get(pid)
        preview = {
            "preview_id": pid,
            "file": entry["file"] if entry else f"{pid}.png",
            "src": f"previews/{entry['file']}" if entry else None,
            "status": "delivered" if entry else "pending",
            "width": (entry or {}).get("source_width") or row.get("width"),
            "height": (entry or {}).get("source_height") or row.get("height"),
            "tiff_sha256": (entry or {}).get("tiff_sha256"),
            "png_sha256": (entry or {}).get("png_sha256"),
            "source_file": (entry or {}).get("source_file"),
            "source_bytes": (entry or {}).get("source_bytes"),
            "polygons_total": (polygons or {}).get("polygons_total"),
            "polygons_drawn": (polygons or {}).get("polygons_drawn"),
            "polygons": (polygons or {}).get("polygons"),
            "outline_cap": previews["outline_cap"],
            "render_parameters": (entry or {}).get("render_parameters"),
        }
        layer = (entry or {}).get("labels_layer")
        if layer:
            preview["labels_src"] = f"previews/{layer['file']}"
            preview["labels_png_sha256"] = layer.get("png_sha256")
            preview["labels_drawn"] = layer.get("outlines_drawn")
        if preview["polygons_total"] is not None and preview["polygons_drawn"] is not None:
            if layer and layer.get("outlines_drawn") == preview["polygons_total"]:
                preview["cap_note"] = (
                    f"Showing all {preview['polygons_total']:,} supplied labels."
                )
            elif preview["polygons_drawn"] < preview["polygons_total"]:
                preview["cap_note"] = (
                    f"Showing {preview['polygons_drawn']} of {preview['polygons_total']:,} "
                    "supplied labels (deterministic cap, source order); the image has "
                    "more labels than are drawn."
                )
            elif preview["polygons_total"] == 1:
                preview["cap_note"] = "Showing the single supplied label."
            else:
                preview["cap_note"] = (
                    f"Showing all {preview['polygons_total']} supplied labels."
                )
        examples.append(
            build_example(
                row,
                reasons[row["record_key"]],
                findings_index.get(row["file_name"]),
                preview,
                stats,
                recovery_lookup.get(("low_count", 42)),
            )
        )

    guided_assets = copy_guided_assets(args.out, inputs, inputs.report)
    rankings = build_ranking_table(inputs.candidates)

    provenance_files = []
    for label, path, role in inputs.files:
        provenance_files.append(
            {
                "label": label,
                "role": role,
                "path": os.path.relpath(path, os.getcwd()).replace(os.sep, "/"),
                "bytes": os.path.getsize(path),
                "sha256": sha256_file(path),
            }
        )
    for name, meta in sorted(guided_assets.items()):
        provenance_files.append(
            {
                "label": f"Guided example image ({meta['file_name']})",
                "role": "asset",
                "path": os.path.relpath(
                    os.path.join(args.out, "assets", name), os.getcwd()
                ).replace(os.sep, "/"),
                "bytes": os.path.getsize(
                    os.path.join(args.out, "assets", name)
                ),
                "sha256": meta["sha256"],
                "source": meta["source"],
                "note": meta["note"],
            }
        )
    provenance_files.extend(previews["png_provenance"])

    site = {
        "meta": {
            "title": "LIVECell Audit — a review website",
            "snapshot_date": inputs.report["snapshot_date"],
            "generated_by": "tools/build_review_site.py",
            "preview_status": previews["status"],
            "preview_generated": previews["generated"],
            "outline_cap": previews["outline_cap"],
            "bundle_note": (
                "A static projection of verified outputs: every number on this site is "
                "copied from one of the input files listed in the technical details."
            ),
        },
        "audit": build_audit(inputs),
        "distribution": {
            name: {
                "median": round_or_none(values["median"]),
                "mad": round_or_none(values["mad"]),
                "min": values["min"],
                "max": values["max"],
            }
            for name, values in stats.items()
        },
        "exampleSelection": {
            "rule": [
                {"step": name, "limit": limit, "description": label}
                for name, limit, label in SELECTION_STEPS
            ],
            "count": len(examples),
            "pool_size": len(inputs.candidates),
            "min": MIN_EXAMPLES,
            "max": MAX_EXAMPLES,
            "statement": (
                f"{len(examples)} of the {len(inputs.candidates)} review-pool candidates are "
                "illustrated in this bundle. Selection is a documented deterministic rule, "
                "not a claim that these are the only images worth reviewing."
            ),
        },
        "examples": examples,
        "guided": {
            "overlap_file": inputs.report["examples"]["shared_image"]["file_name"],
            "overlap_records": inputs.report["examples"]["shared_image"],
            "overlap_layers": [
                layer for layer in previews["guided_layers"]
                if layer["file_name"] == inputs.report["examples"]["shared_image"]["file_name"]
            ],
            "overlap_finding": finding_summary(
                findings_index.get(inputs.report["examples"]["shared_image"]["file_name"])
            ),
            "sparse_file": "A172_Phase_A7_1_01d04h00m_3.tif",
            "sparse_image_id": inputs.report["examples"]["sparse_labels"]["image_id"],
            "sparse_annotation": inputs.report["examples"]["sparse_labels"]["sample_annotation"],
            "sparse_count": inputs.report["examples"]["sparse_labels"]["count"],
            "sparse_finding": finding_summary(
                findings_index.get(inputs.report["examples"]["sparse_labels"]["file_name"])
            ),
            "assets": guided_assets,
        },
        "methods": build_methods(inputs),
        "glossary": [
            {"term": "Dataset", "meaning": "A collection of images and their cell labels."},
            {"term": "Label", "meaning": "An outline that tells the model where a cell is. Researchers call it an annotation."},
            {"term": "Train split", "meaning": "Images the model learns from."},
            {"term": "Validation split", "meaning": "Images used to check the model's progress."},
            {"term": "Test split", "meaning": "Held-back images; ranked here only for exploration."},
            {"term": "Data leakage", "meaning": "The same image was used for learning and checking."},
            {"term": "Duplicate record", "meaning": "The same photo has more than one record; label versions may differ."},
            {"term": "Review pool", "meaning": "Every canonical image record the model did not learn from (3,067)."},
            {"term": "Priority", "meaning": "A relative review order, not a probability that a label is wrong."},
            {"term": "Precision@K", "meaning": "Of the K highest-priority images, the share that were injected defects in the simulation."},
            {"term": "Isolation Forest", "meaning": "A machine-learning method that scores how unusual a row looks."},
        ],
        "provenance": {
            "files": provenance_files,
            "chain": [
                "The audit read the three COCO annotation files and the local TIFFs.",
                "The ranking run consumed the warehouse/feature build made from those same hashes.",
                "The evaluation run consumed that exact ranking run.",
                "This site consumed the three completed outputs above.",
            ],
            "source_hashes": inputs.ranking_summary["provenance"]["source_sha256"],
            "feature_manifest_sha256": inputs.ranking_summary["provenance"][
                "feature_manifest_sha256"
            ],
            "warehouse_manifest_sha256": inputs.ranking_summary["provenance"][
                "warehouse_manifest_sha256"
            ],
            "annotation_reproduction": {
                key: inputs.ranking_summary["provenance"][key]
                for key in ("annotation_reproduction_max_abs_diff",
                            "annotation_reproduction_max_abs_diff_sql")
            },
            "preview_manifest_sha256": previews["manifest_sha256"],
            "preview_curated_sha256": previews["curated_sha256"],
            "preview_render": previews["render"],
            "preview_source_dir": previews["source_dir"],
            "preview_png_count": sum(
                1 for item in previews["png_provenance"] if item["role"] == "preview-png"
            ),
            "preview_labels_count": sum(
                1 for item in previews["png_provenance"] if item["role"] == "preview-labels-png"
            ),
            "note": (
                "The site bundle contains no raw TIFF images, no database and no model files; "
                "the review pool projection is the only large table. The illustrated previews are "
                "downsampled display copies rendered on the data server from the source TIFFs; each "
                "one's PNG SHA-256 and its source TIFF SHA-256 are listed above. Dense images also "
                "have a transparent label layer with every supplied outline. Those display copies "
                "and label layers are the only image files in the bundle."
            ),
        },
    }

    data_dir = os.path.join(args.out, "data")
    write_js(os.path.join(data_dir, "site-data.js"), "LIVECELL_REVIEW", site)
    write_js(os.path.join(data_dir, "rankings.js"), "LIVECELL_RANKINGS", rankings)
    write_text(
        os.path.join(data_dir, "curated-examples.json"),
        json.dumps(
            {
                "schema_version": 1,
                "generated_by": "tools/build_review_site.py",
                "purpose": (
                    "Input for tools/make_previews.py: exactly the illustrated example set "
                    "so the server render and this website cannot drift apart."
                ),
                "pool": "ranking-fixed-mem/candidates.jsonl (review pool)",
                "pool_size": len(inputs.candidates),
                "outline_cap": OUTLINE_CAP,
                "selection_rule": [
                    {"step": name, "limit": limit, "description": label}
                    for name, limit, label in SELECTION_STEPS
                ],
                "count": len(examples),
                "guided": [
                    {"split": split, "image_id": image_id,
                     "file_name": inputs.report["examples"]["shared_image"]["file_name"]}
                    for split, key in (("train", "train_image_ids"), ("val", "val_image_ids"))
                    for image_id in inputs.report["examples"]["shared_image"][key][:1]
                ],
                "examples": [
                    {
                        "preview_id": example["id"],
                        "split": example["split"],
                        "image_id": example["image_id"],
                        "file_name": example["file_name"],
                        "record_key": example["record_key"],
                        "cell_line": example["cell_line"],
                        "n_annotations": example["n_annotations"],
                        "finding_type": example["finding_type"],
                        "selection_reason": example["selection_reason"],
                    }
                    for example in examples
                ],
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
    )

    downloads = os.path.join(args.out, "downloads")
    write_rankings_csv(os.path.join(downloads, "ranking-pool.csv"), rankings)
    download_map = [
        ("ranking-summary.json", os.path.join(inputs.rank_dir, "summary.json")),
        ("model-card.json", os.path.join(inputs.rank_dir, "model_card.json")),
        ("evaluation-summary.json", os.path.join(inputs.eval_dir, "evaluation_summary.json")),
        ("known-cases.json", os.path.join(inputs.eval_dir, "known_cases.json")),
        ("review-sample.json", os.path.join(inputs.eval_dir, "review_sample.json")),
        ("audit-findings.json", os.path.join(inputs.audit_dir, "findings.json")),
    ]
    os.makedirs(downloads, exist_ok=True)
    for target, source in download_map:
        shutil.copyfile(source, os.path.join(downloads, target))
    write_text(
        os.path.join(downloads, "RANKING-README.txt"),
        "LIVECell review ranking — download notes\n"
        "========================================\n\n"
        "ranking-pool.csv\n"
        "  Every row of the 3,067-image review pool with its rank in each method.\n"
        "  Units: n_annotations is a count; area features are pixels^2; bbox and\n"
        "  image dimensions are pixels; pixel statistics are 8-bit intensity\n"
        "  (0-255) used as stored, with no rescaling; time_hours is hours.\n"
        "  Priority columns are relative review priorities, NOT probabilities\n"
        "  that a label is wrong.\n\n"
        "ranking-summary.json, model-card.json, evaluation-summary.json,\n"
        "known-cases.json, review-sample.json, audit-findings.json\n"
        "  The verified source documents this site projects, copied unchanged so a\n"
        "  reader can check every number. The review sample's judgment fields are\n"
        "  blank: no scientific reviewer has been supplied and no expert judgment is\n"
        "  claimed.\n",
    )
    write_text(
        os.path.join(args.out, "THIRD_PARTY_NOTICES.txt"),
        "Third-party notices\n"
        "===================\n\n"
        "Images and annotations: LIVECell, Sartorius Corporate Research, licensed\n"
        "CC BY-NC 4.0 (https://creativecommons.org/licenses/by-nc/4.0/).\n"
        "Upstream software: MIT. Reference: Edlund et al., LIVECell, and\n"
        "https://github.com/sartorius-research/LIVECell#license\n\n"
        "Changes made for this site: TIFF images were converted to PNG and\n"
        "downsampled for display; supplied annotation polygons are drawn over the\n"
        "display copy as an overlay (for dense images, as a separate transparent\n"
        "label layer with every supplied outline); a viewing aid may be added to a\n"
        "very small polygon so a person can find it. The overlay marks the supplied\n"
        "label, not a cell boundary detected by this project. No upstream file is\n"
        "modified.\n\n"
        "Upstream reports this site reproduces (sartorius-research/LIVECell):\n"
        "  issue #26, train/validation overlap and duplicate records: OasisArtisan\n"
        "  issue #21, incomplete annotations: ivan-ea (maintainer reply: Rickard\n"
        "            Sjögren, Sartorius)\n"
        "  issue #14, alternate annotation versions: explained by Christoffer\n"
        "            Edlund (Sartorius)\n\n"
        "Typeface: Inter, copyright 2016 The Inter Project Authors\n"
        "(https://github.com/rsms/inter), licensed under the SIL Open Font License\n"
        "1.1 (assets/fonts/OFL-inter.txt); the latin variable subsets (as packaged\n"
        "by Fontsource) are included unmodified.\n\n"
        "Intended use: independent, noncommercial portfolio and education work.\n"
        "This project is not affiliated with, endorsed by, or connected to Sartorius.\n",
    )

    print(f"examples selected: {len(examples)} (cap {MAX_EXAMPLES})")
    print(f"preview status: {previews['status']}")
    print(f"ranking rows: {len(rankings['rows'])}")
    print(f"inputs recorded: {len(provenance_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
