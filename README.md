# LIVECell Audit 🔬

> **A repeatable data-quality audit of Sartorius' public LIVECell cell-image dataset, with a plain-language review website.**

Researchers mark cells in microscope images so an AI model can learn to recognise them. This project checks those images and their labels before they are used in an experiment, and explains what it found in plain language.

**What is implemented:** a streaming audit of the COCO annotation files, a DuckDB/Parquet warehouse, a documented pandas feature table, a fit-only Isolation Forest review ranking compared with simple baselines at the same review budget, a static review website, and one release entrypoint (`tools/run_release.sh`) that runs the whole chain into an immutable run directory and packages a portable, path-clean ZIP. The simple low-count rule beat the Isolation Forest, on two tests that are defined by label count and so favour it by design. No cell-segmentation model is trained and no effect on its accuracy is measured. Expert scientific review of the flagged images and any upstream contribution remain future work.

---

## What the audit found (reproduced from the raw files)

| Group | Image records | Distinct photos | Extra records | Cell markings |
|---|---:|---:|---:|---:|
| Training | 3,253 | 3,188 | 65 | 1,018,576 |
| Validation | 570 | 569 | 1 | 181,610 |
| Test | 1,564 | 1,512 | 52 | 462,261 |

- **5,239** distinct image files, all present locally; **1,662,447** cell markings in total.
- **30 images** appear in both the learning (train) and checking (validation) groups, a form of data leakage that affects validation scores. In all 30, the two groups carry different annotation versions of the photo (the authors' dual annotations, issue #14). The test group shares no filenames with either.
- **118 extra within-split records**: the same photo with more than one record. The dataset authors kept alternate annotation versions (issue #14), so these are flagged as needing an explicit policy, not as corrupt records.
- **9 previously reported sparse-label images** have 10 or fewer markings; the low-count rule recovers all 9 as review candidates. They were reported upstream with the same ten-or-fewer threshold, so this is a regression check, not a discovery.

Both issues were first reported on GitHub (#26 and #21); this project reproduces them from the raw files and tracks them with evidence. See [`docs/02_upstream_bugs_and_findings.md`](docs/02_upstream_bugs_and_findings.md).

---

## Quickstart

Requirements: Python 3.12 and the LIVECell data (`scripts/download_data.sh`, ~2.1 GB). The audit streams the ~909 MB of COCO JSON and never holds it in memory, but the full pipeline and the integration test need about 1 GB; run them on a machine or job with that headroom.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

```bash
# 1. Audit (data root holds annotations/livecell_coco_{train,val,test}.json and images/)
PYTHONPATH=src .venv/bin/python -m livecell_audit audit \
    --data-root data/raw --out-dir data/workbench/audit

# 2. DuckDB + typed Parquet warehouse from the completed audit
PYTHONPATH=src .venv/bin/python -m livecell_audit warehouse \
    --audit-dir data/workbench/audit --out-dir data/workbench/warehouse --data-root data/raw

# 3. pandas feature table + cell-line summaries
PYTHONPATH=src .venv/bin/python -m livecell_audit features \
    --warehouse-dir data/workbench/warehouse --data-root data/raw --out-dir data/workbench/features

# 4. Fit-only baselines + Isolation Forest review rankings
PYTHONPATH=src .venv/bin/python -m livecell_audit rank \
    --features-dir data/workbench/features --warehouse-dir data/workbench/warehouse \
    --out-dir data/workbench/ranking

# 5. Known-case regression, synthetic annotation-loss experiment, frozen review sample
PYTHONPATH=src .venv/bin/python -m livecell_audit evaluate \
    --features-dir data/workbench/features --warehouse-dir data/workbench/warehouse \
    --ranking-dir data/workbench/ranking --out-dir data/workbench/evaluation
```

The source data is only read, never modified. Every stage refuses an input directory without a valid `COMPLETED` marker, and ranking/evaluation record the exact feature/warehouse build they consumed.

```bash
# Tests: fixtures everywhere, plus the full-dataset check when the data root is set
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
LIVECELL_DATA_ROOT=data/raw PYTHONPATH=src .venv/bin/python -m pytest tests/test_integration.py -q
```

```bash
# View the committed review website (no build step; file:// works too)
python3 -m http.server --directory web/review 8321
```

Measured in the reference release run (`20261018T220218Z-0e359b9bf088`, one capped job per stage, process peak RSS): audit 44 s / 199 MB, warehouse 16 s / 323 MB, features 98 s / 275 MB (5,239 TIFF reads dominate), ranking 28 s / 340 MB, evaluation 12 s / 350 MB, preview render 22 s / 60 MB. The test suite is **266 passed** in ~79 s including the real-data integration check. Details: [`docs/12_release.md`](docs/12_release.md).

---

## Release in one command

`tools/run_release.sh` runs the whole chain: audit → warehouse → features → rank (seed 42, budgets 10/25/50) → evaluate → preview render → site build → site check → portable package. By default every stage runs through a memory-capped launcher (`run-job`); see the script's `--help`.

```bash
tools/run_release.sh --data-root data/raw --out-root data/runs        # chain + package
tools/run_release.sh validate --data-root data/raw                    # preflight only, starts nothing
tools/run_release.sh package --run data/runs/<RUN_ID>                 # re-package a finished run
tools/run_release.sh verify-package --zip <release.zip>               # re-verify an archive
tools/run_release.sh list --out-root data/runs                        # list runs and their state
```

What a run guarantees:

- **Immutable run IDs.** A run lives in `<out-root>/<UTC timestamp>-<input-hash prefix>/`, where the prefix comes from the three COCO files' SHA-256 plus the image-set inventory (and, with `--tiff-identity`, every raw image's SHA-256). An existing run directory is never reused or overwritten, and a failed run is preserved for inspection.
- **Per-stage records.** Each stage writes `records/stage-<stage>.json` with its exact command, configuration, seeds, input identity, cross-stage checks, per-file output hashes, an output tree hash, log hash, wall time and peak memory with its measurement method.
- **Fail-fast cross-stage validation.** A stage refuses to start unless its inputs carry a valid `COMPLETED` marker and pass their identity checks (COCO source hashes, warehouse, feature and ranking manifest hashes).
- **Completion last.** The pipeline `COMPLETED` marker and the release receipt are written only after `verify-chain` re-verifies every marker, manifest, cross-link and recorded output hash.
- **A portable package.** `<run>/release/livecell-audit-release-<RUN_ID>.zip` carries the static site, ranking/evaluation tables, model card, methodology docs, `THIRD_PARTY_NOTICES.txt`, provenance and the release receipt. Raw TIFFs, COCO JSON, the DuckDB database, Parquet, model binaries and absolute/private paths are excluded by an automatic scan, and the archive is unzipped and every relative link re-resolved before it is accepted.

Release packages and run records stay out of git (the receipt inside each run directory holds machine paths; the packaged copy is redacted). No container is built: Docker, podman and buildah are unavailable on the data server. The procedure, exclusions and what is and is not verified are in [`docs/12_release.md`](docs/12_release.md).

---

## Review website

`web/review/` is a static projection of one completed release run: no backend, no framework, no network call. It has three tabs: an overview of the findings, a "Try it" image browser with 39 illustrated examples and switchable supplied labels (dense images show every supplied outline), and the method comparison with the full 3,067-row ranking table and evidence downloads. Every number is copied from the run's verified outputs, and `tools/check_review_site.py` re-derives them. See [`docs/11_review_interface.md`](docs/11_review_interface.md) and [`DESIGN.md`](DESIGN.md).

---

## Repository layout

```text
src/livecell_audit/   # audit.py + coco_stream.py (streaming COCO reader), filenames.py,
                      # warehouse.py (DuckDB/Parquet), features.py (pandas),
                      # ranking.py (baselines + Isolation Forest),
                      # evaluate.py (known cases, synthetic loss, review sample),
                      # completion.py (staged promotion / completion marker), cli.py
tests/                # fixture tests for every stage, the release tooling and the
                      # preview render, plus the full-dataset integration check
scripts/              # download_data.sh (how the data was obtained)
tools/                # run_release.sh (one entrypoint), release_tools.py (stage records,
                      # cross-stage validation, packaging), build_review_site.py,
                      # make_previews.py (bounded server-side previews),
                      # check_review_site.py (static traceability/link checks)
web/review/           # the review website, built from the reference release run
docs/                 # findings (02), data contract and model card (09),
                      # ranking evaluation (10), review website (11), release (12),
                      # upstream issue snapshots (upstream/)
data/, releases/      # gitignored: raw data, local outputs, release runs and packages
```

---

## Output definitions

`python3 -m livecell_audit audit --data-root … --out-dir OUT` writes:

- `report.json` — deterministic summary: per-split counts, filename overlap, low-count records, the nine known cases with observed counts, the derived validation policy, worked examples, and run metadata.
- `findings.json` — 157 per-finding records (`cross_split_overlap`, `alternate_annotation_version`, `low_annotation_count`, plus missing/dimension/filename findings when present), each with status, evidence, and a recommended next step.
- `tables/images.jsonl` — one normalized row per COCO image record: split, ids, parsed filename fields (cell line, well, field, timestamp, crop), dimensions, local-file reconciliation, annotation count, and a `dimensions_status` field.
- `tables/annotations.jsonl` — one compact row per annotation (ids, area, bbox, polygon size); ~261 MB.
- `manifests/sources.json` — SHA-256 checksums and sizes of the three source COCO files.
- `manifests/val_membership.json` — derived validation membership: every validation record marked included/excluded with a reason. Policy: exclude validation records whose photo also appears in train (30 excluded, 540 included). Source files untouched, original ids and versions preserved, no polygon sets merged. This is an evaluation policy, not an official correction.
- `run.json` — command, runtime seconds, peak-memory MB, and the measurement methods.
- `COMPLETED` — run-completion marker (written last, atomically). An output directory is a *completed* run if and only if this marker is present, valid JSON, declares `status: "completed"`, and its source checksums and summary counts match the other outputs. Outputs are staged and promoted only on success; a failed or interrupted run removes the marker, so partial data is never consumed by a later stage.

All JSON outputs use sorted keys and sorted records, so reruns over the same inputs are byte-identical (tested, excluding run metadata paths/timings and the marker's completion timestamp).

`dimensions_status` separates five mutually exclusive states: `matching`, `mismatching`, `missing` (no local file), `unreadable` (file present but its size could not be read), and `not_checked` (`--no-check-dimensions`). Only `mismatching` records appear in `dimension_mismatches`.

## Warehouse and feature outputs

`python3 -m livecell_audit warehouse --audit-dir AUDIT --out-dir WH` consumes a *completed* audit run and writes:

- `audit.duckdb` — DuckDB database, schema `lc`. Tables: `images`, `annotations`, `findings`, `finding_evidence`, `sources`, `runs`, `val_membership`. Versioned views: `v1_image_annotation_stats`, `v1_split_membership`, `v1_split_overlap`, `v1_annotation_versions`, `v1_alternate_versions`, `v1_cell_line_summary`, `v1_annotation_totals`, `v1_finding_lookup`, `v1_finding_context`, `v1_image_join_ambiguity`, `v1_orphan_annotations`.
- `parquet/` — typed Parquet exports of the base tables and the main views (~34 MB for annotations vs ~249 MB JSONL).
- `integrity.json` — deterministic reconciliation (expected/loaded/delta per table and split), join integrity (ambiguous ids, orphans, duplicate annotation keys), input-file hashes and export sizes.
- `manifest.json`, `run.json`, `COMPLETED` — per-output size + SHA-256 (the marker records the manifest's hash, so a swapped database is rejected), run metadata, and the completion marker.

Image records are keyed by `(split, record_position)`; the original `id` is a field, not a global key. Ambiguous `(split, image_id)` joins and orphan annotations are detected and reported, never guessed or multiplied.

`python3 -m livecell_audit features --warehouse-dir WH --out-dir FEAT` verifies the warehouse, then writes one feature row per annotation-version image record (`features.parquet`/`.jsonl`: annotation aggregates, acquisition metadata, image dimensions, once-per-filename TIFF statistics, quality flags), cell-line summaries, feature distributions and a `feature_dictionary.json` with each feature's source, formula, unit and missing-value behaviour. The full contract is [`docs/09_data_contract.md`](docs/09_data_contract.md); the ranking configuration and its limitations are in [`docs/09_model_card.md`](docs/09_model_card.md) and the results in [`docs/10_ranking_evaluation.md`](docs/10_ranking_evaluation.md).

## Limitations

- Filename identity is physical identity: matches are by filename, not pixels. Near-duplicates are out of scope.
- A low count alone is not proof of an error, and recovering the nine known cases is a regression check. Both evaluation tests are defined by label count, so they cannot show what a model adds beyond counting; real-world precision is unmeasured until a scientist reviews the frozen sample.
- The Isolation Forest mixes label statistics with image texture, so it flags unusual images rather than labels that disagree with their image; three of its 24 features are constant on LIVECell (every image is 704 × 520).
- Image statistics are texture proxies, not cell counts; annotation area is not true confluence. The real dataset has no ambiguous ids or orphan annotations, so those paths are fixture-verified only.
- No cell-segmentation model is trained and no effect on its accuracy is measured or claimed.
- Layout and keyboard behaviour were checked in Chromium; a real recruiter comprehension check has not been performed.
- No container, no cloud execution and no public deployment; `DEPLOY.md` in the release package gives generic hosting steps.
- Raw image pixels are content-hashed only where the run recorded a TIFF identity manifest (`--tiff-identity`, on by default); COCO hashes alone cannot detect changed pixel content.

## Credit & attribution

- LIVECell dataset and annotations: Sartorius Corporate Research (Edlund et al., *Nature Methods* 2021), CC BY-NC 4.0. Upstream repository: `sartorius-research/LIVECell`.
- Overlap/duplication report (issue #26): community reporter `OasisArtisan`. Incomplete-annotation report (issue #21): `ivan-ea`, with a reply from Rickard Sjögren (Sartorius). Dual-annotation explanation (issue #14): Christoffer Edlund (Sartorius).
- Audit code and website in this repository: **Saber Sojudi** (2026), for non-commercial research, portfolio, and educational purposes.
