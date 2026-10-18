# 12. Release procedure

This document describes the single reproducible entrypoint for the whole pipeline
(audit → warehouse → features → rank → evaluate → review-site build → site check →
portable package), what a run records, where the release receipt lives, and exactly
what has and has not been verified.

**No container is used or assumed.** Docker, podman and buildah were inspected on the
data server and are not available there, so the release runs directly in the project
virtualenv through the existing `run-job` launcher. No container image is built and none
is claimed. This is a documented limitation of the environment, not a pending step.

## 1. What one run does

`tools/run_release.sh` is the only entrypoint. It takes a data root and an output root:

```bash
cd <PROJECT>
tools/run_release.sh --data-root <DATA_ROOT> --out-root <OUT_ROOT>
```

Stages, in order (each one writes into this run's directory only):

| # | Stage | Command it runs | Output directory (inside the run) |
| ---: | --- | --- | --- |
| 1 | `audit` | `python -m livecell_audit audit` | `audit/` |
| 2 | `warehouse` | `python -m livecell_audit warehouse` | `warehouse/` |
| 3 | `features` | `python -m livecell_audit features` | `features/` |
| 4 | `ranking` | `python -m livecell_audit rank --seed 42 --budgets 10 25 50` | `ranking/` |
| 5 | `evaluation` | `python -m livecell_audit evaluate --budgets 10 25 50` | `evaluation/` |
| 6 | `site-seed` (only with previews) | `python tools/build_review_site.py` (provisional) | `site-seed/` |
| 7 | `previews` (optional) | `python tools/make_previews.py` (bounded: ≤ 40 PNGs, ≤ 704 px) | `site-inputs/previews/` |
| 8 | `site` | `python tools/build_review_site.py` (final, with previews when rendered) | `site/` |
| 9 | `site-check` | `python tools/check_review_site.py` | (log only) |

The provisional `site-seed` build exists because the preview renderer consumes the
deterministic curated-example list that the site builder produces; the final `site`
build then picks up the rendered previews. With `--no-previews` the chain runs
`site` once and the site declares previews as pending.

Each stage runs as its own `run-job` invocation (one job at a time, 1536 MiB RAM cap,
1 CPU, 30-minute timeout), so a stage's timeout and accounting stay independent. Use
`--single-job` only when the entrypoint is already inside a launcher job; nested
`run-job` calls are refused by the launcher's lock, and the entrypoint refuses to nest
by itself.

Run directories are immutable: `<OUT_ROOT>/<UTC timestamp>-<input-hash prefix>/`
is created once and never reused, and a new run never overwrites a previous one. The
input-hash prefix comes from the pre-run fingerprint (the three COCO files' SHA-256
plus the image-set inventory, and per-image TIFF SHA-256 when `--tiff-identity` is
used), so the run ID identifies the exact inputs it was built from.

## 2. What a run records

| Path | Contents |
| --- | --- |
| `provenance/input-fingerprint.json` | COCO sizes/hashes, image-set count/bytes/inventory hash, the input hash used in the run ID |
| `provenance/tiff-identity.json` | per-image SHA-256 of every raw image read (with `--tiff-identity`) |
| `records/run-start.json` | code identity (git head/tree/dirty, code fingerprint), environment (Python, platform, pinned packages, cgroup limits), data identity |
| `records/stage-<stage>.json` | one record per stage: exact command, configuration, seeds, input identity, every cross-stage check, output tree hash and per-file hashes, log hash, wall time, peak memory and its measurement method, exit code |
| `logs/<stage>.log` | full stage output |
| `records/chain-verification.json` | post-run re-verification of every stage marker, manifest, cross-link and recorded output hash |
| `records/release-receipt.json` | the run's release receipt (see §4) |
| `records/package.json` | the packaged archive's path, size, SHA-256 and verification result |
| `COMPLETED` | pipeline completion marker, written last and atomically |

Peak memory is measured inside the capped job with
`resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` around the stage command, together with
the cgroup values (`/sys/fs/cgroup/memory.max`, `memory.peak`) that the launcher
actually enforces; the measurement method is recorded per stage rather than assumed.
`/usr/bin/time -v` is not required.

Stage discipline, enforced by `tools/release_tools.py`:

- a stage refuses to start unless every input stage has a valid `COMPLETED` marker and
  passes its cross-stage identity checks (COCO hashes, warehouse manifest hash, feature
  manifest hash, ranking manifest hash);
- the evaluation stage's recorded `ranking_build.ranking_manifest_sha256` must equal the
  ranking `manifest.json` hash of the run it is packaged with, and
  `ranking.assert_build_consistency` must accept the features/ranking/evaluation triple;
- a stage writes its run record only after its marker and required outputs verify, and a
  failed stage writes a failure record and exits non-zero;
- the pipeline `COMPLETED` marker is written only after `verify-chain` passes.

## 3. Portable release package

```bash
tools/run_release.sh package --run <OUT_ROOT>/<RUN_ID>
# or, in one go:
tools/run_release.sh --data-root <DATA_ROOT> --out-root <OUT_ROOT>     # chains + packages
```

The ZIP is written to `<run>/release/livecell-audit-release-<RUN_ID>.zip` with a
`.sha256` sidecar and contains:

- `site/` — the static review website (open `site/index.html`, or serve it);
- `tables/ranking/`, `tables/evaluation/` — ranking and evaluation tables and summaries
  (CSV/JSON/JSONL only; Parquet is excluded);
- `docs/` — the repository README, the model card, the data contract, the
  ranking/evaluation results, the review-interface spec and this release document;
- `provenance/` — input fingerprint, optional per-image TIFF hashes, environment, code
  identity and the run receipt;
- `RELEASE_RECEIPT.json`, `RELEASE_NOTES.md`, `README.md`, `DEPLOY.md`, `DEMO.md`;
- `checksums.sha256` (SHA-256 of every packaged file);
- `THIRD_PARTY_NOTICES.txt` — attribution, licences and the preview change notice.

Excluded, and refused by an automatic scan: raw TIFFs, the COCO JSON, the DuckDB
database, model binaries (`*.joblib`), Parquet, secrets, absolute machine paths and
private names. Absolute paths inside the packaged documentation are replaced by
`<ABSOLUTE_PATH>`-style placeholders and the server's SSH target by `<ssh-target>`; the
repository copies of those documents are unmodified. The receipt files are redacted the
same way (see §4). The ZIP is then unzipped to a temporary directory where
`checksums.sha256` is re-checked and every relative link, asset, download and preview in
the site is re-resolved.

## 4. Release receipt

The receipt exists in two places:

- `<run>/records/release-receipt.json` — the local record, including machine paths;
- `<package>/RELEASE_RECEIPT.json` and `<package>/provenance/run-receipt.json` — the
  portable, path-redacted copy that ships inside the ZIP.

It states the release/run ID, code identity (git revision and dirty state), environment,
input fingerprint (COCO hashes, image inventory, optional per-image hashes), every
stage's command, configuration, timings, peak memory, marker hash and output tree hash,
the verification results, the declared exclusions and redactions, the limitations, the
deferred items, and the attribution/licence summary. It also carries a `reference_run`
block: the one completed end-to-end run this tooling ships against (§5), with its
chain-verification counts, input fingerprint, audit/warehouse/evaluation facts, the
review-site check result, the hash-verified previews, each stage's peak memory and the
packaged archive's file name, byte size and SHA-256. That block contains ids, counts,
hashes and sizes only — no machine paths — so it survives redaction unchanged.

The portable copy is redacted field by field: machine-specific values (the repository,
data root, output root and virtualenv paths, the run directory, the account home directory
and account name, and any `user@host` SSH target) are replaced by portable
`<repo>` / `<run>` / `<data-root>` / `<out-root>` / `<venv>` / `<home>` / `<user>` /
`<ssh-target>` tokens before the receipt is written, so the packaging guard's absolute-path
and private-name scan passes. The receipt carries a `redaction_map` naming every placeholder
that was used, what it stands for and the SHA-256 of the withheld value; that keeps the
redaction auditable without shipping the machine value. Hashes, versions and timings are
copied through unchanged.

## 5. What has been verified, and what has not

### 5.1 The completed end-to-end run

The whole chain (audit → warehouse → features → rank → evaluate → preview render → site →
site check → package) has been run on the data server and packaged; the latest run is the
reference. Its outputs were checked against the run records, the chain verification and the
packaged archive. Those verified facts are:

| Fact | Value |
| --- | --- |
| Release run ID | `20261018T220218Z-0e359b9bf088` (18 October 2026) |
| Chain verification | 92 checks, 0 failures |
| Input fingerprint | the three raw COCO files' SHA-256, plus all 5,239 raw images fingerprinted |
| Audit counts | 5,387 image records, 1,662,447 annotations, 5,239 distinct filenames |
| Warehouse | schema v1, views v1, all reconciliations clean |
| Evaluation ↔ ranking | consumed ranking manifest `1289e9499402de2d9c33641409d6e6be34eadba6f0eda517e2d234c32ff04e61`; exact match against the ranking stage's manifest |
| Review-site check | 1,120 checks, 0 failures, 1 skipped (JavaScript syntax: node is not installed on the server; checked with `node --check` on the development machine) |
| Rendered previews | 39 of 39 previews (native 704 × 520 px) written and hash-verified, plus 30 full label layers for the dense images and the two label versions of the overlap example |
| Release ZIP | `livecell-audit-release-20261018T220218Z-0e359b9bf088.zip`, 10,925,730 bytes, SHA-256 `dbe585c491b8d428804115f311a2521ad849a3baa56648101cda7afb8f83a582`, `verified: true` |

Rerunning on the same inputs reproduces the ranking candidates, partition, synthetic
experiment, review sample and audit findings byte for byte.

Per-stage peak memory for that run (MB, process peak RSS of each stage command via
`getrusage(RUSAGE_CHILDREN).ru_maxrss`, one job at a time):

| audit | warehouse | features | ranking | evaluation | previews | site |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 199 | 323 | 275 | 340 | 350 | 60 | 111 |

The launcher's cgroup `memory.peak` is higher because it also counts page cache: it reached
about 1 GiB (the throttle) in the audit, warehouse and features stages, and the features
stage used up to 240 MB of job swap. No stage reached the 1536 MiB limit.

The receipt carries the same facts as a machine-readable `reference_run` block (see §4),
so a later package still names the run that proved this tooling.

### 5.2 Verified static checks

Verified for this tooling on the development machine (dataset-free):

- `bash -n` on `tools/run_release.sh` and `python -m py_compile` on
  `tools/release_tools.py`;
- the packaging and verification path exercised end to end against a synthetic run
  directory (assemble → checksum → unzip → re-resolve links → leak scan), including the
  receipt redaction described in §4;
- the pipeline's own stage contracts, which are covered by the project's test suite
  (`tests/test_ranking.py`, `tests/test_features.py`, `tests/test_warehouse.py`,
  `tests/test_audit.py`, `tests/test_integration.py`, `tests/test_release_tools.py`,
  `tests/test_review_previews.py`).

### 5.3 Not verified, and therefore not claimed

- no container build was performed: Docker, podman and buildah are unavailable on the data
  server (inspected), so containers are explicitly **not** verified;
- no cloud run, no publication, no public website integration, no scheduled execution;
- no independent scientific review: real-world precision/recall remains unmeasured, and
  the review sample ships with blank judgments.

## 6. Resource envelope and limits

Every stage stayed inside the launcher's 1536 MiB limit. The process peaks of the
completed release run (`20261018T220218Z-0e359b9bf088`, peak RSS, one job at a time) are:
audit 199 MB, warehouse 323 MB, features 275 MB, ranking 340 MB, evaluation 350 MB,
previews 60 MB, site 111 MB. Counting page cache, the cgroup reached about 1 GiB in the
audit, warehouse and features stages, and the features stage used up to 240 MB of swap. Peak memory per stage is recorded in each stage record, so a regression is
visible rather than assumed.
Limits are never raised automatically after a failure, and stages are not run in parallel.

## 7. Deferred (out of scope for this local release)

- Cloud execution and any paid resource.
- Public website integration/deployment. `DEPLOY.md` in the package gives generic
  static-hosting and rollback steps.
- Upstream contributions to the LIVECell repository.
- Container packaging (runtime unavailable, see above).
- Applying the output of this audit to the dataset: no image is deleted or relabelled.

## 8. Attribution and licensing

- LIVECell dataset and annotations: Sartorius Corporate Research (Edlund et al.,
  *Nature Methods* 2021), **CC BY-NC 4.0**; upstream software is **MIT**.
- Community reports preserved in the evidence: `ivan-ea` (upstream issue #21,
  incomplete annotations), `OasisArtisan` (issue #26, cross-split overlap/duplicates), and the
  alternate-annotation-version discussion in issue #14 (Christoffer Edlund, Sartorius).
- Rendered previews are transformed copies (TIFF → downsampled PNG with supplied outlines
  overlaid); the transformation is disclosed in `THIRD_PARTY_NOTICES.txt`.
- This work is independent and non-commercial; it is not affiliated with, endorsed by or
  connected to Sartorius. No real-world precision, recall or expert judgement is claimed.
