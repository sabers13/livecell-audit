# 09. Public data contract: warehouse and features

This is the machine-readable contract for the two stages that follow the audit:

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
    finding_evidence.parquet  sources.parquet  runs.parquet  val_membership.parquet
    v1_image_annotation_stats.parquet  v1_cell_line_summary.parquet
    v1_annotation_totals.parquet  v1_finding_lookup.parquet  v1_finding_context.parquet
  integrity.json        # reconciliation, join integrity, input hashes, sizes, timings
  run.json              # paths, environment, timings, database size
  manifest.json         # size + SHA-256 for every required output (incl. database)
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
| `finding_evidence` | `(finding_position, split, image_id)` | `source` (`records`/`evidence`), `record_key`, `match_status` (`exact`/`ambiguous_image_id`/`unresolved`) |
| `sources` | `(split)` | `path`, `sha256`, `bytes`, `images_root`, `local_image_files` |
| `runs` | `(run_id)` | tool version, `completed_at`, splits, source hashes, marker counts |
| `val_membership` | `(record_position)` | `image_id`, `file_name`, `decision`, `reason` |

### Views (`v1_*`)

| View | Purpose |
| --- | --- |
| `v1_image_annotation_stats` | SQL aggregation of annotations by `(split, image_id)`: `n_annotations`, area sum/mean/median/min/max/std, bbox width/height mean+median, polygon-point mean/median/min/max/sum |
| `v1_split_membership` | every image record's split membership with its record key |
| `v1_split_overlap` | per filename: `splits`, `n_splits`, record counts, and `annotation_count_sum_records` (a per-record snapshot sum, not a source total) |
| `v1_annotation_versions` | every annotation-version record with `versions_in_split`, `records_for_filename`, `splits_for_filename` |
| `v1_alternate_versions` | filenames with more than one version in a split, with ids/positions/counts |
| `v1_cell_line_summary` | per cell line: `image_records`, `unique_filenames`, reliable `annotations_attributed`, and the labelled `annotation_count_sum_records` |
| `v1_annotation_totals` | global reconciliation: `annotations_total = annotations_attributed + annotations_ambiguous + annotations_orphan` |
| `v1_finding_lookup` | a finding's **exact** original image evidence via `finding_evidence` (version-specific; `evidence_match_status` marks ambiguity) |
| `v1_finding_context` | filename/split context for a finding, labelled `filename_split_context` (context, not evidence) |
| `v1_image_join_ambiguity` | `(split, image_id)` keys shared by several records |
| `v1_orphan_annotations` | annotation `(split, image_id)` groups with no image record |

### `integrity.json`

Deterministic apart from its `timings` block. Contains: input identity
(marker status, `completed_at`, source hashes), `input_files` (size + SHA-256 of
the audit exports actually consumed), validation checks, per-table and per-split
reconciliation (expected/loaded/delta), join integrity (ambiguity, orphans,
duplicate annotation keys), and deterministic export sizes. The DuckDB file's
own byte size is run metadata (recorded in `run.json`), because DuckDB's on-disk
page allocation is not byte-stable; the Parquet exports are.

### `manifest.json` and marker verification

Each stage publishes a `manifest.json` listing `{path, bytes, sha256}` for every
required output, including the database. The `COMPLETED` marker records the
manifest's own SHA-256 under `manifest`. A consumer must:

1. check the marker's `stage`, `schema_version`/`view_version` (warehouse) or
   `feature_schema_version` (features), and `partial`;
2. verify the manifest SHA-256 and every listed file's size/SHA-256;
3. for the feature stage, additionally cross-check the database's `runs`/
   `sources` source checksums, live table counts and `integrity.json` against the
   marker (done internally by `run_features`).

A tampered marker, a modified database, or a database swapped in from a
different run is rejected. The warehouse marker also sets `partial: false`;
the feature marker sets `partial`/`mode`.

`features.verify_features_outputs` (and `features.assert_release_ready`) go
further than the marker's own fields: they **reconcile** the marker against the
protected `manifest.json` (mode/partial/schema), the immutable
`feature_dictionary.json` provenance (source hashes, schema, mode), `run.json`
diagnostics, and the actual `features.jsonl`/`features.parquet` record counts.
A marker whose `partial`/`mode`/`source_sha256` fields were edited alone is
rejected, because the protected outputs and the real feature table still
describe the original run. (Regression test: a partial `max_filenames=1` build
whose marker is edited to `partial=false`, `mode=full` and a wrong
`source_sha256` is rejected as `not release-ready`.)

The ranking stage records the exact **feature build identity** it consumed
(`source_sha256`, `feature_manifest_sha256`, `warehouse_manifest_sha256`), and
`ranking.assert_build_consistency` refuses to combine a ranking or evaluation run
with a different feature/warehouse build — even one with the same COCO source
hashes but a different feature table (for example after TIFF pixels changed).

