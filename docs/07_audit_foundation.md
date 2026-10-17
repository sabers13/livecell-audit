# 07. Audit foundation: baseline, repairs, and measured results

Milestones 0 (baseline) and 1 (repair the audit) of the roadmap, implemented on
the isolated local branch `expansion/audit-foundation`. Local-only: no remote,
no push, no merge, no publication, no global configuration change. The raw
dataset and the original `/home/saber/projects/livecell-audit` tree were read
only; generated outputs live outside the repository.

## 1. Starting point and preservation

- Starting commit (unmodified baseline): `a80645648d69614e1e49e640759905c17f24a395`, working tree clean.
- Environment: Python 3.12.3, Pillow 12.3.0, pytest 9.1.1. The audit is standard library plus Pillow; no new runtime dependency was added.
- Source surface (SHA-256, baseline) — reproduced from the untouched tree:

  | File | SHA-256 |
  | --- | --- |
  | `src/livecell_audit/audit.py` | `94787504ac7405d1d97f2cd40f3add8a366ab5f4d7e72b856516a2dbfa170c07` |
  | `src/livecell_audit/coco_stream.py` | `d29af91ab21a77502f3c9485ba06db5d39fd7fce3ed2ccf9fd4ba45b90299514` |
  | `src/livecell_audit/cli.py` | `c347da2a32d509f147c709b760de1c0b788dc2f1202be138130c0cd2b58445ea` |
  | `src/livecell_audit/filenames.py` | `2cc5182781493dcfac28bfe38d98a1bd77f0410e85e3d56ebee1e76bc78f8dac` |
  | `src/livecell_audit/known_cases.py` | `709e5aa4d91cc69fe280435aa844b226216eaaa55059458a197512d327ec9c9e` |
  | `scripts/build_report.py` | `f598130e40ed9844fc38d2b685eee5ecb7c4a4dad95d809839ba4257fc09d7f0` |

- Source-data checksums (read-only, `/home/saber/projects/livecell-audit/data/raw/annotations`):

  | Split | SHA-256 | Bytes |
  | --- | --- | ---: |
  | train | `6788902479bc0ffd2838fb7160a778078f464ea6034ed76bb5a0e40263cb9e78` | 551,131,789 |
  | val | `eb72a68f5065d9ade1805b496eecdcd14ca275e5f1bc6aae7e07279bddadfdc7` | 97,272,542 |
  | test | `9eb5805ca031cebcd70bab4a4aa0c89d98646c206d333803a8d5b10ea7fe9960` | 260,794,837 |

  5,239 local image files under `images/`.

## 2. Reproduced baseline (before any behaviour change)

Fresh runs on the unmodified `a806456`:

| Check | Command | Result |
| --- | --- | --- |
| Fixture suite | `PYTHONPATH=src python3 -m pytest tests/ -q` | 22 passed (27.8 s) |
| Integration (isolated) | `LIVECELL_DATA_ROOT=… PYTHONPATH=src python3 -m pytest tests/test_integration.py -q` | 1 passed (27.6 s) |
| Fresh full audit | `PYTHONPATH=src python3 -m livecell_audit audit --data-root … --out-dir /tmp/opencode/baseline-out` | 27.84 s, 192.785 MB peak |
| Independent parser probe | `PYTHONPATH=src python3 /tmp/livecell-supervisor-parser-probe.py` | 138 checks, **36 failures** (24 invalid-accepted, 12 valid-rejected) |

Baseline audit reproduced every snapshot number exactly: 5,387 image records,
5,239 distinct filenames, 1,662,447 annotations, 0 missing files, 30 train/val
overlaps, 9 low-count records, 9/9 known sparse cases. Evidence preserved at
`/tmp/opencode/baseline-out/`, `/tmp/opencode/baseline-report.json`,
`/tmp/opencode/baseline-run.json`.

## 3. Reproduced failures

1. **COCO section ordering.** With `annotations` before `images`, an
   annotations-first fixture gave `orphan_annotations=3` and per-image counts
   of 0, while the same content with `images` first gave `orphans=0` and
   correct counts — the single streaming pass only counted annotations whose
   image had already been seen.
   Command: see `tests/test_regressions.py::test_section_order_does_not_change_counts_or_evidence`.
