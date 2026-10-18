# LIVECell Audit 🔬

> **A small, repeatable data-quality audit for Sartorius' public LIVECell cell-image dataset, with a recruiter-friendly report.**

Researchers mark cells in microscope images so an AI model can learn to recognise them. This project checks those images and their labels before they are used in an experiment — and explains what it found in plain language.

**Status: audit + warehouse + feature analysis implemented.** The repeatable audit runs, a DuckDB/Parquet warehouse and a documented pandas feature table are built from it, and the report page shows real generated data. The full dataset reproduces every snapshot number below. There is no model training, no model-score comparison, no health score, and no upstream contribution yet.

---

## What the audit found (reproduced from the raw files)

| Group | Image records | Distinct photos | Extra records | Cell markings |
|---|---:|---:|---:|---:|
| Training | 3,253 | 3,188 | 65 | 1,018,576 |
| Validation | 570 | 569 | 1 | 181,610 |
| Test | 1,564 | 1,512 | 52 | 462,261 |

- **5,239** distinct image files, all present locally; **1,662,447** cell markings in total.
- **30 images** appear in both the learning (train) and checking (validation) groups — a form of data leakage. The test group shares no filenames with either.
- **118 extra within-split records**: the same photo with more than one record. The dataset authors kept alternate annotation versions (see upstream issue #14), so these are flagged as needing an explicit policy, not as corrupt records.
- **9 previously reported sparse-label images** have 10 or fewer markings; the simple low-count baseline recovers all 9 as review candidates.

---

## Quickstart

Requirements: Python 3.12. Create the pinned project-local environment once:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

```bash
# 1. Run the audit (streams the ~909 MB of COCO JSON, never fully in memory)
PYTHONPATH=src python3 -m livecell_audit audit \
    --data-root /path/to/data/raw \
    --out-dir /path/to/outputs
```

`--data-root` must contain `annotations/livecell_coco_{train,val,test}.json` and `images/` (see `scripts/download_data.sh` for how the data was obtained). The source tree is only read, never modified.

```bash
# 2. Load the completed audit into DuckDB + typed Parquet
PYTHONPATH=src python3 -m livecell_audit warehouse \
    --audit-dir /path/to/outputs \
    --out-dir data/workbench/warehouse \
    --data-root /path/to/data/raw

# 3. Build the pandas feature table + cell-line summaries
PYTHONPATH=src python3 -m livecell_audit features \
    --warehouse-dir data/workbench/warehouse \
    --data-root /path/to/data/raw \
    --out-dir data/workbench/features

# 4. Fit fit-only baselines + Isolation Forest and export review rankings
PYTHONPATH=src python3 -m livecell_audit rank \
    --features-dir data/workbench/features \
    --warehouse-dir data/workbench/warehouse \
    --out-dir data/workbench/ranking

# 5. Known-case regression, synthetic annotation-loss experiment, review sample
PYTHONPATH=src python3 -m livecell_audit evaluate \
    --features-dir data/workbench/features \
    --warehouse-dir data/workbench/warehouse \
    --ranking-dir data/workbench/ranking \
    --out-dir data/workbench/evaluation

# 6. Rebuild the connected report page from the audit outputs
PYTHONPATH=src python3 scripts/build_report.py \
    --audit-dir /path/to/outputs \
    --data-root /path/to/data/raw \
    --report-dir web/report

# 7. View it (no build step; file:// works too)
python3 -m http.server --directory web/report 8321
```

`--audit-dir` must be a *completed* run: the builder refuses a directory without a valid `COMPLETED` marker (see below) and never overwrites an existing report from incomplete or stale evidence. To reuse an older snapshot, rerun the audit into it first — the builder does not fabricate markers for pre-marker snapshots such as the committed `data/derived/`. The `warehouse`, `features`, `rank` and `evaluate` stages enforce the same completion contract, and a ranking/evaluation run records the exact feature/warehouse build identity it consumed.

```bash
# 8. Run the checks
PYTHONPATH=src python3 -m pytest tests/ -q          # full suite (fixtures + integration)
LIVECELL_DATA_ROOT=/path/to/data/raw \
  PYTHONPATH=src python3 -m pytest tests/test_integration.py -q   # full-dataset check only
```

Measured on the full dataset under the capped server job (1536 MiB RAM, 1024 MiB throttle, 1 CPU): the repaired two-pass audit takes **~47 s, ~118 MB peak**; the warehouse **~13–27 s**; the feature stage **~65–71 s** (5,239 TIFF reads dominate); the ranking stage **~27 s, ~400 MB peak** (down from ~1.0 GB before the bounded annotation-read repair); the evaluation stage **~11 s, ~256 MB peak**. The full suite is **237 passed** (~77 s including the real-data integration check). See `docs/08_data_analysis_completion.md`, `docs/09_model_card.md` and `docs/10_ranking_evaluation.md` for exact commands, sizes and limitations.

---

## Repository layout

```text
src/livecell_audit/   # audit package: streaming COCO reader, filename parser,
                      # analysis + deterministic exports, CLI
                      # + warehouse.py (DuckDB/Parquet), features.py (pandas),
                      # + ranking.py (baselines + Isolation Forest),
                      # + evaluate.py (known cases, synthetic loss, review sample)
                      # + completion.py (staged promotion / completion marker)
tests/                # fixture tests (overlap, versions, missing/malformed files,
                      # parsing, determinism, section-order/dimension/known-case
                      # regressions, completion contract, report-builder validation,
                      # warehouse reconciliation/ambiguity, feature formulas,
                      # ranking/evaluation contract)
                      # + full-dataset integration check
scripts/              # download_data.sh (provenance), build_report.py (report builder)
data/derived/         # committed small outputs: report.json, findings.json,
                      # run.json, manifests/sources.json + val_membership.json
                      # (a pre-marker MVP snapshot; rerun the audit to regenerate)
data/workbench/       # gitignored local outputs: warehouse/ (DuckDB + Parquet),
                      # features/ (feature table + summaries),
                      # ranking/ (candidates, partition, model card, models),
                      # evaluation/ (known cases, synthetic loss, review sample)
web/prototype/        # reference interface mock (verified snapshot, no engine)
web/report/           # connected static report built from real generated data
docs/                 # design + interface spec + findings + completion reports
                      # (docs/07 audit, docs/08 warehouse+features,
                      #  docs/09_data_contract.md, docs/09_model_card.md,
                      #  docs/10_ranking_evaluation.md)
requirements.txt      # exact runtime pins
requirements-dev.txt  # runtime + test runner
```

## Output definitions

`python3 -m livecell_audit audit --data-root … --out-dir OUT` writes:

- `report.json` — deterministic summary: per-split counts, filename overlap, low-count records, the nine known cases with observed counts, the derived validation policy, worked examples, and run metadata.
- `findings.json` — 157 per-finding records (`cross_split_overlap`, `alternate_annotation_version`, `low_annotation_count`, plus missing/dimension/filename findings when present), each with status, evidence, and a recommended next step.
- `tables/images.jsonl` — one normalized row per COCO image record: split, ids, parsed filename fields (cell line, well, field, timestamp, crop), dimensions, local-file reconciliation, annotation count, and a `dimensions_status` field.
- `tables/annotations.jsonl` — one compact row per annotation (ids, area, bbox, polygon size). Large (~261 MB); not committed.
- `manifests/sources.json` — SHA-256 checksums and sizes of the three source COCO files.
- `manifests/val_membership.json` — derived validation membership: every validation record marked included/excluded with a reason. Policy: exclude validation records whose photo also appears in train (30 excluded, 540 included). Source files untouched, original ids and versions preserved, no polygon sets merged. This is an evaluation policy, not an official correction.
- `run.json` — command, runtime seconds, peak-memory MB, and the measurement methods.
- `COMPLETED` — run-completion marker (written last, atomically). An output directory is a *completed* run if and only if this marker is present, valid JSON, declares `status: "completed"`, and its source checksums and summary counts match the other outputs. Outputs are staged and promoted only on success; a failed or interrupted run (including a missing input on a reused directory) removes the marker, so partial data is never served as a valid report, and `scripts/build_report.py` refuses to publish without it.

All JSON outputs use sorted keys and sorted records, so reruns over the same inputs are byte-identical (tested, excluding run metadata paths/timings and the marker's completion timestamp).

## Warehouse and feature outputs

`python3 -m livecell_audit warehouse --audit-dir AUDIT --out-dir WH` consumes a *completed* audit run (marker, source checksums and summary counts are validated first) and writes:

- `audit.duckdb` — DuckDB database, schema `lc`. Tables: `images`, `annotations`, `findings`, `finding_evidence`, `sources`, `runs`, `val_membership`. Versioned views: `v1_image_annotation_stats`, `v1_split_membership`, `v1_split_overlap`, `v1_annotation_versions`, `v1_alternate_versions`, `v1_cell_line_summary`, `v1_annotation_totals`, `v1_finding_lookup`, `v1_finding_context`, `v1_image_join_ambiguity`, `v1_orphan_annotations`.
- `parquet/` — typed Parquet exports of the base tables plus the annotation-stats, cell-line-summary, annotation-totals and finding evidence/context views (~34 MB for annotations vs ~249 MB JSONL).
- `integrity.json` — deterministic reconciliation (expected/loaded/delta per table and split), join integrity (ambiguous ids, orphans, duplicate annotation keys), input-file hashes and export sizes.
- `manifest.json` — size + SHA-256 for every required output, including the database. The marker records the manifest's SHA-256, so a modified or swapped database is rejected by a consumer.
- `run.json`, `COMPLETED` — run metadata and the completion marker.

Image records are keyed by `(split, record_position)`; the original `id` is a field, not a global key. Ambiguous `(split, image_id)` joins and orphan annotations are detected and reported, never guessed or multiplied. `v1_finding_lookup` returns a finding's *exact* implicated version via `finding_evidence`; filename/split context is separate and labelled in `v1_finding_context`. `v1_cell_line_summary` publishes a reliable `annotations_attributed` alongside the explicitly labelled per-record `annotation_count_sum_records`, and `v1_annotation_totals` reconciles `total = attributed + ambiguous + orphan`.

`python3 -m livecell_audit features --warehouse-dir WH --out-dir FEAT` verifies the warehouse marker, manifest and database provenance, then writes one feature row per annotation-version image record:

- `features.parquet` / `features.jsonl` — annotation aggregates (count, area and bbox summaries, polygon-point summaries), acquisition metadata, image dimensions, once-per-filename TIFF statistics (`pixel_mean`, `pixel_std`, `gradient_mean`, …) and missingness/quality flags.
- `cell_line_summary.json`/`.csv`/`.parquet` and `feature_distributions.json` — grouped summaries and quantiles.
- `feature_dictionary.json` — per-feature source, formula, unit, `depends_on_annotations`, missing-value behaviour, a raw-TIFF intensity contract, plus a schema version and source-manifest linkage.
- `manifest.json` — size + SHA-256 for every required feature output.
- `run.json`, `COMPLETED` — run metadata, `mode` (`full`/`partial`) and the completion marker.

Zero annotations are a measured zero (with undefined area/bbox summaries left null); missing or unreadable TIFFs keep null statistics and an explicit status; ambiguous annotation joins are quarantined with `annotation_join_status = "ambiguous_image_id"`. A development `--max-filenames` limit marks the run `partial` and leaves unread records `not_attempted`; `features.assert_release_ready` rejects a partial or tampered feature directory. Alternate annotation versions are all retained and reuse image-only features by filename. See `docs/09_data_contract.md` for the full contract.

### Dimension states

`dimensions_status` (and the report's `dimension_status_counts`) separates five mutually exclusive states, most importantly reporting *not checked* distinctly from a mismatch: `matching`, `mismatching`, `missing` (no local file), `unreadable` (file present but its size could not be read), and `not_checked` (`--no-check-dimensions`). Only `mismatching` records appear in `dimension_mismatches`. The row-level `dimensions_match` boolean is kept for compatibility and is true only for `matching`.

## Limitations

- Filename identity is physical identity: matches are by filename, not pixels. Near-duplicates are out of scope.
- The low-count rule flags review candidates; a low count alone is not proof of an error, and recovering the nine known cases is a regression check, not ML generalisation (no IsolationForest, no growth modelling in this MVP).
- Warehouse/feature stages: the feature table keys rows by `(split, record_position)` and keeps `image_id` as a field; the real dataset has no ambiguous ids or orphan annotations, so those paths are fixture-verified only. Image statistics are texture proxies, not cell counts; annotation area is not true confluence. The measured JSONL-vs-DuckDB query difference is a single warm-cache query shape, not a speedup guarantee.
- No model is trained and no accuracy effect is measured or claimed.
- Desktop/mobile layout and keyboard behaviour were checked in Chromium; a real recruiter comprehension check has not been performed.

## Credit & attribution

- LIVECell dataset and annotations: Sartorius Corporate Research (Edlund et al., *Nature Methods* 2021), CC BY-NC 4.0. Upstream repository: `sartorius-research/LIVECell`.
- Overlap/duplication report (issue #26): community reporter `Alimusto`. Incomplete-annotation report (issue #21): `alxndrdiaz`, with discussion from community members. Dual-annotation explanation (issue #14): Christoffer Edlund (Sartorius).
- Audit code and report in this repository: **Saber Sojudi** (2026), for non-commercial research, portfolio, and educational purposes.
