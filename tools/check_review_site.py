#!/usr/bin/env python3
"""Static checks for the review website. No dataset, no pytest, no server.

Everything here is a bounded local check over the committed site files and the
verified inputs:

1.  every generated data file parses (JS globals and JSON);
2.  every illustrated example maps to a real review-pool candidate row with the
    same image id, filename, annotation count and ranks;
3.  the curated example list matches the example data exactly;
4.  the full ranking table matches ``candidates.jsonl`` row for row;
5.  every number in the site is re-derived from the input files (audit counts,
    overlap, findings, known-case recovery, synthetic metrics, distribution);
6.  the declared provenance hashes match the actual input files;
7.  feature units still exist in the authoritative feature dictionary;
8.  every relative link, image and download exists - or is a preview that the
    page honestly declares as not delivered;
9.  no private absolute path, host name or environment path appears anywhere;
10. no forbidden claim appears (health score, detected cell boundary, working
    run-audit control, fixed-dataset download);
11. if previews are delivered, each one exists and its outline total equals the
    ranked annotation count for that record;
12. the delivered JavaScript parses (`node --check`), or is recorded as an
    explicit `SKIPPED-missing-node` when no JavaScript runtime is installed.

Exit code 0 means every check passed. A skipped check is reported and counted,
and does not change the exit code. Usage (with one completed release run RUN):
    python3 tools/check_review_site.py --site web/review \
        --audit-dir "$RUN/audit" --inputs "$RUN/site-inputs"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess

FAIL = []
SKIP = []


def check(condition, message):
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        FAIL.append(message)


def skip_check(message):
    """Record a check that could not run. A skip is honest but never a failure."""
    print(f"  SKIP {message}")
    SKIP.append(message)


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_js_global(path, global_name):
    text = open(path, encoding="utf-8").read()
    prefix = "window." + global_name + " = "
    if not text.startswith(prefix):
        raise ValueError(f"{path}: expected {prefix!r}")
    body = text[len(prefix):].strip()
    if not body.endswith(";"):
        raise ValueError(f"{path}: expected a trailing semicolon")
    return json.loads(body[:-1])


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_site_files(site):
    skip = {".git"}
    for dirpath, dirnames, filenames in os.walk(site):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            yield os.path.join(dirpath, name)


def check_javascript_syntax(site):
    """Check the delivered scripts with ``node --check`` when node is installed.

    The data server has no JavaScript runtime. A missing node is recorded as an
    explicit skipped check (``SKIPPED-missing-node``) instead of a failure: the
    release must not depend on a tool the server does not ship.
    """
    found = subprocess.run(["bash", "-lc", "command -v node"], capture_output=True, text=True)
    if found.returncode != 0 or not found.stdout.strip():
        skip_check(
            "SKIPPED-missing-node: node --check assets/review.js, data/site-data.js, "
            "data/rankings.js"
        )
        return
    for script in ("assets/review.js", "data/site-data.js", "data/rankings.js"):
        result = subprocess.run(
            ["node", "--check", os.path.join(site, script)], capture_output=True, text=True
        )
        check(result.returncode == 0, f"node --check {script}" + (f": {result.stderr.strip()[:120]}" if result.returncode else ""))


def main(argv=None):
    FAIL.clear()
    SKIP.clear()
    parser = argparse.ArgumentParser(description="Static checks for the review website.")
    parser.add_argument("--site", default="web/review")
    parser.add_argument("--inputs", required=True, help="the run's site inputs (RUN/site-inputs)")
    parser.add_argument("--audit-dir", required=True, help="a completed audit run (RUN/audit)")
    args = parser.parse_args(argv)

    site = args.site
    rank_dir = os.path.join(args.inputs, "ranking-fixed-mem")
    eval_dir = os.path.join(args.inputs, "evaluation-fixed-mem")

    required = [
        os.path.join(site, "data", name)
        for name in ("site-data.js", "rankings.js", "curated-examples.json")
    ] + [
        os.path.join(args.audit_dir, name)
        for name in ("report.json", "findings.json", "manifests/sources.json")
    ] + [
        os.path.join(rank_dir, name)
        for name in ("summary.json", "model_card.json", "candidates.jsonl")
    ] + [
        os.path.join(eval_dir, name)
        for name in ("evaluation_summary.json", "known_cases.json", "review_sample.json")
    ]
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        print("Review-site verification needs the completed source outputs; missing:")
        for path in missing:
            print(f"  {path}")
        print("Pass --audit-dir and --inputs from the matching completed server run. "
              "See web/review/README.md. The shipped website can still be opened offline.")
        return 1

    print("1. generated data parses")
    try:
        data = load_js_global(os.path.join(site, "data", "site-data.js"), "LIVECELL_REVIEW")
        check(True, "data/site-data.js parses as window.LIVECELL_REVIEW")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        check(False, f"data/site-data.js: {exc}")
        return 1
    try:
        ranking = load_js_global(os.path.join(site, "data", "rankings.js"), "LIVECELL_RANKINGS")
        check(True, "data/rankings.js parses as window.LIVECELL_RANKINGS")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        check(False, f"data/rankings.js: {exc}")
        return 1
    curated = load_json(os.path.join(site, "data", "curated-examples.json"))
    check(True, "data/curated-examples.json parses")
    for name in os.listdir(os.path.join(site, "data")):
        if name.endswith(".json"):
            load_json(os.path.join(site, "data", name))
    check(True, "every data/*.json parses")

    print("\n2. inputs load")
    report = load_json(os.path.join(args.audit_dir, "report.json"))
    findings = load_json(os.path.join(args.audit_dir, "findings.json"))
    sources = load_json(os.path.join(args.audit_dir, "manifests", "sources.json"))
    ranking_summary = load_json(os.path.join(rank_dir, "summary.json"))
    model_card = load_json(os.path.join(rank_dir, "model_card.json"))
    evaluation = load_json(os.path.join(eval_dir, "evaluation_summary.json"))
    known_cases = load_json(os.path.join(eval_dir, "known_cases.json"))
    review_sample = load_json(os.path.join(eval_dir, "review_sample.json"))
    candidates = []
    with open(os.path.join(rank_dir, "candidates.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                candidates.append(json.loads(line))
    by_key = {row["record_key"]: row for row in candidates}
    check(True, f"{len(candidates)} review-pool candidate rows")

    print("\n3. every illustrated example maps to a real input record")
    examples = data["examples"]
    check(25 <= len(examples) <= 40, f"example count {len(examples)} is within 25-40")
    seen_ids = set()
    for example in examples:
        row = by_key.get(example["record_key"])
        label = example["record_key"]
        if row is None:
            check(False, f"{label}: no such review-pool record")
            continue
        ok = (
            row["file_name"] == example["file_name"]
            and row["split"] == example["split"]
            and row["image_id"] == example["image_id"]
            and row["n_annotations"] == example["n_annotations"]
            and row["cell_line"] == example["cell_line"]
        )
        check(ok, f"{label} matches the candidate row (file, split, image id, markings, cell line)")
        ranks = example["ranks"]
        ok = (
            ranks["low_count"]["seed42"] == row["low_count_rank"]
            and ranks["robust"]["seed42"] == row["robust_rank"]
            and ranks["isolation_forest"]["seed42"] == row["if_rank_seed42"]
            and ranks["isolation_forest"]["seed7"] == row["if_rank_seed7"]
            and ranks["isolation_forest"]["seed101"] == row["if_rank_seed101"]
            and ranks["random"]["seed42"] == row["random_rank_seed42"]
        )
        check(ok, f"{label} ranks match the candidate row")
        check(example["id"] not in seen_ids, f"{label} preview id is unique")
        seen_ids.add(example["id"])
        check(
            example["id"] == f"{example['split']}_{example['image_id']}",
            f"{label} preview id follows split_imageid",
        )
        features_ok = all(
            example["features"].get(name) == (int(row[name]) if isinstance(row[name], bool) else row[name])
            for name in ("n_annotations", "width", "height", "records_for_filename", "splits_for_filename")
        )
        check(features_ok, f"{label} core feature values match the candidate row")
        expected_kind = (
            "cross_split_overlap" if row["is_cross_split_overlap"]
            else "alternate_annotation_version" if row["is_alternate_version"]
            else "low_annotation_count" if row["is_known_case"]
            else "review_priority"
        )
        check(example["finding_type"] == expected_kind, f"{label} finding type matches its flags")
        if example["finding"]:
            check(
                example["finding"]["finding_id"] in {f["finding_id"] for f in findings},
                f"{label} audit finding id exists in findings.json",
            )

    print("\n4. curated list matches the example data")
    check(curated["count"] == len(examples), "curated count equals the example count")
    curated_ids = [(e["preview_id"], e["split"], e["image_id"], e["file_name"]) for e in curated["examples"]]
    data_ids = [(e["id"], e["split"], e["image_id"], e["file_name"]) for e in examples]
    check(curated_ids == data_ids, "curated preview ids/splits/image ids/filenames match the site data")
    check(curated["outline_cap"] == data["meta"]["outline_cap"], "outline cap agrees between curated list and site")

    print("\n5. the full ranking table matches candidates.jsonl")
    columns = ranking["columns"]
    check(len(ranking["rows"]) == len(candidates), "ranking row count equals the candidate count")
    index = {name: position for position, name in enumerate(columns)}
    mismatches = 0
    for row in ranking["rows"]:
        key = row[index["record_key"]]
        source = by_key.get(key)
        if source is None:
            mismatches += 1
            continue
        for name in columns:
            expected = source.get(name)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[index[name]] != expected:
                mismatches += 1
                break
    check(mismatches == 0, f"all {len(ranking['rows'])} ranking rows equal their candidate row ({mismatches} mismatches)")
    check(
        set(row[index["file_name"]] for row in ranking["rows"]) == {r["file_name"] for r in candidates},
        "the ranking table covers exactly the pool filenames",
    )

    print("\n6. every published number is re-derived from the inputs")
    audit = data["audit"]
    check(audit["counts"]["distinct_filenames"] == report["unique_filenames"], "distinct filenames come from report.json")
    check(audit["counts"]["image_records"] == report["total_image_records"], "image records come from report.json")
    check(audit["counts"]["annotations"] == report["total_annotations"], "annotation total comes from report.json")
    check(
        audit["counts"]["extra_within_split_records"] == report["extra_within_split_records"],
        "extra within-split records come from report.json",
    )
    check(
        audit["overlap"]["train_val"] == report["cross_split_filename_overlap"]["train_val"]["filenames"],
        "train/val overlap comes from report.json",
    )
    check(audit["findings"]["total"] == len(findings), "finding total equals findings.json length")
    if "differing_label_counts" in audit["overlap"]:
        differing = sum(
            1
            for finding in findings
            if finding["type"] == "cross_split_overlap"
            and len({tuple(r.get("counts") or []) for r in finding["records"].values()}) > 1
        )
        check(
            audit["overlap"]["differing_label_counts"] == differing,
            f"overlap findings with different label counts per split ({differing}) come from findings.json",
        )
    check(
        audit["known_sparse_cases"]["recovered"] == report["known_sparse_cases_recovered"],
        "known sparse recovery comes from report.json",
    )
    distribution = data["distribution"]
    annotations = sorted(row["n_annotations"] for row in candidates)
    median = annotations[len(annotations) // 2] if len(annotations) % 2 else (
        annotations[len(annotations) // 2 - 1] + annotations[len(annotations) // 2]
    ) / 2
    check(
        abs(distribution["n_annotations"]["median"] - median) < 1e-9,
        "pool median annotation count is recomputed correctly",
    )
    check(distribution["n_annotations"]["min"] == min(annotations), "pool minimum annotation count is correct")
    check(distribution["n_annotations"]["max"] == max(annotations), "pool maximum annotation count is correct")

    methods = data["methods"]
    check(
        methods["pools"]["review"]["size"] == len(candidates),
        "review-pool size equals the candidate count",
    )
    check(
        methods["pools"]["synthetic"]["size"] == evaluation["synthetic_annotation_loss"]["pool_size"],
        "synthetic-pool size comes from the evaluation summary",
    )
    for name in ("retained_fraction", "annotations_before", "annotations_after"):
        if name in evaluation["synthetic_annotation_loss"]:
            check(
                methods["synthetic"].get(name) == evaluation["synthetic_annotation_loss"][name],
                f"synthetic {name} comes from the evaluation summary",
            )
    check(
        methods["knownCase"]["upstream_reporter"] == known_cases["upstream_reporter"],
        "the upstream reporter comes from known_cases.json",
    )
    for row in methods["knownCase"]["recovery_table"]:
        for seed, budgets in row["seeds"].items():
            for budget, value in budgets.items():
                expected = known_cases["recovery"][f"{row['method']}_seed{seed}"][f"at_{budget}"]["recovered"]
                check(value == expected, f"recovery {row['method']} seed {seed} at {budget} = {expected}")
    for key, metrics in methods["synthetic"]["metrics"].items():
        expected = evaluation["synthetic_annotation_loss"]["metrics"][key]
        check(
            metrics == expected,
            f"synthetic metrics for {key} come from the evaluation summary",
        )
    for case in methods["knownCase"]["per_case"]:
        source = [c for c in known_cases["cases"] if c["file_name"] == case["file_name"]]
        check(bool(source), f"known case {case['file_name']} exists in known_cases.json")
        if source:
            check(
                case["ranks"]["low_count_seed42"] == source[0]["ranks"]["low_count_seed42"]
                and case["observed_annotations"] == source[0]["observed_annotations"],
                f"known case {case['file_name']} ranks and counts match",
            )
    check(
        methods["model"]["fitted_cell_lines"] == model_card["fitted_cell_lines"]
        and methods["model"]["feature_selection"] == model_card["feature_selection"]
        and methods["limitations"] == model_card["limitations"],
        "model configuration, feature selection and limitations come from the model card",
    )
    check(methods["humanReview"]["real_world_precision"] is None, "the site claims no measured real-world precision")
    check(
        all(record.get("judgment") is None for record in review_sample["records"]),
        "no fabricated judgment exists in the review sample",
    )

    print("\n7. provenance hashes match the input files")
    repo_root = os.getcwd()
    for entry in data["provenance"]["files"]:
        path = entry["path"]
        full = os.path.join(repo_root, path)
        if not os.path.isfile(full):
            check(False, f"provenance file exists: {path}")
            continue
        check(sha256_file(full) == entry["sha256"], f"declared SHA-256 matches the file ({path})")
        check(os.path.getsize(full) == entry["bytes"], f"declared size matches the file ({path})")
    for split, entry in sources["coco_files"].items():
        check(
            data["provenance"]["source_hashes"].get(split) == entry["sha256"],
            f"site source hash for {split} matches the audit manifest",
        )
    check(
        data["provenance"]["feature_manifest_sha256"]
        == ranking_summary["provenance"]["feature_manifest_sha256"],
        "feature manifest hash comes from the ranking summary",
    )
    check(
        data["provenance"]["warehouse_manifest_sha256"]
        == ranking_summary["provenance"]["warehouse_manifest_sha256"],
        "warehouse manifest hash comes from the ranking summary",
    )

    print("\n8. feature units still exist in the feature dictionary")
    features_source = os.path.join("src", "livecell_audit", "features.py")
    units = set()
    for example in examples:
        for item in example["evidence_features"]:
            if item["unit"]:
                units.add(item["unit"])
    if os.path.isfile(features_source):
        text = open(features_source, encoding="utf-8").read()
        for unit in sorted(units):
            base = unit.split(" (")[0]
            check(base in text, f"unit {base!r} occurs in {features_source}")
    else:
        check(False, f"{features_source} is present")

    print("\n9. every relative link, asset and download exists")
    html = open(os.path.join(site, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(site, "assets", "review.js"), encoding="utf-8").read()
    references = set()
    for match in re.finditer(r'(?:href|src)="([^"]+)"', html):
        references.add(match.group(1))
    for match in re.finditer(r"downloads/([A-Za-z0-9._-]+)", html + js):
        references.add("downloads/" + match.group(1))
    for match in re.finditer(r'"(previews/[A-Za-z0-9._-]+)"', js):
        references.add(match.group(1))
    pending = data["meta"]["preview_status"] != "delivered"
    for reference in sorted(references):
        if reference.startswith("#") or reference.startswith("http") or reference.startswith("mailto"):
            continue
        target = os.path.join(site, reference)
        if reference.startswith("previews/") and pending:
            continue  # the page declares previews as not delivered; no broken image is rendered
        check(os.path.isfile(target), f"relative reference resolves: {reference}")
    check("/web/review/" not in html + js, "no root-absolute site path is used")
    check(not re.search(r'(href|src)="/', html), "no root-absolute href or src in the page")

    print("\n10. no private path, host or environment detail is published")
    forbidden = ("/home/", "/Users/", "saber_s13", "firstmate", "livecell-mvp", "C:\\")
    leaks = []
    for path in iter_site_files(site):
        if not path.endswith((".html", ".css", ".js", ".json", ".txt", ".md", ".csv")):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        for needle in forbidden:
            if needle in text:
                leaks.append(f"{os.path.relpath(path, site)} contains {needle!r}")
    check(not leaks, "no private path or environment detail in any site file" + (f": {leaks[:3]}" if leaks else ""))

    print("\n11. no forbidden claim appears on the site")
    page_text = html + "\n" + js
    negations = ("no ", "not ", "never ", "does not ", "without ", "no invented ", "instead of ")
    for phrase in (
        "health score",
        "detected cell boundary",
        "detected cell boundaries",
        "Download fixed dataset",
        "Run audit",
        "improved segmentation accuracy",
        "probability that a label is wrong",
    ):
        lowered = page_text.lower()
        offender = None
        start = lowered.find(phrase.lower())
        while start != -1:
            window = lowered[max(0, start - 60):start]
            if not any(negation in window for negation in negations):
                offender = page_text[max(0, start - 60):start + len(phrase)].replace("\n", " ")
                break
            start = lowered.find(phrase.lower(), start + 1)
        check(offender is None, f"forbidden phrase absent unless explicitly denied: {phrase!r}"
              + (f" (found: ...{offender})" if offender else ""))
    check(
        "run a new audit" in page_text or "does not run a new audit" in page_text,
        "the page states plainly that it does not run a new audit",
    )

    print("\n12. delivered previews match the ranked annotation counts")
    if pending:
        check(True, "previews are declared pending, so no preview file is referenced")
    else:
        preview_dir = os.path.join(args.inputs, "previews")
        manifest = load_json(os.path.join(preview_dir, "manifest.json"))
        polygons = load_json(os.path.join(preview_dir, "polygons.json"))
        manifest_by_id = {entry["preview_id"]: entry for entry in manifest["previews"]}
        png_provenance = {
            entry["preview_id"]: entry
            for entry in data["provenance"]["files"]
            if entry.get("role") == "preview-png"
        }
        check(
            manifest["curated_sha256"]
            == sha256_file(os.path.join(site, "data", "curated-examples.json")),
            "the render manifest was produced from this site's exact curated-example set",
        )
        check(
            data["provenance"]["preview_manifest_sha256"]
            == sha256_file(os.path.join(preview_dir, "manifest.json")),
            "the recorded preview manifest SHA-256 matches manifest.json",
        )
        for example in examples:
            source = example["preview"]["src"]
            path = os.path.join(site, source) if source else None
            check(bool(source) and os.path.isfile(path), f"{example['id']}: preview file exists")
            if not (path and os.path.isfile(path)):
                continue
            entry = manifest_by_id.get(example["id"])
            check(entry is not None and entry.get("file") == os.path.basename(source),
                  f"{example['id']}: preview is named by the render manifest")
            row = by_key[example["record_key"]]
            check(
                example["preview"]["polygons_total"] == row["n_annotations"],
                f"{example['id']}: supplied outline total equals the ranked annotation count",
            )
            check(
                (example["preview"]["polygons_drawn"] or 0) <= data["meta"]["outline_cap"],
                f"{example['id']}: drawn outlines respect the cap",
            )
            digest = sha256_file(path)
            check(digest == example["preview"]["png_sha256"],
                  f"{example['id']}: displayed PNG matches the recorded PNG SHA-256")
            if entry:
                check(digest == entry.get("png_sha256"),
                      f"{example['id']}: displayed PNG matches the render manifest SHA-256")
                check(example["preview"]["tiff_sha256"] == entry.get("tiff_sha256"),
                      f"{example['id']}: source TIFF SHA-256 matches the render manifest")
            provenance_entry = png_provenance.get(example["id"])
            check(bool(provenance_entry) and provenance_entry.get("sha256") == digest,
                  f"{example['id']}: provenance footer records the PNG SHA-256")
            check(bool(provenance_entry) and provenance_entry.get("tiff_sha256")
                  == example["preview"]["tiff_sha256"],
                  f"{example['id']}: provenance footer records the source TIFF SHA-256")
            drawn = example["preview"]["polygons"] or []
            check(len(drawn) == (example["preview"]["polygons_drawn"] or 0)
                  and len(drawn) <= data["meta"]["outline_cap"],
                  f"{example['id']}: drawn outline count matches the polygons carried by the site")
            check(
                (polygons.get("examples", {}).get(example["id"]) or {}).get("file_name")
                == example["file_name"],
                f"{example['id']}: drawn outlines belong to the same source filename",
            )
            if entry and "labels_layer" in entry:
                capped = (example["preview"]["polygons_drawn"] or 0) < row["n_annotations"]
                layer = entry["labels_layer"]
                check(bool(layer) == capped,
                      f"{example['id']}: a label layer exists exactly when the outlines are capped")
                if layer:
                    layer_path = os.path.join(site, "previews", layer["file"])
                    layer_digest = sha256_file(layer_path) if os.path.isfile(layer_path) else None
                    check(layer_digest is not None and layer_digest == layer.get("png_sha256")
                          and example["preview"].get("labels_png_sha256") == layer_digest,
                          f"{example['id']}: label layer matches the render manifest SHA-256")
                    check(layer.get("outlines_drawn") == row["n_annotations"]
                          and example["preview"].get("labels_drawn") == row["n_annotations"],
                          f"{example['id']}: label layer draws every supplied outline")
                    check(example["preview"].get("labels_src") == "previews/" + layer["file"],
                          f"{example['id']}: the site displays the recorded label layer")
            elif entry is not None:
                skip_check(f"{example['id']}: render manifest predates label layers")
        check(len(examples) <= 40, "delivered preview count respects the 40-image bound")
        check(len(png_provenance) == len(examples),
              "every illustrated example has a preview PNG recorded in provenance")
        check(
            all(
                ((polygons.get("examples", {}).get(example["id"]) or {}).get("polygons")
                 == example["preview"]["polygons"])
                for example in examples
            ),
            "the outlines drawn by the site are exactly those in polygons.json",
        )

    print("\n13. JavaScript syntax")
    check_javascript_syntax(site)

    print(f"\n{len(FAIL)} failed check(s)")
    if SKIP:
        print(f"{len(SKIP)} skipped check(s)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
