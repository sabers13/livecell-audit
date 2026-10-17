# 09. Public data contract: warehouse and features

This is the machine-readable contract for the two expansion stages added on
branch `expansion/research-workbench`:

- **warehouse** (`livecell_audit.warehouse`) — a completed audit run loaded into
  DuckDB plus typed Parquet exports;
- **features** (`livecell_audit.features`) — a documented pandas feature table
  and cell-line summaries.

The next stage (scikit-learn ranking) should consume `features.parquet` and
`feature_dictionary.json` and nothing else from this contract unless it needs
the raw evidence tables.

Version identifiers (all currently `1`):

| Identifier | Where | Meaning |
| --- | --- | --- |
| `schema_version` | warehouse marker + `integrity.json` | DuckDB schema revision |
| `view_version` | warehouse marker + `integrity.json` | SQL view revision (view names are prefixed `v1_`) |
| `feature_schema_version` | features marker + `feature_dictionary.json` | feature-table revision |

## 1. Input contract — a completed audit run

A warehouse build consumes a directory that the audit produced and refuses
anything else. Required files:

| File | Role |
| --- | --- |
| `COMPLETED` | completion marker; must parse and declare `status == "completed"` |
| `tables/images.jsonl` | one row per image record (5,387 for the full dataset) |
| `tables/annotations.jsonl` | one row per annotation (1,662,447) |
| `findings.json` | audit findings array |
| `manifests/sources.json` | per-split COCO SHA-256 and byte size |
| `manifests/val_membership.json` | derived validation membership |
| `report.json`, `run.json` | audit report and run metadata |

Before writing any table the builder checks the marker, cross-checks
`marker.source_sha256` against `sources.json`, optionally re-hashes the raw COCO
files when `--data-root` is given, and reconciles record/annotation/filename
counts against the marker and the per-split report. Any mismatch aborts the
build (`WarehouseError`) and the output directory is left without a marker.

## 2. Identity and join rules

- **Image-record key** = `(split, record_position)`, exposed as `record_key`
  (`"<split>:<position>"`). This is the only key treated as unique.
- **`id`** (original COCO image id) is a field scoped to a split, not a global
  key. It may be ambiguous.
- **`file_name`** is the *logical* image identity (one acquisition). Two records
  sharing a filename are **alternate annotation versions**, preserved, never
  merged and never counted as corruption.
- **Ambiguous `(split, image_id)`**: when several image records share an id, the
  annotation join is ambiguous. The warehouse exposes the groups and does not
  guess; the feature stage quarantines them (annotation features null,
  `annotation_join_status = "ambiguous_image_id"`).
- **Orphan annotations** (an `image_id` with no image record) are preserved in
  the raw `annotations` table and reported; they are never attached to a record.

## 3. Warehouse outputs

```
<warehouse-out>/
  audit.duckdb          # DuckDB database, schema `lc`
  parquet/              # typed Parquet exports (deterministic)
    images.parquet  annotations.parquet  findings.parquet
    sources.parquet  runs.parquet  val_membership.parquet
    v1_image_annotation_stats.parquet  v1_cell_line_summary.parquet
  integrity.json        # reconciliation, join integrity, sizes, timings
  run.json              # paths, environment, timings, database size
  COMPLETED             # written last, atomically
```

The DuckDB catalog is named `audit` (from the file name); the schema is `lc`,
so queries read `SELECT * FROM lc.images`.

### Tables (schema `lc`)

| Table | Key | Notable columns |
| --- | --- | --- |
| `images` | `(split, record_position)` | `record_key`, `id`, `file_name`, `original_filename`, acquisition fields, `dimensions_status`, `annotation_count` |
| `annotations` | `(split, id)` | `record_position`, `image_id`, `area`, `bbox DOUBLE[4]`, `n_points` |
| `findings` | `(record_position)` | `finding_id`, `type`, `status`, `file_name`, `splits`, `records JSON`, `evidence JSON` |
| `sources` | `(split)` | `path`, `sha256`, `bytes`, `images_root`, `local_image_files` |
| `runs` | `(run_id)` | tool version, `completed_at`, splits, source hashes, marker counts |
| `val_membership` | `(record_position)` | `image_id`, `file_name`, `decision`, `reason` |

### Views (`v1_*`)

| View | Purpose |
| --- | --- |
| `v1_image_annotation_stats` | SQL aggregation of annotations by `(split, image_id)`: `n_annotations`, area sum/mean/median/min/max/std, bbox width/height mean+median, polygon-point mean/median/min/max/sum |
| `v1_split_membership` | every image record's split membership with its record key |
| `v1_split_overlap` | per filename: `splits`, `n_splits`, record/annotation counts per split |
| `v1_annotation_versions` | every annotation-version record with `versions_in_split`, `records_for_filename`, `splits_for_filename` |
| `v1_alternate_versions` | filenames with more than one version in a split, with ids/positions/counts |
| `v1_cell_line_summary` | cell-line aggregates (records, filenames, annotation counts, zeros) |
| `v1_finding_lookup` | a finding joined to its original image record(s) for evidence retrieval |
| `v1_image_join_ambiguity` | `(split, image_id)` keys shared by several records |
| `v1_orphan_annotations` | annotation `(split, image_id)` groups with no image record |