## 4. Feature outputs

```
<features-out>/
  features.parquet          # one row per annotation-version image record
  features.jsonl            # same rows as newline-delimited JSON
  cell_line_summary.json    # grouped summary (JSON)
  cell_line_summary.csv     # same summary (CSV)
  cell_line_summary.parquet # same summary (Parquet)
  feature_distributions.json# quantiles overall and by cell line
  feature_dictionary.json   # schema version, intensity contract, source manifest, features
  run.json                  # paths, environment, diagnostics, mode, timings
  manifest.json             # size + SHA-256 for every required output
  COMPLETED                 # written last, atomically
```

- Row key: `record_key` = `(split, record_position)`. `image_id` is a field.
- All alternate annotation versions are retained. Image-only statistics are
  computed once per distinct `file_name` and reused across versions.
- `feature_dictionary.json` is the authoritative column list: each entry has
  `name`, `dtype`, `source`, `formula`, `unit`, `depends_on_annotations`,
  `missing_behavior`, `description`, plus `feature_schema_version`, `mode`, an
  `image_statistics` contract block, and a `source_manifest` linking back to the
  audit run id and source hashes.
- `run_features` refuses a warehouse whose marker/manifest/database provenance
  do not agree (see §3). Consumers verify the feature run with
  `features.verify_features_outputs(dir)` or reject it with
  `features.assert_release_ready(dir)`, which fails on a partial or tampered
  directory.

Missing-value semantics:

| Situation | Behaviour |
| --- | --- |
| record with no annotations | `n_annotations = 0`; area/bbox/polygon summaries `null`; `zero_annotation = true` |
| ambiguous `(split, image_id)` | all annotation features `null`; `annotation_join_status = "ambiguous_image_id"`; `has_annotations`/`zero_annotation` `null` |
| missing local TIFF | pixel statistics `null`; `image_features_status = "missing_file"` |
| unreadable TIFF | pixel statistics `null`; `image_features_status = "unreadable"` |
| filename not read (partial run) | pixel statistics `null`; `image_features_status = "not_attempted"`; marker `partial = true` |
| valid uniform TIFF | `pixel_std = 0`, `gradient_mean = 0` (a measurement, not missing data) |

TIFF pixel intensities are used as stored (8-bit unsigned, 0-255 for this
dataset), with **no normalization or rescaling**. `pixel_mean`/`pixel_std` are
the mean and population standard deviation over all pixels; `gradient_mean` is
the mean magnitude of the central-difference gradient (`numpy.gradient`). These
are texture proxies, not cell counts; annotation counts are label evidence, not
true confluence. Because values are not rescaled, they are not comparable across
bit depths (a documented limitation).

## 5. CLI contract

```bash
# 1. audit
PYTHONPATH=src python3 -m livecell_audit audit \
    --data-root data/raw \
    --out-dir data/workbench/audit

# 2. warehouse
PYTHONPATH=src python3 -m livecell_audit warehouse \
    --audit-dir data/workbench/audit \
    --out-dir data/workbench/warehouse \
    --data-root data/raw

# 3. features
PYTHONPATH=src python3 -m livecell_audit features \
    --warehouse-dir data/workbench/warehouse \
    --data-root data/raw \
    --out-dir data/workbench/features
```

Python API: `WarehouseConfig` + `run_warehouse`, and `FeatureConfig` +
`run_features`, both returning a dict with `run`, `integrity`/`diagnostics`,
`marker` and `out_dir`. Consumer helpers: `completion.verify_manifest(dir, ...)`,
`features.verify_features_outputs(dir)` and `features.assert_release_ready(dir)`.

## 6. Determinism contract

Byte-identical across reruns with the same inputs: all Parquet exports,
`integrity.json` (minus `timings`), `features.jsonl`, `features.parquet`,
`cell_line_summary.*`, `feature_dictionary.json`, `feature_distributions.json`.
Variable (run metadata): `run.json`, `manifest.json`, the marker's
`completed_at`, the `timings` block, and the DuckDB file's byte size. The
feature marker's `source_sha256` is copied from the verified warehouse marker,
so it is data-derived, not run-derived.

## 7. Limitations

- The warehouse reads the audit's JSONL exports; it does not re-read the raw
  COCO files except to re-hash them.
- `record_position` for annotations is the 0-based position in the audit's
  annotation export (splits in order train, val, test); the loader verifies the
  first and last rows line up before trusting it.
- The full dataset has no ambiguous ids, no orphan annotations and no duplicate
  annotation ids; those paths are exercised by fixtures, not by the real data.
- Content manifests are integrity evidence, not a defence against an attacker
  who rewrites both the marker and the manifest.
- Feature distributions summarise observed label/image statistics. Cell-line
  differences are acquisition context, not proof of biological growth.
