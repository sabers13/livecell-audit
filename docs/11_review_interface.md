# 11. Review website (static review interface)

The review website lives at `web/review/`. It is a static projection of one
completed release run (currently `20261018T181405Z-0e359b9bf088`, see
`docs/12_release.md`): no backend, no build step, no framework, no network call,
and no dataset on the machine that renders it. Its design rules are in
`DESIGN.md`.

Three tabs:

- **Overview** — purpose, the two findings with their upstream origin, a real
  sparse-label image with its supplied label already drawn, and what was built.
- **Try it** — a real image is already selected: an image selector, a
  next-image button and a label toggle, with the finding and next action beside
  the image. Search and filters, detailed counts and the two worked examples are
  optional disclosures.
- **Results** — the method comparison (known-case regression and simulated
  annotation loss, with their limits), the full 3,067-row ranking table,
  evidence downloads and technical details (file hashes, model configuration,
  review status, glossary).

Direct links select the right tab and open parent disclosures. Tabs support
arrow keys, Home/End and browser back/forward.

```bash
python3 -m http.server --directory web/review 8321   # or open index.html via file://
```

## How it is built

The release run builds and checks the site (`tools/run_release.sh`, stages
`site-seed` → `previews` → `site` → `site-check`); `web/review/` is a byte copy of
that run's `site/` directory. The same tools can be run by hand against one
completed run `RUN`:

```bash
python3 tools/build_review_site.py --audit-dir "$RUN/audit" --inputs "$RUN/site-inputs" --out web/review
python3 tools/check_review_site.py --site web/review --audit-dir "$RUN/audit" --inputs "$RUN/site-inputs"
```

`index.html`, `assets/review.css` and `assets/review.js` are hand-written.
`tools/build_review_site.py` writes `data/`, `downloads/`, `previews/` and the
notices; it never measures, fits or infers anything. It copies numbers out of the
verified files and records each input's SHA-256 in `data/site-data.js`, which the
page prints in Technical details. The two guided images
(`assets/guided-*.png`) are committed PNG copies of their source TIFFs.

## Inputs and what each one supplies

| Input (inside `RUN`) | Supplies |
| --- | --- |
| `audit/report.json` | snapshot date, counts, split table, overlap, the two guided examples |
| `audit/findings.json` | all 157 findings: type, status, title, recommendation, records |
| `audit/manifests/sources.json` | the three COCO paths, sizes and SHA-256 |
| `site-inputs/ranking-fixed-mem/candidates.jsonl` | the 3,067-row review pool (ranks, priorities, features, flags) |
| `site-inputs/ranking-fixed-mem/summary.json` | ranking counts, protocol, source/feature/warehouse hashes |
| `site-inputs/ranking-fixed-mem/model_card.json` | model configuration, feature selection (incl. constant features), limitations, partition |
| `site-inputs/evaluation-fixed-mem/evaluation_summary.json` | known-case regression, synthetic loss (images corrupted, labels before/after), seed spread, evidence notes |
| `site-inputs/evaluation-fixed-mem/known_cases.json` | the nine reported cases, per-case ranks, recovery record, upstream reporter |
| `site-inputs/evaluation-fixed-mem/review_sample.json` | the frozen 100-record sample (blank judgments) |
| `site-inputs/previews/*` | server-rendered PNGs and label layers, `manifest.json`, `polygons.json` |

The build refuses a stale or mixed input set: the audit's distinct filenames must
equal the ranking's canonical filenames, the audit COCO hashes must equal the
ranking provenance hashes, the model card and known-case pool must agree with the
ranking summary, and no real-world precision value or review judgment may be
present.

## Outputs

- `data/site-data.js` — `window.LIVECELL_REVIEW`: audit counts (including how many
  of the 30 overlapping filenames carry different label counts per split),
  distribution, the illustrated examples, methods, evaluation, glossary and
  provenance. A script global, so the page also works from `file://`.
- `data/rankings.js` — `window.LIVECELL_RANKINGS`: the full pool projection
  (31 columns × 3,067 rows).
