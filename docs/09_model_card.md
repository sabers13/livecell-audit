# 09-ML. Model card — fit-only review ranking (milestone 4)

> File-name note: the machine-readable **data contract** for the warehouse and
> feature stages is [`09_data_contract.md`](09_data_contract.md). This file is
> the requested milestone-4 **model card** (a distinct document).

Scope: the Isolation Forest review ranking and its baselines, implemented in
`src/livecell_audit/ranking.py` on branch `expansion/research-workbench`.
Local-only: no cloud run, no publication, no push/merge, no UI change. The raw
dataset at `/home/saber/projects/livecell-audit/data/raw` and the original
`/home/saber/Projects/firstmate` tree were read only.

## 1. Scientific question

At the **same review budget**, does an Isolation Forest ranking offer useful
prioritisation compared with simple baselines — the existing low-count rule, a
robust statistical ranking, and a seeded random reference? A negative result is
valid. Scores indicate **unusualness**, never a probability that a label is
wrong; a sparse image may be annotated correctly. Real-world precision is
**unmeasured** (no qualified scientific reviewer was supplied).

## 2. Declared, frozen protocol

| Setting | Value |
| --- | --- |
| default seed | `42` |
| sensitivity seeds | `7`, `42`, `101` |
| review budgets | `10`, `25`, `50` images |
| Isolation Forest | `n_estimators=200`, `max_samples=min(256, n_fit)`, `contamination='auto'`, `n_jobs=1` |
| preprocessing | `Pipeline(SimpleImputer(median), RobustScaler, IsolationForest)` |
| per-cell-line minimum fit support | `20` images (else documented global fit-only fallback) |
| partition hold-out fraction | `0.25` (deterministic; seed `42`) |
| score direction | higher priority = more unusual = earlier review |

`max_samples` is capped at the fit-row count because scikit-learn warns and
substitutes `n_samples` when the cap exceeds the sample count; declaring the
cap as `min(256, n_fit)` is the justified compatibility change. No
hyperparameter was tuned on test, validation, held-out or known-case results.
`contamination='auto'` means the returned labels are not used for ranking; the
priority is built from `score_samples` (see §6).

## 3. Inputs and build identity

`ranking.run_ranking` consumes a completed **feature** run and the **warehouse**
it was built from, and refuses a mismatched pair:

1. `features.assert_release_ready(features_dir)` — reconciles the feature marker
   with its protected `manifest.json`, `feature_dictionary.json`,
   `run.json` diagnostics and the actual `features.jsonl`/`features.parquet`
   record counts;
2. the warehouse marker is an exact, non-partial `warehouse` stage;
3. the warehouse `manifest.json` SHA-256 equals the feature marker's
   `warehouse_manifest_sha256` (the database is the one the features came from);
4. the warehouse marker's `source_sha256` equals the feature marker's, and every
   warehouse manifest file matches its size/SHA-256.

The ranking stage records, and the evaluation stage re-checks, an exact **build
identity** (`ranking.feature_build_identity`):

```json
{
  "source_sha256": {"train": "...", "val": "...", "test": "..."},
  "feature_schema_version": "1",
  "feature_records": 5387,
  "feature_manifest_sha256": "<sha256 of the feature manifest.json>",
  "warehouse_manifest_sha256": "<sha256 of the warehouse manifest.json>"
}
```

`feature_manifest_sha256` protects every feature-output byte, so two feature
builds with the *same COCO source hashes* but different TIFF pixels (a different
feature table) have different identities. `ranking.assert_build_consistency`
rejects mixing a ranking or evaluation run with a different feature/warehouse
build; the future website exporter must call it before combining artifacts.

## 4. Identity and canonicalisation policy

One canonical annotation version is chosen per filename, deterministically:
**prefer an official training record, otherwise validation then test; within a
split the lowest original id, then the lowest record position.** All versions
are preserved in `version_provenance.jsonl` with their per-version annotation
counts, so the choice and the count range (`annotation_count_min`/`_max`) are
exported. This is a modelling policy, not proof that the chosen annotation is
correct; the sparsest version is never selected silently.

A record is **eligible** only when its annotation join is unambiguous
(`annotation_join_status == "ok"`) and its image statistics were computed
(`image_features_status == "ok"`). A filename with no eligible version gets no
canonical row and an explicit exclusion reason (`ambiguous_annotation_join` or
`image_features_missing_file`/`_unreadable`/`_not_attempted`); unavailable inputs
are never scored with invented values.

## 5. Grouping, partition and known-case exclusion

- Group = `(cell_line, well, field)`, so time points, crops and alternate
  versions stay together. An **unparsed filename is its own group**
  (`unparsed|<file_name>`) and is never merged into a normal group. This is a
  practical leakage proxy, not proof of biological independence.
- Within each cell line, eligible official training groups are split
  deterministically ~75/25 (fit / held-out) with at least one group on each side
  when possible. The complete assignment is exported in
  `partition_manifest.json` (per record and per cell line).
- **Only eligible official training rows in fit groups** enter
  fitting/preprocessing. Official validation and test rows, held-out groups and
  known cases never enter fitting.
- **Every known-case filename** (and all its alternate/split records) is
  excluded from fitting and preprocessing before any model sees the data. Known
  cases remain in the review pool so they can be ranked.

## 6. Features, models and scores