2. **`--no-check-dimensions` reported mismatches.** Running with
   `check_dimensions=False` produced a `dimension_mismatches` entry and
   `dimensions_match=False` for an image that was never checked, instead of an
   explicit *not checked* state.
3. **Known-case identity.** A known case whose image id existed under a
   *different* filename reported `record_present=False` but
   `observed_annotations=1` / `recovered_by_baseline=True`: the lookup keyed on
   `(split, id)` only.
4. **Malformed JSON accepted.** The header loop accepted a trailing comma
   (`{"images":[],"annotations":[],}`), and `json`'s default acceptance of
   `NaN` / `Infinity` / `-Infinity` passed through. At small chunk sizes the
   reader also (a) raised `UnicodeDecodeError` on a multi-byte character split
   across a chunk and (b) accepted a truncated scalar prefix (`-1` from
   `-1.25e-2`). The probe and `tests/test_coco_stream.py` reproduce all of
   these.
5. **Failed runs looked complete.** A run wrote tables incrementally; a failed
   rerun into a reused directory left partial data beside the old
   `report.json`, with no way to tell a complete run from a partial one.

Adding the focused regressions made the gap visible: `tests/test_coco_stream.py`
plus `tests/test_regressions.py` gave **42 failed, 95 passed** on the baseline
(`/tmp/opencode/regressions-before-fix.txt`).

### Fixture correction (disclosure)

Five of those initial 42 failures were *not* implementation bugs: one
parametrised valid fixture embedded a raw newline instead of `\n`, so it was
invalid JSON and rejected (correctly). Those five were fixed in the test
fixture. The genuinely reproduced implementation failures are the rest. The
independent probe's 36 failures over the same grammar are all implementation
defects.

## 4. Implemented changes

### 4.1 Section-order-independent, bounded-memory streaming (`audit.py`)

Each split is now read in two streaming passes: pass 1 aggregates image records
(`images`, `id_to_positions`, `counts` initialised to 0); pass 2 aggregates
annotations, capping per-image annotation-id and sample evidence at
`threshold + 40` and orphan ids at 1,000. No full annotation list is held. The
result is independent of whether `images` or `annotations` appears first.
Measured effect: runtime rose (two passes) while peak memory fell (§6).

### 4.2 Explicit dimension states (`audit.py`)

Added `dimensions_status` per image row and `dimensions_checked` /
`dimension_status_counts` to the report. States: `matching`, `mismatching`,
`missing`, `unreadable`, `not_checked`. Only `mismatching` rows appear in
`dimension_mismatches`; `dimensions_match` remains for compatibility and is
true only for `matching`. Legacy consumers that read counts are unaffected; the
new fields are additive.

### 4.3 Known-case identity (`audit.py`)

The lookup key is now `(split, original image id, filename)`. A different
filename with the same id yields `record_present=False`,
`observed_annotations=None`, `recovered_by_baseline=False`.

### 4.4 Strict streaming JSON grammar (`coco_stream.py`)

- Incremental UTF-8 decoding, so multi-byte characters split across chunks never fail.
- A decoded value is accepted only when a legal JSON delimiter (or end of input) follows it; a numeric token is re-read while it can still grow into the next chunk, so `-1` is never accepted from `-1.25e-2`.
- `parse_constant` rejects `NaN` / `Infinity` / `-Infinity`.
- The header loop rejects a trailing comma before `}`; trailing content, bad separators and malformed structure continue to raise `AuditError` naming file and location.
- Values are still parsed with `json.JSONDecoder` (a maintained parser); only the surrounding grammar was corrected. No new dependency.

### 4.5 Completion contract (`audit.py`, `scripts/build_report.py`)

- Outputs are written into `<out_dir>/.staging/` and promoted into `out_dir` only after everything is written.
- The `COMPLETED` marker is published last, atomically via a sibling `*.tmp` + `os.replace`, so a partial marker is never observable.
- Any failure — including preflight validation (missing input) — first invalidates the old marker, then cleans staging and any attempted marker on the way out.
- `scripts/build_report.py` refuses to publish unless the marker exists, parses, declares `status == "completed"`, and links to the other outputs (source checksums and summary counts). It writes nothing on rejection, so a stale or mixed directory cannot overwrite a good report.
- Pre-marker snapshots (for example the committed `data/derived/`) are **not** given a fabricated marker; regenerate them by rerunning the audit into the directory.

