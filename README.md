# LIVECell Audit 🔬

> **A small, repeatable data-quality audit for Sartorius' public LIVECell cell-image dataset, with a recruiter-friendly report.**

Researchers mark cells in microscope images so an AI model can learn to recognise them. This project checks those images and their labels before they are used in an experiment — and explains what it found in plain language.

**Status: MVP implemented.** The repeatable audit runs, the report page shows real generated data, and the full dataset reproduces every snapshot number below. There is no model training, no model-score comparison, no health score, and no upstream contribution yet.

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

Requirements: Python 3.10+ and Pillow (`pip install pillow`). No other installation — the audit is standard library plus Pillow, run from the repository root:

```bash
# 1. Run the audit (streams the ~909 MB of COCO JSON, never fully in memory)
PYTHONPATH=src python3 -m livecell_audit audit \
    --data-root /path/to/data/raw \
    --out-dir /path/to/outputs
```

`--data-root` must contain `annotations/livecell_coco_{train,val,test}.json` and `images/` (see `scripts/download_data.sh` for how the data was obtained). The source tree is only read, never modified.

```bash
# 2. Rebuild the connected report page from the audit outputs
PYTHONPATH=src python3 scripts/build_report.py \
    --audit-dir /path/to/outputs \
    --data-root /path/to/data/raw \
    --report-dir web/report

# 3. View it (no build step; file:// works too)
python3 -m http.server --directory web/report 8321
```

```bash
# 4. Run the checks
PYTHONPATH=src python3 -m pytest tests/ -q          # fast fixture suite
LIVECELL_DATA_ROOT=/path/to/data/raw \
  PYTHONPATH=src python3 -m pytest tests/test_integration.py -q   # full-dataset check
```

Measured on the full dataset: **~32–39 s runtime, ~197–201 MB peak memory** (peak resident set size via `resource.getrusage`; see `run.json` and `docs/06_mvp_completion_report.md`).

---

## Repository layout

```text
src/livecell_audit/   # audit package: streaming COCO reader, filename parser,
                      # analysis + deterministic exports, CLI
tests/                # fixture tests (overlap, versions, missing/malformed files,
                      # parsing, determinism) + full-dataset integration check
scripts/              # download_data.sh (provenance), build_report.py (report builder)
data/derived/         # committed small outputs: report.json, findings.json,
                      # run.json, manifests/sources.json + val_membership.json
web/prototype/        # reference interface mock (verified snapshot, no engine)
web/report/           # connected static report built from real generated data
docs/                 # design + interface spec + findings + completion report
```

## Output definitions

`python3 -m livecell_audit audit --data-root … --out-dir OUT` writes:

- `report.json` — deterministic summary: per-split counts, filename overlap, low-count records, the nine known cases with observed counts, the derived validation policy, worked examples, and run metadata.
- `findings.json` — 157 per-finding records (`cross_split_overlap`, `alternate_annotation_version`, `low_annotation_count`, plus missing/dimension/filename findings when present), each with status, evidence, and a recommended next step.
- `tables/images.jsonl` — one normalized row per COCO image record: split, ids, parsed filename fields (cell line, well, field, timestamp, crop), dimensions, local-file reconciliation, annotation count.
- `tables/annotations.jsonl` — one compact row per annotation (ids, area, bbox, polygon size). Large (~261 MB); not committed.
- `manifests/sources.json` — SHA-256 checksums and sizes of the three source COCO files.
- `manifests/val_membership.json` — derived validation membership: every validation record marked included/excluded with a reason. Policy: exclude validation records whose photo also appears in train (30 excluded, 540 included). Source files untouched, original ids and versions preserved, no polygon sets merged. This is an evaluation policy, not an official correction.
- `run.json` — command, runtime seconds, peak-memory MB, and the measurement methods.

All JSON outputs use sorted keys and sorted records, so reruns over the same inputs are byte-identical (tested, excluding run metadata paths/timings).

## Limitations

- Filename identity is physical identity: matches are by filename, not pixels. Near-duplicates are out of scope.
- The low-count rule flags review candidates; a low count alone is not proof of an error, and recovering the nine known cases is a regression check, not ML generalisation (no IsolationForest, no growth modelling in this MVP).
- No model is trained and no accuracy effect is measured or claimed.
- Desktop/mobile layout and keyboard behaviour were checked in Chromium; a real recruiter comprehension check has not been performed.

## Credit & attribution

- LIVECell dataset and annotations: Sartorius Corporate Research (Edlund et al., *Nature Methods* 2021), CC BY-NC 4.0. Upstream repository: `sartorius-research/LIVECell`.
- Overlap/duplication report (issue #26): community reporter `Alimusto`. Incomplete-annotation report (issue #21): `alxndrdiaz`, with discussion from community members. Dual-annotation explanation (issue #14): Christoffer Edlund (Sartorius).
- Audit code and report in this repository: **Saber Sojudi** (2026), for non-commercial research, portfolio, and educational purposes.
