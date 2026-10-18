# 10. Ranking and evaluation results

A fit-only review ranking (baselines + Isolation Forest) and three separate
kinds of evidence, implemented in `src/livecell_audit/ranking.py` and
`src/livecell_audit/evaluate.py`. The configuration is frozen in
[`09_model_card.md`](09_model_card.md); the machine-readable feature/warehouse
contract is [`09_data_contract.md`](09_data_contract.md).

**Headline:** at the same review budget, Isolation Forest did **not** beat the
existing low-count baseline. Low-count recovered 9/9 known cases in the top 10
and reached precision@K = 1.0 at every budget on the synthetic annotation-loss
task (AUC 0.964), while the Isolation Forest recovered 2–5 of the nine known
cases and reached AUC 0.846–0.873. ML did not add value here, but read this with
care: both tests are defined by label count (the nine cases were reported with a
ten-or-fewer threshold, and the simulation removes ~90% of the labels of the
corrupted images), so they favour a counting rule by design and cannot show what
a model adds beyond counting. No image is deleted or relabelled.

## 1. Environment and exact commands

Ran on the data server inside the memory-capped launcher `run-job`
(1536 MiB RAM, 1024 MiB throttle, 1 CPU, one job, 30 min). Python 3.12.15;
`numpy 1.26.4`, `pandas 3.0.3`, `duckdb 1.5.6`, `scikit-learn 1.6.1`,
`scipy 1.15.3`.

