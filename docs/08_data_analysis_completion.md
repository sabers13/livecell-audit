# 08. Data warehouse and feature analysis: completion record

Milestones 2 (DuckDB/SQL/Parquet warehouse) and 3 (pandas feature analysis) of
the roadmap, implemented on the local branch `expansion/research-workbench`,
starting from commit `d5eace0` (clean). Local-only: no remote, no push, no
merge, no publication, no cloud resource, no ML or UI change. The raw dataset at
`/home/saber/projects/livecell-audit/data/raw` and the original
`/home/saber/Projects/firstmate` tree were read only; generated outputs live
under the gitignored `data/workbench/`.

The public data contract is in [`09_data_contract.md`](09_data_contract.md).

## 1. Environment

- Python 3.12.3, project-local virtual environment `.venv` (never global).
- Pins (`requirements.txt` / `requirements-dev.txt`), recorded from the tested
  environment:

  | Package | Version |
  | --- | --- |
  | duckdb | 1.5.6 |
  | numpy | 1.26.4 |
  | pandas | 3.0.3 |
  | Pillow | 12.3.0 |
  | pytest | 9.1.1 |

- PyArrow was **not** installed: DuckDB writes Parquet natively and pandas reads
  and writes through DuckDB, so no extra dependency was added.
- Recreate the environment:

  ```bash
  cd /home/saber/projects/livecell-audit-expansion
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-dev.txt
  ```

## 2. What was built

- `src/livecell_audit/warehouse.py` — validates a completed audit run, loads its
  exports into DuckDB with explicit JSON column types and DDL, creates versioned
  SQL views, exports typed Parquet, and writes a reconciling `integrity.json`.
- `src/livecell_audit/features.py` — builds the pandas feature table by joining
  SQL annotation aggregates, image metadata and once-per-filename TIFF
  statistics with validated keys; emits cell-line summaries, quantile
  distributions and a feature dictionary.
- `src/livecell_audit/completion.py` — shared staged-promotion and atomic
  completion-marker helpers for the new stages.
- `src/livecell_audit/cli.py` — adds `warehouse` and `features` subcommands; the
  existing `audit` command is unchanged and `scripts/build_report.py` still
  consumes audit outputs.
- `tests/test_warehouse.py`, `tests/test_features.py` — 25 focused tests.

## 3. Exact commands