- `data/curated-examples.json` — the illustrated example set, shared with the
  preview renderer so the pictures and the site cannot drift apart.
- `previews/` — 39 PNG previews plus 30 label layers for the dense images.
- `downloads/` — `ranking-pool.csv`, `ranking-summary.json`, `model-card.json`,
  `evaluation-summary.json`, `known-cases.json`, `review-sample.json`,
  `audit-findings.json`, `RANKING-README.txt` (the JSON files are unchanged
  copies of the run's outputs).
- `THIRD_PARTY_NOTICES.txt` — licence, change notice and upstream credits.

## Illustrated example selection (deterministic)

39 of the 3,067 review-pool candidates are illustrated, by this fixed rule
(earlier steps win; a row is never taken twice; every step is a stable sort):

| Step | Limit | Meaning |
| --- | ---: | --- |
| `known_case` | 9 | the upstream-reported sparse-label cases (issue #21) |
| `top_isolation_forest` | 10 | highest Isolation Forest priority (seed 42) |
| `top_robust_baseline` | 5 | highest robust statistical-baseline priority |
| `top_low_count_baseline` | 5 | fewest supplied annotations after the known cases |
| `review_sample_random` | 6 | the frozen review sample's random stratum, in sample order |
| `alternate_annotation_version` | 2 | repeated filenames with alternative annotation versions |
| `confirmed_cross_split_overlap` | 2 | filenames present in both learning and checking groups |
| `cell_line_coverage` | 8 | top Isolation Forest row for any cell line not yet covered |

The rule covers all eight cell lines and all three splits and is stated on the
page. The count is bounded at 40 because each example needs one preview.

## Preview rendering contract

`tools/make_previews.py` runs on the data server and is bounded by design:

- at most 40 PNGs (`--max-previews`, hard maximum 40), longest side at most
  640 px (`--longest-side`, hard maximum 640);
- at most 20 supplied polygons per image in `polygons.json` (`--outline-cap`,
  hard maximum 20), taken in COCO source order; these are drawn as an SVG overlay
  for sparse images, with a locator circle for a single label;
- an image with more polygons than the cap also gets a transparent label layer
  (`<preview_id>.labels.png`) with every supplied outline, so a fully labelled
  image never looks like one with missing labels;
- one pass per split with the project's streaming COCO reader; one TIFF open at a
  time; refuses to write inside the raw data root, refuses an existing manifest
  without `--force`, and writes the manifest last so a partial run is detectable.

`manifest.json` records, per preview: `file`, `source_file`, `source_bytes`,
`source_width`, `source_height`, `tiff_sha256`, `png_sha256`, `png_bytes`,
`polygons_total`, `polygons_drawn`, `labels_layer` (file, SHA-256, bytes and
outlines drawn, or null below the cap) and the render parameters.

## Verification

`tools/check_review_site.py` re-derives every published number from the inputs,
maps every example to its candidate row, compares all 3,067 ranking rows with
`candidates.jsonl`, verifies every declared SHA-256 and every relative link, and
scans for private paths and forbidden claims. For previews it checks each PNG and
label layer against the render manifest, that a label layer exists exactly when
the outlines are capped and draws every supplied outline, and that the render was
made from this site's byte-identical curated set. JavaScript syntax is checked
with `node --check` where node is installed; the server has none, so that one
check is recorded as skipped there and was run on the development machine. The
reference run's counts are in `docs/12_release.md` §5.

Checked in Chromium at 1440 px and 390 px on 18 October 2026: no console errors,
no horizontal scroll on any tab, the label toggle and its status text, the
single-label locator, full label layers on dense images, and the Results text.

## Limitations

- The previews are 39 of the 3,067 review-pool candidates, downsampled to
  ≤ 640 px. Every other candidate shows its numbers without an image, and the page
  says so.
- No comprehension test with a person unfamiliar with the field has been
  performed; the design's comprehension questions are goals, not results.
- No scientific reviewer is supplied, so real-world precision is unmeasured and
  the review sample ships with blank judgments.