```bash
# ranking over the completed real warehouse + features
run-job .venv/bin/python -m livecell_audit rank \
    --features-dir data/workbench/features \
    --warehouse-dir data/workbench/warehouse \
    --out-dir data/workbench/ranking

# evaluation over the completed ranking
run-job .venv/bin/python -m livecell_audit evaluate \
    --features-dir data/workbench/features \
    --warehouse-dir data/workbench/warehouse \
    --ranking-dir data/workbench/ranking \
    --out-dir data/workbench/evaluation

# focused, then full test suite (real-data integration check enabled)
run-job .venv/bin/python -m pytest tests/test_ranking.py tests/test_features.py -q
run-job env LIVECELL_DATA_ROOT=data/raw .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

Reference official docs: scikit-learn
[IsolationForest](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.IsolationForest.html)
and
[GroupShuffleSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GroupShuffleSplit.html).

## 2. Candidate pools (named explicitly)

Both pools are canonical (one annotation version per filename, see the model
card) and deduplicated by filename.

| Pool | Definition | Size |
| --- | --- | --- |
| **Review pool** (A + the UI's exploratory ranking) | every canonical non-fit row: held-out training groups, the official validation split, the official test split, and known-case rows excluded from fitting | 3,067 |
| **Synthetic pool** (B) | untouched held-out training groups only, known cases excluded; each physical image once | 1,010 |

Partition: 2,172 fit rows, 1,010 held-out training rows, 539 validation rows,
1,512 test rows; 5,239 canonical filenames; 0 unusable rows; 6 training
known-case rows excluded from fit (`excluded_known_case`). Eight cell-line
models were fitted plus one global fit-only model (2,172 rows). Every fitted
cell line had ≥197 fit images, so on the real data **no** candidate used the
global fallback; the fallback is exercised by fixtures.

The pandas annotation aggregation that the synthetic stage uses was verified
against the warehouse view on a deterministic 500-image sample (pandas max abs
diff 2.9e-10) and independently in SQL (max abs diff 0.0), so recomputation uses
exactly the published feature definitions.

## 3. A. Known-case regression

Nine upstream-reported sparse-label filenames ranked against the **review pool**
(3,067). All nine reported `(split, image_id, filename)` identities are present
in the preserved provenance, all nine canonical versions equal the reported
version, and none is in fit. This is a regression exercise on nine examples, not
recall over all bad labels and not a generalisation benchmark.

Per-case rank in the default pool (seed 42):

| filename | split | annotated | IF | robust | low-count | random |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| BT474_Phase_B3_1_03d00h00m_3.tif | train | 1 | 12 | 4 | 3 | 2896 |
| A172_Phase_A7_1_01d04h00m_3.tif | train | 1 | 1 | 1 | 1 | 89 |
| A172_Phase_D7_1_01d20h00m_1.tif | train | 1 | 45 | 2 | 2 | 838 |
| BV2_Phase_C4_1_01d16h00m_3.tif | train | 4 | 151 | 291 | 8 | 349 |
| BV2_Phase_D4_1_00d12h00m_2.tif | train | 9 | 385 | 893 | 9 | 86 |
| SHSY5Y_Phase_D10_1_01d16h00m_4.tif | train | 1 | 17 | 21 | 4 | 2019 |
| SkBr3_Phase_H3_2_00d00h00m_1.tif | val | 1 | 7 | 17 | 5 | 1072 |
| BV2_Phase_A4_2_00d00h00m_1.tif | test | 2 | 248 | 159 | 6 | 1145 |
| BV2_Phase_A4_2_02d04h00m_3.tif | test | 2 | 64 | 77 | 7 | 78 |

Recovered at K (denominator = 10 / 25 / 50; pool 3,067):

| method | seed | @10 | @25 | @50 |
| --- | ---: | ---: | ---: | ---: |
| low-count | 7/42/101 | **9/9/9** | **9/9/9** | **9/9/9** |
| robust | 7/42/101 | 3/3/3 | 5/5/5 | 5/5/5 |
| Isolation Forest | 7 | 2 | 4 | 4 |
| Isolation Forest | 42 | 2 | 4 | 5 |
| Isolation Forest | 101 | 3 | 3 | 4 |
| random | 7/42/101 | 0/0/0 | 0/0/0 | 0/0/0 |

Baseline counts (annotations): the nine cases have 1,1,1,4,9,1,1,2,2
annotations, i.e. the low-count rule (`n_annotations ≤ 10`) is exactly the rule
that these nine upstream reports triggered — it recovers all nine at K=10. The
Isolation Forest, using all 24 numeric features, does not.

## 4. B. Held-out synthetic annotation-loss experiment

Untouched held-out training groups only, known cases excluded, each physical
image once. **Structured** deterministic mask (mask seed 42, fixed across model
seeds): half of the images with ≥2 annotations are selected, and the lowest 10%
(≥1) of their annotation `record_position` rows is retained; every
label-dependent feature is recomputed with the same definitions and image-only
features are reused unchanged. 505 of 1,010 images were corrupted
(168,370 → 16,849 annotations). This is a simulation with a deliberately
structured loss scenario, not random dropout, and unchanged images are not
expert-confirmed clean.

Precision@K for identifying **injected** loss (denominator = 10 / 25 / 50;
positives = 505), and synthetic ROC AUC:

| method | seed | p@10 | p@25 | p@50 | AUC |
| --- | ---: | ---: | ---: | ---: | ---: |
| low-count | 42 | 1.000 | 1.000 | 1.000 | 0.9639 |
| robust | 42 | 1.000 | 1.000 | 0.960 | 0.8000 |
| Isolation Forest | 42 | 1.000 | 1.000 | 0.980 | 0.8731 |
| Isolation Forest | 7 | 1.000 | 1.000 | 0.960 | 0.8561 |
| Isolation Forest | 101 | 1.000 | 0.960 | 0.940 | 0.8457 |
| random | 42 | 0.500 | 0.480 | 0.500 | 0.4933 |

Mean/spread across seeds 7/42/101 (model/reference seed sensitivity only; the
corruption mask and partition are fixed):

| method | p@10 mean | p@25 mean | p@50 mean | AUC mean | AUC range |
| --- | ---: | ---: | ---: | ---: | ---: |
| low-count | 1.000 | 1.000 | 1.000 | 0.9639 | 0.0000 |
| robust | 1.000 | 1.000 | 0.960 | 0.8000 | 0.0000 |
| Isolation Forest | 1.000 | 0.9867 | 0.960 | **0.8583** | 0.0274 |
| random | 0.533 | 0.520 | 0.507 | 0.5019 | 0.0400 |

Paired score deltas (corrupted − original) for the 505 injected images, median:

| method | percentile-priority delta | raw-score delta |
| --- | ---: | ---: |
| Isolation Forest | +0.257 | +0.064 (IF anomaly `-score_samples`) |
| robust | +0.238 | +0.378 (robust z) |
| low-count | +199.0 | +199.0 (fewer annotations) |
| random | 0.000 | 0.000 |

The raw Isolation Forest delta is reported alongside the percentile delta so a
finite-reference tail tie cannot hide a real score change.

## 5. C. Frozen human scientific review sample

A frozen, deterministic sample of 100 review-pool candidates: 25 disjoint
top-Isolation-Forest (seed 42), 25 top-robust-baseline, 25 top-low-count-baseline
and 25 deterministic random (seed 42). Each row carries identity, ranks and blank
`judgment` / `reason` / `reviewer_expertise` / `reviewer_id` fields.
`evaluate.verify_evaluation_outputs` rejects a sample containing a fabricated
judgment. **No scientific reviewer is currently supplied**, so no judgment is
recorded, no expert validation is claimed, and **real-world precision is
unmeasured**. The enriched (top-ranked + random) sample does not justify an
unweighted population precision estimate.

## 6. What won and what lost

- **Low-count baseline won** on every stated metric: 9/9 known-case recovery at
  K=10, precision@K = 1.0 at all budgets, and the best synthetic AUC (0.964).
- **Isolation Forest lost** to low-count: 2–5/9 known cases, synthetic AUC
  0.846–0.873.
- **Isolation Forest beat the random reference and the robust z baseline on
  synthetic AUC** (mean 0.858 vs 0.800 robust and 0.502 random), but recovered
  no more known cases than the robust baseline at any budget (fewer at K=25),
  and stayed far below low-count. It did not add value over the existing rule.
- No hyperparameter was tuned to change this; the negative result stands.
- **Why the comparison favours low-count:** the known cases were selected
  upstream with a "ten or fewer segments" threshold, and the simulation removes
  ~90% of the annotations of each corrupted image. A count rule is the target
  of both tests. They show that the Isolation Forest is not a better *counter*;
  they cannot show whether it finds label problems a count misses. That needs a
  criterion that is not a label count: the frozen human review sample, or
  agreement between the images that were annotated twice.
- **Why the Isolation Forest is weak here:** its 24 features mix label
  statistics with image texture, so it ranks unusual *images* rather than labels
  that disagree with their image; acquisition time and well are excluded; and
  three features (`width`, `height`, `image_area_pixels`) are constant because
  every LIVECell image is 704 × 520. The model card records constant features
  under `feature_selection.constant_in_fit`.

## 7. Measured runtime, memory and output sizes

| Stage | Runtime | Peak memory (process; unit) |
| --- | ---: | ---: |
| ranking (real) | 26.8 s | 340.5 MB; 400.3 MB |
| evaluation (real) | 11.3 s | 256.4 MB |
| focused tests (`test_ranking`+`test_features`) | 28.5 s | 223.8 MB |
| full suite (237 tests, real integration) | 76.9 s | ≤1.0 GB unit peak |

The ranking stage originally materialised all 1.66 M annotation records in pandas
(~950 MB). It now (a) verifies the annotation aggregation on a bounded
500-image sample, in SQL and in pandas, and (b) reads only annotation
identities/aggregates for the images it needs, so peak memory fell to ~400 MB —
a bounded approach for the 1536 MiB launcher limits.

Outputs (under `data/workbench/`): `ranking/candidates.parquet` 702 KB,
`candidates.jsonl` 7.1 MB, `partition_manifest.json` 1.34 MB,
`fit_reference.json` 508 KB, `model_card.json` 18.8 KB, `summary.json` 1.9 KB,
`version_provenance.jsonl`, `models/` (27 pipelines); `evaluation/known_cases.json`
+`.csv`, `synthetic_annotation_loss.json`, `synthetic_paired_deltas.csv`,
`review_sample.json`+`.csv`, `evaluation_summary.json`. Each stage writes
`manifest.json` + `COMPLETED` last.

## 8. Verification

- Focused: **60 passed** (`tests/test_ranking.py` 35, `tests/test_features.py`
  25) in 28.5 s at 223.8 MB peak.
- Full suite: **237 passed** in 76.9 s including the real-dataset integration
  check.
- Contract tests cover: canonical identity (train/lowest-id) and version
  provenance; known-case exclusion from fit and exact-identity recovery;
  group/filename disjointness; training-only preprocessing (perturbing held-out
  values leaves every fitted parameter and reference unchanged); the global
  fallback rule; unusable-row exclusion with a reason; zero-MAD and all-missing
  handling; equal-budget denominators; recomputation after annotation removal
  (record-position identity, duplicate-id guard); source preservation; seed
  sensitivity and byte-reproducible rankings; failure/partial-output rejection;
  mixed feature-build rejection (ranking from feature build A cannot be
  evaluated with feature build B that shares the COCO hashes but differs in TIFF
  pixels); tampered feature marker rejection; and fabricated-judgment rejection.

## 9. Data contract for consumers (website, exports)

Consume the completed runs with the helpers, not by re-deriving anything:

- `features.assert_release_ready(features_dir)` — verified feature table;
- `ranking.assert_ranking_ready(ranking_dir)` — verified ranking run;
- `evaluate.assert_evaluation_ready(evaluation_dir)` — verified evaluation run;
- `ranking.assert_build_consistency(features_dir, ranking_dir, evaluation_dir)` —
  **required before combining** artifacts into any website bundle; it rejects
  mixing runs from different feature/warehouse builds.

Machine-readable, path-free summaries for the website bundle:
`ranking/summary.json` and `evaluation/evaluation_summary.json`. Full rankings
are `ranking/candidates.jsonl`/`.parquet` (raw scores, priorities, ranks,
provenance columns); the review sample is `evaluation/review_sample.json`.
Keep raw data, databases, model files and the large candidates tables out of the
website bundle.

## 10. Limitations

- Scores are unusualness indicators, not error probabilities; a sparse image may
  be annotated correctly.
- The nine known cases are a regression exercise, not recall over all bad labels.
- The synthetic metrics are simulation metrics with a structured loss mask, not
  real annotation-error precision; unchanged images are not confirmed clean.
- Real-world precision is unmeasured (no reviewer); the review sample is frozen
  and awaiting one.
- Acquisition groups are a coarse leakage proxy (4–6 training groups per cell
  line), not proof of biological independence.
- Official test imagery is ranked only for exploration, never for tuning.