```bash
cd /home/saber/projects/livecell-audit-expansion

# warehouse: completed audit -> DuckDB + Parquet
env PYTHONPATH=src .venv/bin/python -m livecell_audit warehouse \
    --audit-dir /tmp/opencode/fixed-out \
    --out-dir data/workbench/warehouse \
    --data-root /home/saber/projects/livecell-audit/data/raw

# features: warehouse -> pandas feature table + summaries
env PYTHONPATH=src .venv/bin/python -m livecell_audit features \
    --warehouse-dir data/workbench/warehouse \
    --data-root /home/saber/projects/livecell-audit/data/raw \
    --out-dir data/workbench/features

# tests (focused, then full suite)
env PYTHONPATH=src .venv/bin/python -m pytest tests/test_warehouse.py tests/test_features.py -q
env PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

## 4. Real-data results

Run over the completed audit at `/tmp/opencode/fixed-out` and all 5,239 TIFFs.

| Check | Result |
| --- | --- |
| Warehouse image records | 5,387 (expected 5,387, delta 0) |
| Warehouse annotations | 1,662,447 (expected 1,662,447, delta 0) |
| Distinct filenames | 5,239 (delta 0) |
| Findings / val-membership rows | 157 / 570 |
| Ambiguous `(split, image_id)` groups | 0 |
| Orphan annotation records | 0 |
| Duplicate annotation keys | 0 |
| Feature records | 5,387 |
| Distinct filenames read for image stats | 5,239 (once each) |
| Annotations attached to feature rows | 1,662,447 (equals the audit total) |
| Alternate-version feature records | 236 |
| Cross-split-overlap feature records | 60 (30 filenames × 2 records) |
| Zero-annotation / missing-file / unreadable records | 0 / 0 / 0 |
| Image statistics computed | 5,387 / 5,387 (`ok`) |

Source-checksum validation: the marker/sources cross-check passed for all three
splits, and the raw COCO files were re-hashed and matched the audit
(`source_checksums_raw_files = pass`).

### Measured sizes

| Artifact | Bytes | Note |
| --- | ---: | --- |
| `audit.duckdb` | 66,596,864 (~63.5 MiB) | queryable database |
| `annotations.parquet` | 35,972,407 (~34.3 MiB) | vs 261,281,470 (~249 MiB) JSONL |
| `images.parquet` | 114,697 | vs 3,097,603 JSONL |
| `v1_image_annotation_stats.parquet` | ~432 KB | per-image annotation aggregates |
| `features.parquet` | 606,971 | 5,387 rows |
| `features.jsonl` | 7,025,474 | same rows as JSON |

### Measured timings (single machine, warm cache; run-to-run variance is large)

| Stage | Observation |
| --- | --- |
| Warehouse total | ~13–27 s across runs (load+views ~10–17 s, Parquet export ~0.4–3.9 s) |
| Features total | ~65–71 s (5,239 TIFF reads dominate) |
| Warehouse peak RSS | ~0.78 GB during the DuckDB load |
| Features peak RSS | ~0.31 GB |

Representative aggregation (annotations per cell line, joined images ↔
annotations), measured by the warehouse itself:

| Source | Runtime |
| --- | --- |
| DuckDB tables/view | ~0.014–0.016 s |
| Raw JSONL via `read_json` | ~0.23–1.6 s |

This is an order-of-magnitude difference on a warm-cache, single-machine
measurement for one query shape. It is **not** a general speedup promise: the
warehouse is also ~7× smaller on disk for annotations, but the honest claim is
the measured comparison, not a guaranteed factor.

### Observed cell-line context (not a biological claim)

Grouped means from `cell_line_summary.json` (annotation counts are label
evidence, not cell counts): BV2 has the highest mean annotations/record (~618)
and highest mean contrast (`pixel_std` ~17.2); Huh7 the lowest (~58) and lowest
contrast (~6.9). Huh7 and SKOV3 are the cell lines carrying the alternate
annotation versions and the train/val filename overlap. These differences are
acquisition/label context and are reported as observations, not errors.

## 5. Tests

- New focused tests: **25 passed** (`tests/test_warehouse.py`,
  `tests/test_features.py`).
- Full suite: **185 passed** in 60.4 s (the 160 milestone-0/1 tests plus the new
  25; includes the real-dataset integration check).
- Coverage of the delivered contract, not implementation branches: row-count
  reconciliation, marker enforcement, marker/sources and raw-file checksum
  mismatches, ambiguous-id and orphan detection, alternate-version preservation,
  empty/zero-label records, missing/unreadable TIFFs, hand-computable feature
  formulas, feature-dictionary/column agreement, failed-build safety, and
  byte-deterministic Parquet/JSON outputs.

## 6. Determinism and safety contract

- Both stages write into a hidden `.staging` tree, promote it into place only
  after every output exists, and publish `COMPLETED` last, atomically. A failed
  build (including input-validation failure) clears any previous marker first,
  so a partial directory can never be mistaken for a completed run.
- Parquet exports, `integrity.json` (minus its `timings` block),
  `features.jsonl`, `features.parquet`, the summaries and the feature dictionary
  are byte-identical across reruns with the same inputs (verified on fixtures
  and on the real data). Run metadata (`run.json`, marker timestamps, the
  `timings` block, and the DuckDB file's byte size) is explicitly variable.

## 7. Output paths

- Warehouse: `data/workbench/warehouse/` → `audit.duckdb`, `parquet/`,
  `integrity.json`, `run.json`, `COMPLETED`.
- Features: `data/workbench/features/` → `features.parquet`, `features.jsonl`,
  `cell_line_summary.json`/`.csv`/`.parquet`, `feature_distributions.json`,
  `feature_dictionary.json`, `run.json`, `COMPLETED`.
- Both directories are gitignored; no generated heavy artifact is committed.

## 8. Limitations

- The feature table keys rows by `(split, record_position)`; `image_id` is a
  field, not a key. The real dataset has no ambiguous ids or orphans, so those
  paths are verified by fixtures only.
- Image statistics are texture proxies (`pixel_std`, `gradient_mean`) computed
  on stored 8-bit intensities; they are not cell counts, and annotation area is
  not true confluence.
- TIFFs are read once per distinct filename, sequentially; runtime is dominated
  by I/O. No parallelism was added to keep output deterministic.
- `record_position` for annotations is the audit export position; the loader
  guards it by checking the first and last rows, not every row.
- No ML, UI, deployment, or cloud work was performed, and no raw file was
  modified.

## 9. Files changed

- Added: `src/livecell_audit/warehouse.py`, `src/livecell_audit/features.py`,
  `src/livecell_audit/completion.py`, `tests/test_warehouse.py`,
  `tests/test_features.py`, `requirements.txt`, `requirements-dev.txt`,
  `docs/08_data_analysis_completion.md`, `docs/09_data_contract.md`.
- Modified: `src/livecell_audit/cli.py` (new subcommands), `pyproject.toml`
  (dependencies, Python 3.12), `.gitignore` (`.venv/`, `data/workbench/`),
  `README.md`.