Predictors are numeric **observed** features only (`MODEL_FEATURES`, 24 columns:
annotation aggregates, image dimensions, texture proxies). Identifiers, split,
acquisition context and bookkeeping columns are excluded with documented
reasons (`EXCLUDED_FEATURES` in `ranking.py` and `model_card.json`). A feature
that is missing for every fit row of a model is dropped for that model
(all-missing handling); remaining nulls (for example annotation means absent
because a record has zero labels) are imputed with the model's **fit-only**
median.

A per-cell-line Isolation Forest is used where the cell line has at least
`MIN_CELL_LINE_FIT_IMAGES = 20` fit images; every other row is scored by one
**global fit-only** model fitted on all fit rows. No model is ever fitted on
validation, test, held-out or known-case rows.

The pandas annotation aggregation used to recompute label-dependent features
(the synthetic-loss stage) is verified against the warehouse view
`lc.v1_image_annotation_stats` in two independent ways: an in-SQL aggregation of
the same rows, and the pandas helper, on a deterministic, memory-bounded sample
of images (default 500). On the real data both agree exactly (SQL max abs diff
0.0; pandas 2.9e-10), so the synthetic stage cannot silently invent a second
feature definition. The focused tests check equality exhaustively on a fixture.

For Isolation Forest the raw anomaly score is
`if_raw = -score_samples` (higher = more unusual). Because scores from separate
per-cell-line models are not automatically comparable, each method's
**priority** is a fit-only empirical percentile:

```
priority = (less + 0.5 * equal) / n
```

over that model's fit-row raw scores. With a finite reference the maximum
achievable priority is `1.0`: any score at or above the reference maximum
shares it (a documented tail tie). The raw score is always preserved alongside
the percentile.

Baselines rank the **same candidate pool** with stable `file_name`/`record_key`
tie-breaking:

- `low_count` — `-n_annotations` (fewer annotations = earlier review; counts are
  directly comparable and need no rescaling);
- `robust` — mean absolute robust z-score per row using fit-only per-cell-line
  median and `1.4826·MAD`; a zero MAD falls back to the fit-only global MAD, and
  if that is also zero the feature is skipped for that row; then a fit-only
  per-cell-line empirical percentile;
- `random` — a seeded uniform reference (seed varies with the sensitivity set).

## 7. Outputs

`<ranking-out>/` (gitignored under `data/workbench/ranking/`):

| File | Role |
| --- | --- |
| `candidates.jsonl` / `.csv` / `.parquet` | complete review-pool rankings with raw scores, priorities, ranks and provenance columns |
| `version_provenance.jsonl` | every filename's chosen version and all versions with count range |
| `partition_manifest.json` | group split, per-record partition, exclusion reasons |
| `model_card.json` | exact config, feature selection, per-model fit counts, partition summary, limitations |
| `fit_reference.json` | fit-only reference distributions (raw IF anomaly scores per model/seed, robust stats) |
| `models/*.joblib` | saved per-cell-line and global pipelines (27 = 9 models × 3 seeds) |
| `summary.json` | compact machine-readable public summary (no private absolute paths) |
| `run.json` | variable run metadata (paths, versions, runtime, peak memory) |
| `manifest.json`, `COMPLETED` | content manifest + completion marker |

Schema versions: `ranking_schema_version = "1"`, `feature_schema_version = "1"`,
warehouse `schema_version ⊆ {"1"}` / `view_version = 1`.

## 8. Determinism and safety

Annotation reads are **bounded**. Neither the ranking nor the evaluation stage
materialises the 1.66 M-row annotation table in pandas: the label-dependent
features used by the synthetic annotation-loss experiment are recomputed in SQL
over only the retained `record_position` rows
(`features.aggregate_annotation_features_sql`), and the synthetic experiment
reads only the *selected* images' annotation identities (`split`, `image_id`,
`record_position`, `id`) — never their `area`/`bbox` payload. `record_position`
is the unique annotation-row identity, so duplicate annotation ids (preserved by
the warehouse) can never multiply a retained set.

Each ranking run still verifies the aggregation definitions against the
published view `lc.v1_image_annotation_stats` on a deterministic, bounded sample
of images (`annotation_reproduction` in `model_card.json`): both the SQL
recomputation and the pandas reference helper are compared column by column, and
a disagreement aborts the run instead of fabricating features. The focused tests
check the pandas helper exhaustively on a fixture. Warehouse connections are
capped to one DuckDB thread and a fixed memory ceiling
(`features.connect_warehouse`), so a read fails cleanly inside the capped job
budget rather than letting DuckDB size itself against system memory.

Every stage writes into `.staging` and publishes `COMPLETED` last, atomically; a
failed or interrupted build removes the marker. `candidates.jsonl`/`.parquet`,
`model_card.json`, `partition_manifest.json` and `fit_reference.json` are
byte-identical across reruns with the same inputs (tested). Serialized model
bytes and `run.json`/`manifest.json` timestamps are run metadata. A consumer
verifies the run with `ranking.verify_ranking_outputs` /
`ranking.assert_ranking_ready`; `evaluate.verify_evaluation_outputs` /
`evaluate.assert_evaluation_ready` does the same for evaluation.

## 9. Limitations

- Scores are unusualness indicators, not error probabilities; a sparse image may
  be annotated correctly.
- The nine known cases are a regression exercise, not recall over all bad labels.
- Acquisition groups are a coarse leakage proxy (4–6 training groups per cell
  line), not proof of biological independence.
- Official test imagery is ranked only for exploration, never for tuning.
- No calibration against real annotation error is claimed or performed; no SHAP
  or extra ML dependency was added (there is no concrete need).
- Content manifests are integrity evidence, not protection against an attacker
  who rewrites both the marker and the manifest.