### `integrity.json`

Deterministic apart from its `timings` block. Contains: input identity
(marker status, `completed_at`, source hashes), validation checks, per-table and
per-split reconciliation (expected/loaded/delta), join integrity (ambiguity,
orphans, duplicate annotation keys), and deterministic export sizes. The
DuckDB file's own byte size is run metadata (recorded in `run.json`), because
DuckDB's on-disk page allocation is not byte-stable; the Parquet exports are.

## 4. Feature outputs

```
<features-out>/
  features.parquet          # one row per annotation-version image record
  features.jsonl            # same rows as newline-delimited JSON
  cell_line_summary.json    # grouped summary (JSON)
  cell_line_summary.csv     # same summary (CSV)
  cell_line_summary.parquet # same summary (Parquet)
  feature_distributions.json# quantiles overall and by cell line
  feature_dictionary.json   # schema version, source manifest, per-feature metadata
  run.json                  # paths, environment, diagnostics, timings
  COMPLETED                 # written last, atomically
```

- Row key: `record_key` = `(split, record_position)`. `image_id` is a field.
- All alternate annotation versions are retained. Image-only statistics are
  computed once per distinct `file_name` and reused across versions.
- `feature_dictionary.json` is the authoritative column list: each entry has
  `name`, `dtype`, `source`, `formula`, `unit`, `depends_on_annotations`,
  `missing_behavior`, `description`, plus `feature_schema_version` and a
  `source_manifest` linking back to the audit run id and source hashes.

Missing-value semantics:

| Situation | Behaviour |
| --- | --- |
| record with no annotations | `n_annotations = 0`; area/bbox/polygon summaries `null`; `zero_annotation = true` |
| ambiguous `(split, image_id)` | all annotation features `null`; `annotation_join_status = "ambiguous_image_id"`; `has_annotations`/`zero_annotation` `null` |
| missing local TIFF | pixel statistics `null`; `image_features_status = "missing_file"` |
| unreadable TIFF | pixel statistics `null`; `image_features_status = "unreadable"` |
| valid uniform TIFF | `pixel_std = 0`, `gradient_mean = 0` (a measurement, not missing data) |

TIFF pixel intensities are used as stored (8-bit unsigned, 0-255 for this
dataset), no rescaling. `pixel_mean`/`pixel_std` are the mean and population
standard deviation over all pixels; `gradient_mean` is the mean magnitude of
the central-difference gradient (`numpy.gradient`). These are texture proxies,
not cell counts; annotation counts are label evidence, not true confluence.

## 5. CLI contract

```bash
# 1. audit (unchanged, milestone 1)
PYTHONPATH=src python3 -m livecell_audit audit \
    --data-root /home/saber/projects/livecell-audit/data/raw \
    --out-dir /tmp/opencode/fixed-out

# 2. warehouse
PYTHONPATH=src python3 -m livecell_audit warehouse \
    --audit-dir /tmp/opencode/fixed-out \
    --out-dir data/workbench/warehouse \
    --data-root /home/saber/projects/livecell-audit/data/raw

# 3. features
PYTHONPATH=src python3 -m livecell_audit features \
    --warehouse-dir data/workbench/warehouse \
    --data-root /home/saber/projects/livecell-audit/data/raw \
    --out-dir data/workbench/features
```

Python API: `WarehouseConfig` + `run_warehouse`, and `FeatureConfig` +
`run_features`, both returning a dict with `run`, `integrity`/`diagnostics`,
`marker` and `out_dir`.

## 6. Determinism contract

Byte-identical across reruns with the same inputs: all Parquet exports,
`integrity.json` (minus `timings`), `features.jsonl`, `features.parquet`,
`cell_line_summary.*`, `feature_dictionary.json`, `feature_distributions.json`.
Variable (run metadata): `run.json`, the marker's `completed_at`, the
`timings` block, and the DuckDB file's byte size.

## 7. Limitations

- The warehouse reads the audit's JSONL exports; it does not re-read the raw
  COCO files except to re-hash them.
- `record_position` for annotations is the 0-based position in the audit's
  annotation export (splits in order train, val, test); the loader verifies the
  first and last rows line up before trusting it.
- The full dataset has no ambiguous ids, no orphan annotations and no duplicate
  annotation ids; those paths are exercised by fixtures, not by the real data.
- Feature distributions summarise observed label/image statistics. Cell-line
  differences are acquisition context, not proof of biological growth.