## 5. Reproduction commands

```bash
cd /home/saber/projects/livecell-audit-expansion

# focused regressions (parser + audit defects + completion contract + report builder)
PYTHONPATH=src python3 -m pytest \
    tests/test_coco_stream.py tests/test_regressions.py tests/test_build_report.py -q

# complete suite (includes the real full-dataset integration check)
PYTHONPATH=src python3 -m pytest tests/ -q

# independent parser probe (138 cases; must exit 0 with no failures)
PYTHONPATH=src python3 /tmp/livecell-supervisor-parser-probe.py

# full audit on the read-only source data
PYTHONPATH=src python3 -m livecell_audit audit \
    --data-root /home/saber/projects/livecell-audit/data/raw \
    --out-dir /tmp/opencode/fixed-out

# report build requires the completion marker
PYTHONPATH=src python3 scripts/build_report.py \
    --audit-dir /tmp/opencode/fixed-out \
    --data-root /home/saber/projects/livecell-audit/data/raw \
    --report-dir /tmp/opencode/report-check
```

## 6. Results after the changes

| Check | Result |
| --- | --- |
| Focused parser + regression + builder tests | all pass |
| Complete suite (`tests/`) | **160 passed** in 49.1 s |
| Integration test (isolated) | passes; 9/9 known cases recovered |
| Independent parser probe | **138 checks, 0 failures** |
| Real full audit | 47.02 s runtime, 117.543 MB peak |
| Real report build | exit 0, 157 findings embedded, polygon check ok |
| Marker-less/truncated/mismatched evidence | report builder exits 2, writes nothing |

Full-data invariants after the repairs, versus the preserved baseline:

- Counts identical: 5,387 records, 5,239 filenames, 1,662,447 annotations, 30 train/val overlaps, 9 low-count records, 9/9 known cases.
- `findings.json`, `tables/annotations.jsonl` and `manifests/val_membership.json` are **byte-identical** to the baseline outputs.
- `tables/images.jsonl` is identical apart from the new `dimensions_status` field.
- `report.json` is identical apart from the additive `dimensions_checked`, `dimension_status_counts` and `outputs.completion_marker` fields; all 5,387 images report `matching`.
- Determinism: data exports are byte-identical across reruns; the only non-deterministic output is run metadata (`run.json`, `report.json`'s `run`, and the marker's `completed_at`), explicitly excluded and separately verified with injected differing timestamps.

Runtime/memory (`resource.getrusage` peak RSS, `time.perf_counter`) changed from
~28 s / ~193 MB to ~47 s / ~118 MB: the two-pass read roughly doubles I/O while
lowering peak memory. Counts and evidence are the source of truth; this note
replaces the earlier ~30 s single-pass claim.

## 7. Limitations

- The two-pass reader reads each COCO file twice; that is the price of
  order-independent bounded-memory counting. A single-pass maintained streaming
  parser could remove the extra read but was not adopted (no new dependency).
- Known-case recovery remains a regression check on nine examples, not a
  generalisation benchmark.
- The marker's `completed_at` is wall-clock metadata; determinism is defined
  over the data exports, not run metadata.
- Alternate annotation versions are preserved, not merged; no ML, warehouse, UI
  redesign, image deletion, polygon merging, or biological-label correction was
  performed.
- `data/derived/` remains a pre-marker MVP snapshot and is no longer directly
  buildable; regenerate it by rerunning the audit.

## 8. Files changed

- `src/livecell_audit/coco_stream.py` — strict chunk-safe streaming grammar.
- `src/livecell_audit/audit.py` — two-pass streaming, dimension states,
  known-case identity, staged promotion and completion marker.
- `scripts/build_report.py` — completion-marker validation before any output.
- `tests/test_coco_stream.py`, `tests/test_regressions.py`,
  `tests/test_build_report.py`, `tests/test_audit.py` — focused regressions.
- `README.md` — completion/CLI semantics, dimension-state semantics, measured runtime.
- `docs/07_audit_foundation.md` — this record.
