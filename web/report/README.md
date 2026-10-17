# Connected audit report

A static, data-driven version of the friendly interface. It keeps the
prototype's guided page (`../prototype/index.html`) and fills every number,
example, and finding from the generated audit outputs. No build step, no
framework, no network: open `index.html` directly or serve this directory.

## Files

- `index.html` — the guided page (Start here / See an example / Evidence / How it works).
- `assets/report.css` — styles (prototype palette plus findings-explorer additions).
- `assets/report.js` — rendering logic; reads the two data scripts below.
- `assets/example-shared.png`, `assets/example-sparse.png` — display copies
  (704×520 PNG) of the exact example TIFFs named in the audit report,
  byte-identical to the prototype's display assets.
- `data/report.js`, `data/findings.js` — generated audit data as script
  globals (`window.LIVECELL_REPORT`, `window.LIVECELL_FINDINGS`).

## Regenerating

From the repository root, after running the audit:

```bash
PYTHONPATH=src python3 scripts/build_report.py \
    --audit-dir data/derived \
    --data-root /path/to/data/raw \
    --report-dir web/report
```

`--audit-dir` must contain `report.json`, `findings.json`, and
`manifests/sources.json` as written by `python3 -m livecell_audit audit`.
The script converts the two example TIFFs to PNG, checks that the sparse
example's supplied polygon lies inside the image, and rewrites `data/*.js`.

## Verification

Serve locally and check both examples, the label-overlay checkbox, evidence
disclosures, the findings search/filter, downloads, keyboard operation
(native buttons, links, checkbox, search, select, disclosures in tab order
with visible focus), and layout at desktop and 360 px widths with no
horizontal overflow. See `docs/06_mvp_completion_report.md` for the
measured run.
