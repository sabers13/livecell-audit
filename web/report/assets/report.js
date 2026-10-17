/* Connected-report rendering: fills the page from the generated audit data.
   Works with no build step and no network: data arrives through
   data/report.js and data/findings.js, which assign window globals. */
(() => {
  'use strict';

  const report = window.LIVECELL_REPORT;
  const findings = window.LIVECELL_FINDINGS;

  function fmt(n) {
    return typeof n === 'number' ? n.toLocaleString('en-US') : String(n);
  }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function download(filename, payload) {
    const blob = new Blob([JSON.stringify(payload, null, 2) + '\n'], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // --- example switcher + label overlay (same interaction as the prototype) ---
  const sharedButton = document.getElementById('select-shared');
  const labelsButton = document.getElementById('select-labels');
  const sharedPanel = document.getElementById('example-shared');
  const labelsPanel = document.getElementById('example-labels');
  const status = document.getElementById('selection-status');
  function selectExample(name) {
    const isShared = name === 'shared';
    sharedButton.setAttribute('aria-pressed', String(isShared));
    labelsButton.setAttribute('aria-pressed', String(!isShared));
    sharedPanel.hidden = !isShared;
    labelsPanel.hidden = isShared;
    status.textContent = isShared
      ? 'Showing an image shared between the learning and validation groups.'
      : 'Showing a cell image whose label file marks very few cells.';
  }
  sharedButton.addEventListener('click', () => selectExample('shared'));
  labelsButton.addEventListener('click', () => selectExample('labels'));
  const checkbox = document.getElementById('show-label');
  const overlay = document.getElementById('label-overlay');
  const overlayStatus = document.getElementById('overlay-status');
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) {
      overlay.removeAttribute('hidden');
      overlayStatus.textContent = 'Showing the cell marked in the label file.';
    } else {
      overlay.setAttribute('hidden', '');
      overlayStatus.textContent = 'Showing the original microscope image without the label overlay.';
    }
  });

  if (!report || !findings) {
    document.getElementById('preview-line').textContent =
      'The dataset files could not be read. Check the source location and try again.';
    ['shared-evidence', 'sparse-evidence', 'tech-details'].forEach((id) => {
      document.getElementById(id).innerHTML =
        '<p>The dataset files could not be read. Check the source location and try again.</p>';
    });
    return;
  }

  const shared = report.examples.shared_image;
  const sparse = report.examples.sparse_labels;
  const trainVal = report.cross_split_filename_overlap.train_val;

  // --- shared-image example evidence ---
  if (shared) {
    document.getElementById('shared-note').textContent =
      `These are the same source image (${shared.file_name}). It appears in both groups.`;
    document.getElementById('shared-title').textContent =
      `${fmt(trainVal.filenames)} images appeared in both the learning and checking groups.`;
    document.getElementById('shared-evidence').innerHTML =
      `<p>The downloaded training and validation files share exactly <strong>${fmt(trainVal.filenames)} image filenames</strong>. ` +
      `This example uses <code>${esc(shared.file_name)}</code>: training image record ` +
      `${shared.train_image_ids.map((id) => `<code>${id}</code>`).join(', ')} and validation image record ` +
      `${shared.val_image_ids.map((id) => `<code>${id}</code>`).join(', ')} resolve to the same local source image.</p>` +
      `<p>There are ${fmt(report.unique_filenames)} distinct image filenames in the reviewed snapshot. ` +
      `All have a local image file. This is an independent reproduction of a previously reported concern.</p>` +
      `<p><a href="https://github.com/sartorius-research/LIVECell/issues/26" target="_blank" rel="noopener">Read the original report</a> ` +
      `(issue #26, reported by Alimusto; alternate annotation versions retained per the authors, see issue #14).</p>`;
  }

  // --- sparse-label example: overlay the real supplied polygon ---
  const polyGroup = document.getElementById('label-polygon-group');
  while (polyGroup.firstChild) polyGroup.removeChild(polyGroup.firstChild);
  const SVGNS = 'http://www.w3.org/2000/svg';
  if (sparse && sparse.sample_annotation && Array.isArray(sparse.sample_annotation.segmentation)) {
    sparse.sample_annotation.segmentation.forEach((poly) => {
      if (!Array.isArray(poly) || poly.length < 6) return;
      const points = [];
      for (let i = 0; i + 1 < poly.length; i += 2) points.push(`${poly[i]},${poly[i + 1]}`);
      const el = document.createElementNS(SVGNS, 'polygon');
      el.setAttribute('points', points.join(' '));
      el.setAttribute('fill', '#ffed00');
      el.setAttribute('fill-opacity', '.25');
      el.setAttribute('stroke', '#ffed00');
      el.setAttribute('stroke-width', '2.5');
      polyGroup.appendChild(el);
    });
    const word = sparse.count === 1 ? 'cell' : 'cells';
    document.getElementById('sparse-count').textContent =
      `${fmt(sparse.count)} marked ${word}`;
    document.getElementById('sparse-desc').textContent =
      `In this image, the label file gives the model ${sparse.count === 1 ? 'only one cell example' : `only ${sparse.count} cell examples`}.`;
    document.getElementById('sparse-evidence').innerHTML =
      `<p>File: <code>${esc(sparse.file_name)}</code>. Image record <code>${sparse.image_id}</code> ` +
      `(${esc(sparse.split)} split). Annotation record <code>${sparse.sample_annotation.annotation_id}</code> ` +
      `supplies the polygon shown in the overlay.</p>` +
      `<p>The original reporter identified nine low-count images. The audit recovered ` +
      `<strong>${report.known_sparse_cases_recovered} of ${report.known_sparse_cases_total}</strong> with the simple ` +
      `“at most ${report.low_count_threshold} annotations” rule. They are useful regression examples, not an ` +
      `independent test of ML performance.</p>` +
      `<p><a href="https://github.com/sartorius-research/LIVECell/issues/21" target="_blank" rel="noopener">Read the original report and author response</a> (issue #21, reported by alxndrdiaz).</p>`;
  }

  // --- snapshot stats + counts table ---
  const stats = [
    [report.unique_filenames, 'Cell images in this snapshot'],
    [report.total_image_records, 'Image records, not distinct photos'],
    [report.total_annotations, 'Supplied individual cell markings'],
    [trainVal.filenames, 'Images present in both groups'],
  ];
  document.getElementById('stat-grid').innerHTML = stats
    .map(([value, label]) => `<div class="stat"><strong>${fmt(value)}</strong><span>${esc(label)}</span></div>`)
    .join('');
  const order = ['train', 'val', 'test'];
  const labels = { train: 'Training', val: 'Validation', test: 'Test' };
  document.querySelector('#counts-table tbody').innerHTML = order
    .map((split) => {
      const row = report.splits[split];
      return `<tr><th scope="row">${labels[split]}</th>` +
        `<td class="numeric">${fmt(row.image_records)}</td>` +
        `<td class="numeric">${fmt(row.unique_filenames)}</td>` +
        `<td class="numeric">${fmt(row.extra_filename_records)}</td>` +
        `<td class="numeric">${fmt(row.annotations)}</td></tr>`;
    })
    .join('');

  // --- findings explorer ---
  const searchInput = document.getElementById('finding-search');
  const typeSelect = document.getElementById('finding-type');
  const list = document.getElementById('finding-list');
  const count = document.getElementById('finding-count');
  const TYPE_LABELS = {
    cross_split_overlap: 'Confirmed overlap',
    alternate_annotation_version: 'Alternative annotation versions',
    low_annotation_count: 'Needs label review',
    missing_image_file: 'Missing image file',
    dimension_mismatch: 'Dimension mismatch',
    unparseable_filename: 'Unparseable filename',
  };

  function evidenceLine(finding) {
    const parts = [];
    if (finding.splits) parts.push(`groups: ${finding.splits.join(', ')}`);
    const ids = [];
    Object.values(finding.records || {}).forEach((rec) => {
      (rec.image_ids || []).forEach((id, i) => {
        const n = (rec.counts || [])[i];
        ids.push(n === undefined ? `<code>${id}</code>` : `<code>${id}</code> (${n})`);
      });
    });
    if (ids.length) parts.push(`records: ${ids.join(', ')}`);
    return parts.join(' · ');
  }

  function renderFindings() {
    const query = searchInput.value.trim().toLowerCase();
    const type = typeSelect.value;
    const shown = findings.filter((finding) => {
      if (type && finding.type !== type) return false;
      if (query && !(finding.file_name || '').toLowerCase().includes(query)) return false;
      return true;
    });
    count.textContent = `Showing ${fmt(shown.length)} of ${fmt(findings.length)} findings.`;
    list.innerHTML = shown
      .map((finding) => `<li class="finding-card"><h4>${esc(finding.title)}</h4>` +
        `<div class="finding-meta"><span class="pill">${esc(finding.status)}</span>` +
        `<span class="pill">${esc(TYPE_LABELS[finding.type] || finding.type)}</span>` +
        `<span><code>${esc(finding.file_name || '')}</code></span></div>` +
        (evidenceLine(finding) ? `<p>${evidenceLine(finding)}</p>` : '') +
        `<p><strong>Recommended next step:</strong> ${esc(finding.recommendation)}</p></li>`)
      .join('');
  }
  searchInput.addEventListener('input', renderFindings);
  typeSelect.addEventListener('change', renderFindings);
  renderFindings();

  document.getElementById('download-report').addEventListener('click', () => download('livecell-audit-report.json', report));
  document.getElementById('download-findings').addEventListener('click', () => download('livecell-audit-findings.json', findings));

  // --- scope + technical details ---
  const run = report.run || {};
  document.getElementById('scope-text').textContent =
    `The repeatable audit (livecell-audit ${report.tool.version}) read ${fmt(report.total_image_records)} image records and ` +
    `${fmt(report.total_annotations)} cell markings, and produced this page, ${fmt(findings.length)} findings with evidence, ` +
    `and a derived validation policy that sets aside ${fmt(report.derived_val_membership.excluded_records)} shared validation records. ` +
    `It does not train a model, change model scores, or correct the dataset.`;
  const checksums = Object.entries(report.source_checksums || {})
    .map(([split, info]) => `<li><code>${esc(split)}</code>: <code>${esc(String(info.sha256).slice(0, 16))}…</code> (${fmt(info.bytes)} bytes)</li>`)
    .join('');
  document.getElementById('tech-details').innerHTML =
    `<p>LIVECell is a published collection of microscope images and cell annotations (Edlund et al., Nature Methods 2021). ` +
    `This report was generated from the three downloaded COCO annotation files by streaming them record by record; ` +
    `no annotation file is ever loaded fully into memory.</p>` +
    `<div class="table-wrap"><table><thead><tr><th scope="col">Group</th><th scope="col">Image records</th><th scope="col">Distinct filenames</th><th scope="col">Extra records</th></tr></thead><tbody>` +
    order.map((split) => {
      const row = report.splits[split];
      return `<tr><th scope="row">${labels[split]}</th><td class="numeric">${fmt(row.image_records)}</td>` +
        `<td class="numeric">${fmt(row.unique_filenames)}</td><td class="numeric">${fmt(row.extra_filename_records)}</td></tr>`;
    }).join('') + `</tbody></table></div>` +
    `<p class="spacer-top">The ${fmt(report.extra_within_split_records)} extra within-split records are not automatically mistakes: ` +
    `the dataset authors retained some alternative annotation versions. The tool applies a documented policy for those versions.</p>` +
    `<p>Run time ${fmt(run.runtime_seconds)} seconds with peak memory ${fmt(run.peak_memory_mb)} MB (${esc(run.peak_memory_method || '')}). ` +
    `Derived validation policy: ${esc(report.derived_val_membership.policy)}</p>` +
    (checksums ? `<p>Source checksums (SHA-256):</p><ul>${checksums}</ul>` : '') +
    `<p><a href="https://github.com/sartorius-research/LIVECell" target="_blank" rel="noopener">Original dataset and documentation</a> · ` +
    `<a href="https://github.com/sartorius-research/LIVECell/issues/14" target="_blank" rel="noopener">Why some images have two annotation versions</a></p>`;
})();
