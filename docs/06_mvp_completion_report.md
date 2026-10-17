# 06. MVP completion report

Branch `fm/livecell-mvp`, local-only, no remotes/PRs/merges. Source folder
`/home/saber/projects/livecell-audit` was read only; the four authorised
paths were imported with matching SHA-256 hashes and committed as the
baseline before any edits.

## Commands and results

```bash
# audit (streams the three COCO files, ~909 MB total)
PYTHONPATH=src python3 -m livecell_audit audit \
    --data-root /home/saber/projects/livecell-audit/data/raw \
    --out-dir <out>
# -> image_records=5387 unique_filenames=5239 annotations=1662447
#    missing_files=0 train_val_overlap=30 low_count_records=9
#    known_cases_recovered=9/9
#    runtime ~31-39 s, peak memory ~197-224 MB
#    (time.perf_counter; resource.getrusage ru_maxrss peak RSS)

# fixture suite + full-dataset integration check
PYTHONPATH=src python3 -m pytest tests/ -q            # 21 passed
LIVECELL_DATA_ROOT=.../data/raw PYTHONPATH=src \
  python3 -m pytest tests/test_integration.py -q       # 1 passed

# connected report
PYTHONPATH=src python3 scripts/build_report.py \
    --audit-dir data/derived --data-root .../data/raw --report-dir web/report
```

Every expected snapshot number reproduced exactly (per-split
3253/3188/65/1018576, 570/569/1/181610, 1564/1512/52/462261; 5239 local
TIFFs, none missing; 30/0/0 overlap; 9/9 known sparse cases; derived
validation 540 included / 30 excluded). Regenerated display PNGs are
byte-identical to the prototype assets; the sparse overlay polygon equals
the prototype's recorded segmentation.

## Verification

- Fixture tests cover cross-split overlap, alternate annotation versions,
  missing files, malformed structure (missing keys/fields, truncation, wrong
  types), filename parsing (all 8 cell lines, wells, timestamps, invalid
  names), dimension mismatch, and byte-identical reruns.
- Browser checks (chrome-devtools-axi, Chromium): both examples, overlay
  toggle, evidence disclosures, findings search/filter (157 findings; filter
  alone shows 9 low-count), Blob downloads, clean console, logical tab order
  with visible focus, no horizontal overflow at desktop and 360 px.
- Not performed: a real recruiter comprehension check; pixel-identity
  matching; any model training or score comparison (explicitly out of scope).

## What changed relative to the imported baseline

New: `src/livecell_audit/` (streaming audit), `tests/`,
`scripts/build_report.py`, `data/derived/` (report, findings, run,
manifests), `web/report/` (connected static page), this report. Edited:
`README.md` (honest MVP scope, reproduction, outputs, limitations, credit),
`pyproject.toml` (stdlib + Pillow only), one favicon line in the report
page. Untouched reference: `web/prototype/`, `DESIGN.md`, docs 01–05.

Large tables (`tables/*.jsonl`, ~264 MB) live only in the worker scratch
outputs, never in the branch.
