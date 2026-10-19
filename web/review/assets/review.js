/* LIVECell review website.
   Every value shown here is read from data/site-data.js (window.LIVECELL_REVIEW)
   or data/rankings.js (window.LIVECELL_RANKINGS); both are generated from the
   verified outputs by tools/build_review_site.py. Nothing is invented here. */

(function () {
  'use strict';

  var SITE = window.LIVECELL_REVIEW || null;
  var RANK = window.LIVECELL_RANKINGS || null;
  var $ = function (id) { return document.getElementById(id); };
  var NUM = new Intl.NumberFormat('en-US');
  var errorBox = $('preview-status');

  /* Keep navigation available even when an evidence data file is missing. */
  var tabLinks = Array.prototype.slice.call(document.querySelectorAll('[data-tab]'));
  var tabPanels = Array.prototype.slice.call(document.querySelectorAll('[data-tab-panel]'));
  $('project-tabs').setAttribute('role', 'tablist');
  tabLinks.forEach(function (link) {
    link.setAttribute('role', 'tab');
    link.setAttribute('aria-controls', link.dataset.tab);
  });
  tabPanels.forEach(function (panel) {
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', 'tab-' + panel.id);
    panel.tabIndex = 0;
    panel.addEventListener('animationend', function (event) {
      if (event.target === panel) { panel.classList.remove('is-entering'); }
    });
  });

  /* One selection bar slides between tabs; without JavaScript each selected tab
     draws its own underline. */
  var tabNav = $('project-tabs');
  var tabIndicator = document.createElement('span');
  tabIndicator.className = 'tab-indicator';
  tabIndicator.setAttribute('aria-hidden', 'true');
  tabNav.appendChild(tabIndicator);
  tabNav.classList.add('has-indicator');
  function placeTabIndicator() {
    var selected = tabLinks.filter(function (link) { return link.getAttribute('aria-selected') === 'true'; })[0];
    if (!selected) { return; }
    tabNav.style.setProperty('--indicator-x', selected.offsetLeft + 'px');
    tabNav.style.setProperty('--indicator-w', selected.offsetWidth + 'px');
  }
  window.addEventListener('resize', placeTabIndicator);
  if (document.fonts && document.fonts.ready) { document.fonts.ready.then(placeTabIndicator); }
  window.requestAnimationFrame(function () {
    window.requestAnimationFrame(function () { tabNav.classList.add('is-ready'); });
  });

  var tabShownHooks = [];
  var shownPanelId = null;
  function syncTabs(scrollToTarget) {
    var targetId;
    try { targetId = decodeURIComponent(window.location.hash.slice(1)); }
    catch (_error) { targetId = ''; }
    if (targetId === 'build' || targetId === 'how') {
      targetId = 'results';
      window.history.replaceState(null, '', '#results');
    }
    var target = $(targetId);
    var activePanel = target && target.closest('[data-tab-panel]');
    var activeId = activePanel ? activePanel.id : (targetId === 'evidence-details' ? 'try-it' : 'overview');
    tabPanels.forEach(function (panel) { panel.hidden = panel.id !== activeId; });
    tabLinks.forEach(function (link) {
      var selected = link.dataset.tab === activeId;
      link.setAttribute('aria-selected', String(selected));
      link.tabIndex = selected ? 0 : -1;
    });
    placeTabIndicator();
    /* a section link (or the page load) starts at the top, not at the panel edge */
    if (scrollToTarget && (!target || target === activePanel)) { window.scrollTo({ top: 0, behavior: 'instant' }); }
    if (shownPanelId !== null && shownPanelId !== activeId) {
      $(activeId).classList.add('is-entering');
    }
    shownPanelId = activeId;
    tabShownHooks.forEach(function (hook) { hook(activeId); });
    if (target && activePanel && target !== activePanel) {
      for (var parent = target.parentElement; parent && parent !== activePanel; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') { parent.open = true; }
      }
      if (scrollToTarget) { target.scrollIntoView({ block: 'start' }); }
    }
  }
  function selectTab(link) {
    if (window.location.hash !== link.hash) { window.history.pushState(null, '', link.hash); }
    syncTabs(false);
    window.scrollTo({ top: 0, behavior: 'instant' });
  }
  tabLinks.forEach(function (link, index) {
    link.addEventListener('click', function (event) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) { return; }
      event.preventDefault();
      selectTab(link);
    });
    link.addEventListener('keydown', function (event) {
      var next;
      if (event.key === 'ArrowRight') { next = (index + 1) % tabLinks.length; }
      else if (event.key === 'ArrowLeft') { next = (index + tabLinks.length - 1) % tabLinks.length; }
      else if (event.key === 'Home') { next = 0; }
      else if (event.key === 'End') { next = tabLinks.length - 1; }
      else if (event.key === ' ') { event.preventDefault(); selectTab(link); return; }
      else { return; }
      event.preventDefault();
      selectTab(tabLinks[next]);
      tabLinks[next].focus();
    });
  });
  /* The rail mirrors the selected section; its links switch tabs the same way. */
  var railLinks = Array.prototype.slice.call(document.querySelectorAll('[data-rail]'));
  railLinks.forEach(function (link) {
    link.addEventListener('click', function (event) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) { return; }
      event.preventDefault();
      selectTab(link);
    });
  });
  tabShownHooks.push(function (panelId) {
    railLinks.forEach(function (link) {
      if (link.dataset.rail === panelId) { link.setAttribute('aria-current', 'page'); }
      else { link.removeAttribute('aria-current'); }
    });
  });

  /* Cards enter one after another each time a section is shown (CSS restarts
     the animation when a hidden panel is displayed). */
  tabPanels.forEach(function (panel) {
    Array.prototype.forEach.call(panel.querySelectorAll('.reveal'), function (node, position) {
      node.style.setProperty('--i', String(position));
    });
  });

  /* Big figures count up and bars fill the first time their section is shown;
     the final value is always the one in the markup or the data file. */
  var motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  function formatCount(node, value) {
    var digits = Number(node.dataset.decimals || 0);
    return (node.dataset.prefix || '') + value.toLocaleString('en-US', {
      minimumFractionDigits: digits, maximumFractionDigits: digits
    }) + (node.dataset.suffix || '');
  }
  function countUp(node) {
    var target = Number(node.dataset.count);
    if (!isFinite(target)) { return; }
    if (motionQuery.matches) { node.textContent = formatCount(node, target); return; }
    var start = window.performance.now();
    var duration = 900;
    (function frame(now) {
      var t = Math.min(1, (now - start) / duration);
      var eased = 1 - Math.pow(1 - t, 3);
      node.textContent = formatCount(node, t < 1 ? target * eased : target);
      if (t < 1) { window.requestAnimationFrame(frame); }
    })(start);
  }
  var animatedPanels = {};
  tabShownHooks.push(function (panelId) {
    if (animatedPanels[panelId]) { return; }
    animatedPanels[panelId] = true;
    var panel = $(panelId);
    Array.prototype.forEach.call(panel.querySelectorAll('[data-count]'), countUp);
    window.requestAnimationFrame(function () {
      Array.prototype.forEach.call(panel.querySelectorAll('[data-pct]'), function (bar) {
        bar.style.width = Math.max(0, Math.min(100, Number(bar.dataset.pct) || 0)) + '%';
      });
    });
  });

  /* Each page opening starts light; a reader can switch for this visit.
     The switch wipes in as a circle from the pressed button. */
  var root = document.documentElement;
  function currentTheme() { return root.dataset.theme || 'light'; }
  var themeButtons = Array.prototype.slice.call(document.querySelectorAll('[data-theme-set]'));
  function syncThemeButtons() {
    themeButtons.forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.themeSet === currentTheme()));
    });
  }
  syncThemeButtons();
  themeButtons.forEach(function (button) {
    button.addEventListener('click', function (event) {
      var next = button.dataset.themeSet;
      if (next === currentTheme()) { return; }
      var apply = function () {
        root.dataset.theme = next;
        syncThemeButtons();
      };
      if (!document.startViewTransition || motionQuery.matches) { apply(); return; }
      var rect = button.getBoundingClientRect();
      var x = event.clientX || rect.left + rect.width / 2;
      var y = event.clientY || rect.top + rect.height / 2;
      var radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
      document.startViewTransition(apply).ready.then(function () {
        root.animate(
          { clipPath: ['circle(0px at ' + x + 'px ' + y + 'px)', 'circle(' + radius + 'px at ' + x + 'px ' + y + 'px)'] },
          { duration: 600, easing: 'cubic-bezier(.65,0,.35,1)', pseudoElement: '::view-transition-new(root)' });
      });
    });
  });

  syncTabs(false);
  window.addEventListener('hashchange', function () { syncTabs(true); });
  /* The browser's own jump to a section's fragment would hide the top bar and
     outline the whole panel; a section opens at the top of the page instead. */
  window.addEventListener('load', function () {
    var targetId;
    try { targetId = decodeURIComponent(window.location.hash.slice(1)); }
    catch (_error) { targetId = ''; }
    var target = targetId ? $(targetId) : null;
    if (target && !target.hasAttribute('data-tab-panel')) { return; }
    if (target && document.activeElement === target) { target.blur(); }
    window.scrollTo({ top: 0, behavior: 'instant' });
  });

  if (!SITE || !RANK) {
    if (errorBox) {
      errorBox.textContent =
        'The local data file could not be read, so the interactive tables are unavailable. ' +
        'The explanation and evidence downloads remain available.';
    }
    Array.prototype.forEach.call(document.querySelectorAll('button, input, select'), function (control) {
      control.disabled = true;
    });
    Array.prototype.forEach.call(document.querySelectorAll('p'), function (paragraph) {
      if (/^Loading/.test(paragraph.textContent.trim())) {
        paragraph.textContent = 'This section is unavailable because a local data file is missing. ' +
          'Reload the complete website or use the evidence downloads below.';
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll('table'), function (table) {
      clear(table);
      table.appendChild(el('caption', null, 'Table unavailable: a local data file is missing.'));
    });
    setText('browse-page', 'Data unavailable');
    setText('rankings-page', 'Data unavailable');
    setText('detail-title', 'Image details unavailable');
    tabShownHooks.forEach(function (hook) { hook(shownPanelId); });
    var unavailableDetail = $('detail').querySelector('p');
    if (unavailableDetail) {
      unavailableDetail.textContent = 'Reload the complete website to explore the images. ' +
        'The explanation and evidence downloads remain available.';
    }
    return;
  }

  function n(value) {
    return typeof value === 'number' && isFinite(value) ? NUM.format(value) : '—';
  }
  function dec(value, digits) {
    return typeof value === 'number' && isFinite(value)
      ? value.toFixed(typeof digits === 'number' ? digits : 4)
      : '—';
  }
  function pct(value, digits) {
    return typeof value === 'number' && isFinite(value)
      ? (value * 100).toFixed(typeof digits === 'number' ? digits : 1) + '%'
      : '—';
  }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (typeof text === 'string') { node.textContent = text; }
    return node;
  }
  function setText(id, text) { var node = $(id); if (node) { node.textContent = text; } }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }

  var METHOD_ORDER = ['low_count', 'isolation_forest', 'robust', 'random'];
  var METHOD_LABELS = SITE.methods.method_labels;
  var BUDGETS = SITE.methods.budgets;
  var DEFAULT_SEED = SITE.methods.default_seed;
  var SEEDS = SITE.methods.sensitivity_seeds;
  var EXAMPLE_SEED = 42;

  function rankOf(example, method, seed) {
    var entry = example.ranks[method];
    if (!entry) { return null; }
    var key = 'seed' + seed;
    if (typeof entry[key] === 'number') { return entry[key]; }
    if (typeof entry.seed42 === 'number') { return entry.seed42; }
    return null;
  }
  function priorityOf(example, method) {
    var entry = example.ranks[method] || {};
    return typeof entry.priority_seed42 === 'number' ? entry.priority_seed42 : null;
  }

  /* ---------------------------------------------------------------- meta */
  if (errorBox) {
    var delivered = SITE.meta.preview_status === 'delivered';
    errorBox.textContent = 'Interactive review · verified dataset snapshot · 17 October 2026 · ' +
      (delivered
        ? 'illustrated previews included (' + SITE.examples.length + ' images)'
        : 'illustrated previews pending a bounded server render');
  }
  setText('footer-snapshot', 'Snapshot: 17 October 2026');

  /* -------------------------------------------------------------- answers */
  var audit = SITE.audit;
  var c = audit.counts;
  setText('intro-coverage', n(c.distinct_filenames));
  setText('intro-overlap', n(audit.overlap.train_val));
  setText('intro-sparse', n(audit.known_sparse_cases.total));
  setText('answer-1', 'Images and cell labels used to teach and to evaluate an AI model. The dataset in ' +
    'this snapshot holds ' + n(c.distinct_filenames) + ' distinct cell images, ' + n(c.image_records) +
    ' image records and ' + n(c.annotations) + ' supplied cell labels.');
  setText('answer-2', 'The same ' + n(audit.overlap.train_val) + ' image filenames appear in both the ' +
    'learning and the checking groups, and ' + n(audit.known_sparse_cases.total) + ' previously reported ' +
    'images have very few labelled cells. A further ' + n(c.extra_within_split_records) + ' records repeat a ' +
    'filename inside one group, which needs a documented policy rather than a silent fix.');
  setText('answer-3', 'A model can look more capable when the images used to check it overlap the images it ' +
    'learned from. Incomplete labels give a model unreliable examples of what a cell looks like.');
  setText('answer-4', 'It records each finding with its evidence, its uncertainty and a recommended review ' +
    'action, and writes a repeatable local report. It does not claim that the dataset has been ' +
    'scientifically corrected, and no image is deleted or relabelled.');
  setText('answer-5', 'Independent verification of the snapshot counts, a bounded feature build and review ' +
    'ranking with an honest evaluation, a repeatable audit pipeline, automated checks, local release ' +
    'tooling and this interface. Reviewed policy exports and a scientific review of the flagged images ' +
    'remain to be done.');

  /* -------------------------------------------------- guided examples A/B */
  function evidenceLine(parts) { return parts.filter(Boolean).join(' '); }

  var overlapFinding = SITE.guided.overlap_finding;
  var overlapRecords = SITE.guided.overlap_records;
  var sharedEvidence = ['The audit matched the three COCO annotation files by filename: ' +
    n(audit.overlap.train_val) + ' filenames occur in both the training and the validation split, and ' +
    n(audit.overlap.train_test + audit.overlap.val_test) + ' filenames overlap the test split.'];
  if (overlapFinding && overlapFinding.records.length) {
    sharedEvidence.push('This example is ' + SITE.guided.overlap_file + ': the training record (image id ' +
      overlapRecords.train_image_ids.join(', ') + ') marks ' + overlapFinding.records[0].counts.join(', ') +
      ' cells and the validation record (image id ' + overlapRecords.val_image_ids.join(', ') + ') marks ' +
      overlapFinding.records[overlapFinding.records.length - 1].counts.join(', ') + ' cells.');
  }
  if (typeof audit.overlap.differing_label_counts === 'number') {
    sharedEvidence.push((audit.overlap.differing_label_counts === audit.overlap.train_val
      ? 'In all ' + n(audit.overlap.train_val)
      : 'In ' + n(audit.overlap.differing_label_counts) + ' of the ' + n(audit.overlap.train_val)) +
      ' overlapping filenames the two groups mark different numbers of cells, so each group holds its ' +
      'own label version of the same photo.');
  }
  sharedEvidence.push('All ' + n(SITE.methods.pools.review.size) + ' canonical records are matched to a ' +
    'local image file (' + n(c.missing_image_filenames) + ' missing). This is an independent reproduction ' +
    'of a concern first reported upstream in issue #26, not a new discovery.');
  setText('guided-shared-evidence', sharedEvidence.join(' '));
  setText('guided-shared-boundary', 'The overlap is a confirmed filename match: images are matched by ' +
    'filename, not by comparing pixels, so near-duplicate photos would not be found.');

  var sparseFinding = SITE.guided.sparse_finding;
  var sparseSeg = SITE.guided.sparse_annotation.segmentation[0];
  var sparsePoints = [];
  for (var i = 0; i + 1 < sparseSeg.length; i += 2) { sparsePoints.push([sparseSeg[i], sparseSeg[i + 1]]); }
  var sparseCount = SITE.guided.sparse_count || sparseFinding.records[0].counts[0];
  setText('sparse-count-callout', n(sparseCount) + (sparseCount === 1 ? ' labelled cell' : ' labelled cells'));
  setText('sparse-meaning', 'In this image, the label file gives the model only ' + n(sparseCount) +
    (sparseCount === 1 ? ' cell example.' : ' cell examples.'));
  setText('sparse-action', sparseFinding.recommendation);
  setText('guided-sparse-evidence', evidenceLine([
    'File ' + SITE.guided.sparse_file + ', image id ' + SITE.guided.sparse_image_id + ', split ' +
      sparseFinding.records[0].split + '.',
    'The annotation record id is ' + SITE.guided.sparse_annotation.annotation_id + ' and its polygon area is ' +
      dec(SITE.guided.sparse_annotation.area, 2) + ' pixels^2; that single polygon is what the overlay draws.',
    'The upstream reporter (' + SITE.methods.knownCase.upstream_reporter + ', issue #21) listed ' +
      n(audit.known_sparse_cases.total) + ' images using a threshold of ten segments, and a dataset ' +
      'maintainer replied that they had slipped through quality control. All ' +
      n(audit.known_sparse_cases.recovered) + ' are recovered in this snapshot by the same rule ' +
      '\u201cat most ' + n(c.low_count_threshold) + ' annotations\u201d, so they are regression examples, ' +
      'not an independent test of a detector.'
  ]));
  setText('guided-sparse-boundary', 'A low label count alone is not proof of an error: the culture may be ' +
    'partly confluent, or the annotator may have labelled a representative subset.');

  var overlaySvg = $('sparse-overlay');
  function polygonNode(points) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    node.setAttribute('points', points.map(function (p) { return p[0] + ',' + p[1]; }).join(' '));
    return node;
  }
  function drawOverlay(svg, polygons, width, height, options) {
    clear(svg);
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    var title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = polygons.length + ' supplied annotation polygon(s) drawn as supplied; ' +
      'these are labels, not cell boundaries detected by this project.';
    svg.appendChild(title);
    polygons.forEach(function (points) {
      var halo = polygonNode(points);
      halo.setAttribute('class', 'mark');
      halo.setAttribute('fill', 'none');
      halo.setAttribute('stroke', '#0f172a');
      halo.setAttribute('stroke-opacity', '.5');
      halo.setAttribute('stroke-width', String((width / 704) * 4.2));
      svg.appendChild(halo);
      var mark = polygonNode(points);
      mark.setAttribute('class', 'mark');
      mark.setAttribute('fill', '#ffed00');
      mark.setAttribute('fill-opacity', '.22');
      mark.setAttribute('stroke', '#ffed00');
      mark.setAttribute('stroke-width', String((width / 704) * 2.4));
      svg.appendChild(mark);
    });
    if (options && options.locator && polygons.length) {
      var xs = polygons[0].map(function (p) { return p[0]; });
      var ys = polygons[0].map(function (p) { return p[1]; });
      var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
      var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
      var cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
      var r = Math.max(Math.max(maxX - minX, maxY - minY, 8) * 2.1, 10);
      r = Math.min(r, Math.max(cx - 3, 8), Math.max(cy - 3, 8),
        Math.max(width - cx - 3, 8), Math.max(height - cy - 3, 8));
      var ring = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      ring.setAttribute('class', 'locator-ring');
      ring.setAttribute('cx', cx); ring.setAttribute('cy', cy); ring.setAttribute('r', r);
      ring.setAttribute('fill', 'none'); ring.setAttribute('stroke', '#0f172a');
      ring.setAttribute('stroke-opacity', '.55'); ring.setAttribute('stroke-width', '3');
      ring.setAttribute('stroke-dasharray', '7 5');
      svg.appendChild(ring);
      var boxWidth = 236, boxHeight = 44;
      var labelX = Math.min(cx + r + 46, width - boxWidth - 6);
      var labelY = Math.min(Math.max(cy - boxHeight / 2, 6), height - boxHeight - 6);
      var line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      line.setAttribute('class', 'locator-line');
      line.setAttribute('d', 'M' + (cx + r) + ' ' + cy + 'H' + labelX);
      line.setAttribute('stroke', '#0f172a'); line.setAttribute('stroke-width', '2');
      svg.appendChild(line);
      var callout = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      callout.setAttribute('class', 'locator-callout');
      var box = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      box.setAttribute('x', labelX); box.setAttribute('y', labelY);
      box.setAttribute('width', boxWidth); box.setAttribute('height', boxHeight);
      box.setAttribute('rx', 6); box.setAttribute('fill', '#0f172a');
      callout.appendChild(box);
      var label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('x', labelX + 14); label.setAttribute('y', labelY + 30);
      label.setAttribute('fill', '#ffed00'); label.setAttribute('font-size', '24');
      label.textContent = 'Only labelled cell';
      callout.appendChild(label);
      svg.appendChild(callout);
    }
  }
  drawOverlay(overlaySvg, [sparsePoints], 704, 520, { locator: true });
  if ($('intro-overlay')) {
    drawOverlay($('intro-overlay'), [sparsePoints], 704, 520, { locator: true });
    setText('intro-readout-file', SITE.guided.sparse_file);
    setText('intro-readout-count', n(sparseCount) + (sparseCount === 1 ? ' supplied label' : ' supplied labels'));
  }
  if ($('overview-sparse-overlay')) {
    drawOverlay($('overview-sparse-overlay'), [sparsePoints], 704, 520, { locator: true });
  }

  /* The one orchestrated moment: the first time the overview is on screen, the
     specimen comes into focus and the locator closes onto the only labelled cell.
     Skipped entirely when the reader asks for reduced motion. */
  var introSpecimen = $('intro-specimen');
  var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var introRevealed = false;
  function revealIntro() {
    if (introRevealed || !introSpecimen) { return; }
    var overview = $('overview');
    if (!overview || overview.hidden) { return; }
    introRevealed = true;
    if (reducedMotion.matches) { introSpecimen.classList.remove('will-reveal'); return; }
    var image = introSpecimen.querySelector('img');
    var ready = image && image.decode ? image.decode().catch(function () {}) : Promise.resolve();
    ready.then(function () {
      window.requestAnimationFrame(function () {
        introSpecimen.classList.remove('will-reveal');
        introSpecimen.classList.add('is-revealed');
      });
    });
  }
  if (introSpecimen && !reducedMotion.matches) { introSpecimen.classList.add('will-reveal'); }
  tabShownHooks.push(function (panelId) { if (panelId === 'overview') { revealIntro(); } });

  /* The overlap example: each group's own label version on the same photo. */
  var overlapLayers = (SITE.guided.overlap_layers || []);
  overlapLayers.forEach(function (layer) {
    var target = $(layer.split === 'train' ? 'guided-train-labels' : 'guided-val-labels');
    var readout = $(layer.split === 'train' ? 'guided-train-readout' : 'guided-val-readout');
    if (!target) { return; }
    target.src = layer.src;
    target.alt = n(layer.labels) + ' supplied cell labels from the ' +
      (layer.split === 'train' ? 'training' : 'validation') + ' record, drawn over the image.';
    target.hidden = false;
    if (readout) {
      readout.textContent = '';
      var swatch = el('span', 'legend-swatch');
      swatch.setAttribute('aria-hidden', 'true');
      readout.appendChild(swatch);
      readout.appendChild(document.createTextNode(n(layer.labels) + ' labels · image id ' + layer.image_id));
    }
  });

  var sharedButton = $('guided-shared-button');
  var sparseButton = $('guided-sparse-button');
  var sharedPanel = $('guided-shared-panel');
  var sparsePanel = $('guided-sparse-panel');
  var guidedStatus = $('guided-status');
  function selectGuided(which) {
    var isShared = which === 'shared';
    var alreadySelected = (isShared ? sharedButton : sparseButton).getAttribute('aria-pressed') === 'true';
    sharedButton.setAttribute('aria-pressed', String(isShared));
    sparseButton.setAttribute('aria-pressed', String(!isShared));
    $('guided-shared-selected').hidden = !isShared;
    $('guided-sparse-selected').hidden = isShared;
    sharedPanel.hidden = !isShared;
    sparsePanel.hidden = isShared;
    guidedStatus.textContent = (alreadySelected ? 'This example is already selected. ' : '') +
      (isShared
        ? 'Viewing example 1 of 2 below: the same image appears in both the learning and the checking groups.'
        : 'Viewing example 2 of 2 below: only ' + n(sparseCount) +
          (sparseCount === 1 ? ' cell is labelled.' : ' cells are labelled.')) +
      ' Choose the other example to switch findings.';
    (isShared ? sharedPanel : sparsePanel).scrollIntoView({
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
      block: 'start'
    });
  }
  sharedButton.addEventListener('click', function () { selectGuided('shared'); });
  sparseButton.addEventListener('click', function () { selectGuided('sparse'); });

  var overviewCase = 'shared';
  var overviewCaseButtons = Array.prototype.slice.call(document.querySelectorAll('[data-overview-case]'));
  overviewCaseButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      overviewCase = button.dataset.overviewCase;
      overviewCaseButtons.forEach(function (item) {
        item.setAttribute('aria-pressed', String(item.dataset.overviewCase === overviewCase));
      });
      $('overview-shared-case').hidden = overviewCase !== 'shared';
      $('overview-sparse-case').hidden = overviewCase !== 'sparse';
      $('overview-case-status').textContent = overviewCase === 'shared'
        ? 'Showing the same photo with its training and validation labels.'
        : 'Showing an image with one supplied cell label.';
    });
  });
  $('overview-case-detail').addEventListener('click', function (event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) { return; }
    event.preventDefault();
    selectTab(tabLinks.filter(function (link) { return link.dataset.tab === 'try-it'; })[0]);
    selectGuided(overviewCase);
  });

  var sparseCheckbox = $('show-sparse-label');
  var sparseStatus = $('sparse-overlay-status');
  sparseCheckbox.addEventListener('change', function () {
    overlaySvg.toggleAttribute('hidden', !sparseCheckbox.checked);
    sparseStatus.textContent = sparseCheckbox.checked
      ? 'Showing the ' + (sparseCount === 1 ? 'one labelled cell.' : n(sparseCount) + ' labelled cells.')
      : 'Showing the original microscope image without the label overlay.';
  });

  /* ------------------------------------------------------------- browse */
  var EXAMPLES = SITE.examples;
  var PER_PAGE = 8;
  var browserState = { query: '', finding: '', cellLine: '', split: '', method: 'low_count', budget: 25, page: 1, selected: null };

  var browseIntro = $('browse-intro');
  browseIntro.textContent = SITE.exampleSelection.statement + ' Order the list by any method below; ' +
    'the selected image opens with its filters, counts, evidence and next action. ' +
    (SITE.meta.preview_status === 'delivered'
      ? 'Each illustrated image carries its supplied annotation outlines.'
      : 'Image previews for this bundle are still pending a bounded server render; the records, ' +
        'ranks and counts below are complete and the images are labelled as not yet included.');

  var findingSelect = $('browse-finding');
  var seenFindings = {};
  EXAMPLES.forEach(function (example) {
    if (!seenFindings[example.finding_label]) {
      seenFindings[example.finding_label] = 1;
      var option = el('option', null, example.finding_label);
      option.value = example.finding_type;
      findingSelect.appendChild(option);
    }
  });
  var cellLineSelect = $('browse-cell-line');
  var cellLines = Array.from(new Set(EXAMPLES.map(function (e) { return e.cell_line; }))).sort();
  cellLines.forEach(function (name) {
    var option = el('option', null, name);
    option.value = name;
    cellLineSelect.appendChild(option);
  });
  var methodSelect = $('browse-method');
  METHOD_ORDER.forEach(function (key) {
    var option = el('option', null, METHOD_LABELS[key] + (key === 'low_count' ? ' (default)' : ''));
    option.value = key;
    methodSelect.appendChild(option);
  });
  methodSelect.value = 'low_count';
  var budgetSelect = $('browse-budget');
  BUDGETS.forEach(function (budget) {
    var option = el('option', null, 'Top ' + budget);
    option.value = String(budget);
    budgetSelect.appendChild(option);
  });
  budgetSelect.value = '25';
  var overlayCheckbox = $('browse-overlay');
  var listNode = $('browse-list');
  var countNode = $('browse-count');
  var pageLabel = $('browse-page');
  var prevButton = $('browse-prev');
  var nextButton = $('browse-next');
  var detail = $('detail');
  var trySelect = $('try-example');
  var tryNext = $('try-next');

  function exampleLabel(example) {
    return example.cell_line + ' cells · ' + n(example.n_annotations) +
      (example.n_annotations === 1 ? ' supplied label' : ' supplied labels');
  }

  function filtered() {
    var query = browserState.query.trim().toLowerCase();
    return EXAMPLES.filter(function (example) {
      if (query && example.file_name.toLowerCase().indexOf(query) === -1) { return false; }
      if (browserState.finding && example.finding_type !== browserState.finding) { return false; }
      if (browserState.cellLine && example.cell_line !== browserState.cellLine) { return false; }
      if (browserState.split && example.split !== browserState.split) { return false; }
      return true;
    }).sort(function (a, b) {
      var ra = rankOf(a, browserState.method, EXAMPLE_SEED);
      var rb = rankOf(b, browserState.method, EXAMPLE_SEED);
      if (ra === null && rb === null) { return a.file_name < b.file_name ? -1 : 1; }
      if (ra === null) { return 1; }
      if (rb === null) { return -1; }
      if (ra !== rb) { return ra - rb; }
      return a.file_name < b.file_name ? -1 : 1;
    });
  }

  /* "Try it" steps through the flagged images first, then the comparison images
     the review methods picked; each group keeps the chosen method's order. */
  function isFlagged(example) { return example.finding_type !== 'review_priority'; }
  function tryOrder(rows) {
    return rows.filter(isFlagged).concat(rows.filter(function (row) { return !isFlagged(row); }));
  }

  function renderCards(rows) {
    clear(listNode);
    clear(trySelect);
    var ordered = tryOrder(rows);
    var flaggedCount = rows.filter(isFlagged).length;
    var groups = [
      { label: 'Flagged by the audit (' + n(flaggedCount) + ')', rows: ordered.slice(0, flaggedCount) },
      { label: 'Comparison images, no issue reported (' + n(ordered.length - flaggedCount) + ')',
        rows: ordered.slice(flaggedCount) }
    ];
    var position = 0;
    groups.forEach(function (group) {
      if (!group.rows.length) { return; }
      var optgroup = el('optgroup');
      optgroup.label = group.label;
      group.rows.forEach(function (example) {
        position += 1;
        var option = el('option', null, position + '. ' + exampleLabel(example));
        option.value = example.id;
        optgroup.appendChild(option);
      });
      trySelect.appendChild(optgroup);
    });
    trySelect.disabled = !rows.length;
    tryNext.disabled = rows.length < 2;
    var pages = Math.max(1, Math.ceil(rows.length / PER_PAGE));
    if (browserState.page > pages) { browserState.page = pages; }
    var start = (browserState.page - 1) * PER_PAGE;
    var slice = rows.slice(start, start + PER_PAGE);
    slice.forEach(function (example) {
      var card = el('button', 'example-card');
      card.type = 'button';
      card.setAttribute('aria-pressed', String(example.id === browserState.selected));
      card.addEventListener('click', function () {
        browserState.selected = example.id;
        Array.prototype.forEach.call(listNode.querySelectorAll('button'), function (button) {
          button.setAttribute('aria-pressed', String(button === card));
        });
        renderDetail();
      });
      card.appendChild(el('span', 'card-badge', example.finding_label));
      if (example.preview.status === 'delivered' && example.preview.src) {
        var img = el('img', 'card-thumb');
        img.src = example.preview.src;
        img.alt = '';
        img.width = example.preview.width || 704;
        img.height = example.preview.height || 520;
        card.appendChild(img);
      }
      card.appendChild(el('span', 'card-name', example.file_name));
      var rank = rankOf(example, browserState.method, EXAMPLE_SEED);
      var meta = example.cell_line + ' · ' + example.split + ' · ' + n(example.n_annotations) +
        (example.n_annotations === 1 ? ' label' : ' labels');
      card.appendChild(el('span', 'card-meta', meta));
      var rankText = 'Rank ' + n(rank) + ' of ' + n(SITE.methods.pools.review.size) + ' by ' +
        METHOD_LABELS[browserState.method];
      var rankNode = el('span', 'card-meta', rankText);
      if (rank !== null && rank <= browserState.budget) {
        rankNode.className = 'card-meta in-budget';
        rankNode.textContent = rankText + ' · inside the top ' + browserState.budget;
      }
      card.appendChild(rankNode);
      if (example.is_known_case) { card.appendChild(el('span', 'card-meta', 'Upstream-reported case')); }
      listNode.appendChild(card);
    });
    if (!slice.length) {
      listNode.appendChild(el('p', null, 'No illustrated example matches these filters. Clear a filter to see more.'));
    }
    pageLabel.textContent = 'Page ' + browserState.page + ' of ' + pages + ' · ' + n(rows.length) +
      ' illustrated example' + (rows.length === 1 ? '' : 's');
    prevButton.disabled = browserState.page <= 1;
    nextButton.disabled = browserState.page >= pages;
    countNode.textContent = n(rows.length) + ' of ' + n(EXAMPLES.length) +
      ' illustrated examples match · ordered by ' + METHOD_LABELS[browserState.method] +
      ' · review budget top ' + browserState.budget + '.';
  }

  function kvList(items) {
    var dl = el('dl', 'kv');
    items.forEach(function (item) {
      var row = el('div');
      row.appendChild(el('dt', null, item[0]));
      var dd = el('dd', null, item[1]);
      if (item[2]) { dd.appendChild(el('span', 'unit', item[2])); }
      row.appendChild(dd);
      dl.appendChild(row);
    });
    return dl;
  }

  function renderDetail() {
    var example = EXAMPLES.filter(function (e) { return e.id === browserState.selected; })[0] || null;
    clear(detail);
    var rows = tryOrder(filtered());
    var currentIndex = rows.findIndex(function (row) { return row.id === browserState.selected; });
    trySelect.value = browserState.selected || '';
    setText('try-progress', example
      ? 'Image ' + (currentIndex + 1) + ' of ' + n(rows.length) +
        (isFlagged(example) ? ' · flagged by the audit' : ' · comparison image')
      : 'No matching images. Clear a filter below.');
    if (!example) {
      var emptyTitle = el('h3', null, 'No matching examples');
      emptyTitle.id = 'detail-title';
      detail.appendChild(emptyTitle);
      detail.appendChild(el('p', null, 'Clear a filter to see an image, its evidence and recommended review action.'));
      return;
    }
    var title = el('h3', null, exampleLabel(example));
    title.id = 'detail-title';
    detail.appendChild(title);

    var badges = el('div', 'badges');
    badges.appendChild(el('span', 'badge strong', example.finding_label));
    badges.appendChild(el('span', 'badge', example.cell_line + ' cells'));
    badges.appendChild(el('span', 'badge', 'split: ' + example.split));
    var partitions = {
      held_out: 'held-out training group',
      validation: 'validation split',
      test: 'test split',
      excluded_known_case: 'known case excluded from fitting'
    };
    badges.appendChild(el('span', 'badge', partitions[example.partition] || example.partition));
    if (example.is_known_case) { badges.appendChild(el('span', 'badge', 'upstream-reported case')); }
    if (example.is_alternate_version) { badges.appendChild(el('span', 'badge', 'alternative annotation version')); }
    if (example.is_cross_split_overlap) { badges.appendChild(el('span', 'badge', 'filename in two splits')); }
    var technical = el('details', 'details');
    technical.appendChild(el('summary', null, 'View this image’s counts, ranking and source'));
    var technicalContent = el('div', 'content');
    technical.appendChild(technicalContent);
    technicalContent.appendChild(el('p', 'filename', example.file_name));
    technicalContent.appendChild(badges);

    var grid = el('div', 'detail-grid');
    var left = el('div');
    var frame = el('div', 'preview-frame');
    if (example.preview.status === 'delivered' && example.preview.src) {
      var stage = el('div', 'image-overlay');
      var img = el('img');
      img.src = example.preview.src;
      img.alt = 'Microscope image ' + example.file_name + ' from the ' + example.split +
        ' split with ' + example.cell_line + ' cells.' +
        (overlayCheckbox.checked ? ' Yellow outlines mark the supplied labels.' : '');
      img.width = example.preview.width || 704;
      img.height = example.preview.height || 520;
      stage.appendChild(img);
      if (overlayCheckbox.checked && example.preview.labels_src) {
        /* Dense image: show every supplied outline from the server-rendered label
           layer, not a capped subset that would look like missing labels. */
        var layer = el('img', 'label-layer');
        layer.src = example.preview.labels_src;
        layer.alt = 'All ' + n(example.preview.labels_drawn) + ' supplied cell labels drawn over the image.';
        layer.width = example.preview.width || 704;
        layer.height = example.preview.height || 520;
        stage.appendChild(layer);
      } else if (overlayCheckbox.checked && example.preview.polygons && example.preview.polygons.length) {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', 'Outlines of the ' + example.preview.polygons_drawn +
          ' supplied labels drawn over the image.');
        drawOverlay(svg, example.preview.polygons, example.preview.width || 704,
          example.preview.height || 520, { locator: example.preview.polygons.length === 1 });
        stage.appendChild(svg);
      }
      frame.appendChild(stage);
      var readout = el('p', 'readout');
      readout.appendChild(el('span', null, example.file_name));
      var readoutMeta = el('span', 'readout-muted');
      var swatch = el('span', 'legend-swatch');
      swatch.setAttribute('aria-hidden', 'true');
      readoutMeta.appendChild(swatch);
      readoutMeta.appendChild(document.createTextNode(n(example.n_annotations) +
        (example.n_annotations === 1 ? ' label' : ' labels') + ' · ' +
        n(example.preview.width || 704) + ' × ' + n(example.preview.height || 520) + ' px'));
      readout.appendChild(readoutMeta);
      frame.appendChild(readout);
    } else {
      var missing = el('div', 'preview-missing');
      missing.appendChild(el('strong', null, 'This image is not included in this build.'));
      missing.appendChild(el('p', null, 'The image record, its counts and its ranks below are complete. ' +
        'The illustrated preview comes from a bounded server-side render (at most ' +
        n(SITE.meta.outline_cap) + ' outlines per dense image) and is not yet delivered, so no image is ' +
        'shown rather than the wrong one.'));
      frame.appendChild(missing);
    }
    var simpleGrid = el('div', 'try-image-grid');
    var simpleImage = el('div');
    simpleImage.appendChild(frame);
    var singleLabel = !example.preview.labels_src && example.preview.polygons &&
      example.preview.polygons.length === 1;
    var labelStatus = el('p', 'disclosure', overlayCheckbox.checked
      ? (example.preview.cap_note || 'Showing the supplied cell labels.') +
        (singleLabel ? ' The ring and pointer help you find it.' : '')
      : 'Labels hidden. You are looking at the original image.');
    labelStatus.id = 'try-label-status';
    labelStatus.setAttribute('role', 'status');
    simpleImage.appendChild(labelStatus);
    simpleGrid.appendChild(simpleImage);
    var simpleMeaning = el('div', 'try-meaning');
    simpleMeaning.appendChild(el('span', isFlagged(example) ? 'finding-chip' : 'finding-chip quiet',
      isFlagged(example) ? example.finding_label : 'Comparison image'));
    simpleMeaning.appendChild(el('h4', null, 'What the audit found'));
    var findingSummary = 'No issue is reported for this image. It is shown for comparison because a review ' +
      'method ranked it highly, which on its own does not mean its labels are wrong.';
    if (example.is_known_case) {
      findingSummary = 'Only ' + n(example.n_annotations) +
        (example.n_annotations === 1 ? ' cell label is' : ' cell labels are') +
        ' supplied for this image. It was flagged for review.';
    } else if (example.is_cross_split_overlap) {
      findingSummary = 'This image appears in both the learning and checking groups.';
    } else if (example.is_alternate_version) {
      findingSummary = 'This filename has more than one version of its cell labels. The versions need review.';
    }
    simpleMeaning.appendChild(el('p', null, findingSummary));
    simpleMeaning.appendChild(el('h4', null, 'What happens next'));
    simpleMeaning.appendChild(el('p', null, example.next_action));
    simpleMeaning.appendChild(el('p', 'disclosure', 'A flag is a reason for human review, not proof of an incorrect label.'));
    simpleGrid.appendChild(simpleMeaning);
    detail.appendChild(simpleGrid);
    detail.appendChild(technical);
    if (example.preview.cap_note) {
      left.appendChild(el('p', 'disclosure', example.preview.cap_note +
        ' Outlines are drawn as supplied; they are labels, not cell boundaries detected by this project.'));
    } else if (example.preview.status === 'delivered') {
      left.appendChild(el('p', 'disclosure', 'Outlines are drawn as supplied; they are labels, not cell ' +
        'boundaries detected by this project.'));
    }
    if (example.preview.tiff_sha256) {
      left.appendChild(el('p', 'disclosure', 'Source TIFF SHA-256: ' + example.preview.tiff_sha256 + '.'));
    }
    if (example.preview.png_sha256) {
      left.appendChild(el('p', 'disclosure', 'This display copy (PNG) SHA-256: ' +
        example.preview.png_sha256 + (example.preview.source_file
          ? '; rendered from ' + example.preview.source_file + '.' : '.')));
    }

    left.appendChild(el('h4', null, 'Why this image is listed'));
    var rank = rankOf(example, browserState.method, EXAMPLE_SEED);
    var inBudget = rank !== null && rank <= browserState.budget;
    var why = [];
    if (example.finding) {
      why.push(example.finding.title + ' (' + example.finding.status + ').');
      if (example.finding.note) { why.push(example.finding.note); }
    }
    why.push('Ordered ' + n(rank) + ' of ' + n(SITE.methods.pools.review.size) + ' by ' +
      METHOD_LABELS[browserState.method] + ', so it is ' + (inBudget ? 'inside' : 'outside') +
      ' the top ' + browserState.budget + ' for this method. Selection reason: ' +
      example.selection_reason.replace(/_/g, ' ') + '.');
    (example.observable_reason_text || []).forEach(function (line) { why.push(line); });
    var whyList = el('ol');
    why.forEach(function (line) { whyList.appendChild(el('li', null, line)); });
    left.appendChild(whyList);
    technicalContent.appendChild(grid);
    grid.appendChild(left);

    var right = el('div');
    right.appendChild(el('h4', null, 'Counts with units'));
    var countItems = [
      ['Supplied cell labels', n(example.n_annotations), 'count'],
      ['Review-pool median labels', n(SITE.distribution.n_annotations.median), 'count'],
      ['Median label area, this image', dec(example.features.area_median, 2), 'pixels^2'],
      ['Median label area, pool', dec(SITE.distribution.area_median.median, 2), 'pixels^2'],
      ['Median label width', dec(example.features.bbox_width_median, 2), 'pixels'],
      ['Median label height', dec(example.features.bbox_height_median, 2), 'pixels'],
      ['Median polygon points per label', dec(example.features.n_points_median, 1), 'points'],
      ['Mean pixel intensity', dec(example.features.pixel_mean, 2), 'intensity (0-255)'],
      ['Mean gradient magnitude', dec(example.features.gradient_mean, 3), 'intensity/pixel'],
      ['Recorded image size', n(example.features.width) + ' × ' + n(example.features.height), 'pixels'],
      ['Records sharing this filename', n(example.features.records_for_filename), 'count'],
      ['Splits using this filename', n(example.features.splits_for_filename), 'count'],
      ['Acquisition time after plating', dec(example.features.time_hours, 1), 'hours']
    ];
    right.appendChild(kvList(countItems));
    right.appendChild(el('p', 'disclosure', 'Pixel intensities are 8-bit values used exactly as stored, ' +
      'with no normalization or rescaling, so they are not comparable across bit depths. Area is the ' +
      'COCO polygon area, not a measured cell size.'));
    right.appendChild(el('h4', null, 'Rank among the 3,067 review-pool candidates'));
    var rankTable = el('table', 'rank-table');
    var head = el('thead');
    var headRow = el('tr');
    ['Method', 'Seed 7', 'Seed 42', 'Seed 101'].forEach(function (label, index) {
      var th = el('th', index ? 'num' : null, label);
      if (index) { th.scope = 'col'; }
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    rankTable.appendChild(head);
    var body = el('tbody');
    METHOD_ORDER.forEach(function (method) {
      var row = el('tr');
      if (method === browserState.method) { row.className = 'row-highlight'; }
      row.appendChild(el('th', null, METHOD_LABELS[method]));
      var seeded = method === 'isolation_forest' || method === 'random';
      ['7', '42', '101'].forEach(function (seed) {
        var value = seeded ? rankOf(example, method, seed)
          : (seed === '42' ? rankOf(example, method, EXAMPLE_SEED) : null);
        row.appendChild(el('td', 'num', value === null ? '—' : n(value)));
      });
      body.appendChild(row);
    });
    rankTable.appendChild(body);
    var rankWrap = el('div', 'table-wrap');
    rankWrap.appendChild(rankTable);
    right.appendChild(rankWrap);
    right.appendChild(el('p', 'disclosure', 'The low-count and robust baselines do not depend on a seed, ' +
      'so they show one rank. Priority for ' + METHOD_LABELS[browserState.method] + ' is ' +
      dec(priorityOf(example, browserState.method), 4) + '. Priorities are relative review priorities, ' +
      'never probabilities that a label is wrong.'));

    right.appendChild(el('h4', null, 'What remains uncertain'));
    var uncertain = el('ul');
    example.uncertainty.forEach(function (line) { uncertain.appendChild(el('li', null, line)); });
    right.appendChild(uncertain);

    var action = el('div', 'next-action');
    action.appendChild(el('strong', null, 'Recommended next action'));
    action.appendChild(el('p', null, example.next_action));
    right.appendChild(action);

    right.appendChild(el('h4', null, 'Provenance'));
    right.appendChild(kvList([
      ['Row key (split:position)', example.record_key, 'identifier'],
      ['COCO image id', n(example.image_id), 'identifier'],
      ['Acquisition group', example.acquisition_group || '—', 'acquisition context'],
      ['Selection reason', example.selection_reason.replace(/_/g, ' '), ''],
      ['Preview id', example.preview.preview_id, '']
    ]));
    if (example.finding) {
      right.appendChild(el('p', 'disclosure', 'Audit finding ' + example.finding.finding_id +
        ' · source record: ' + example.finding.records.map(function (rec) {
          return rec.split + ' image id ' + (rec.image_ids || []).join(', ') + ' with ' +
            (rec.counts || []).join('/') + ' labels';
        }).join('; ') + '.'));
    }
    grid.appendChild(right);
  }

  function renderBrowse() {
    var rows = filtered();
    browserState.page = Math.min(browserState.page, Math.max(1, Math.ceil(rows.length / PER_PAGE)));
    var visible = rows.slice((browserState.page - 1) * PER_PAGE, browserState.page * PER_PAGE);
    if (!visible.some(function (r) { return r.id === browserState.selected; })) {
      browserState.selected = visible.length ? visible[0].id : null;
    }
    renderCards(rows);
    renderDetail();
  }

  $('browse-search').addEventListener('input', function (event) {
    browserState.query = event.target.value; browserState.page = 1; renderBrowse();
  });
  findingSelect.addEventListener('change', function (event) {
    browserState.finding = event.target.value; browserState.page = 1; renderBrowse();
  });
  cellLineSelect.addEventListener('change', function (event) {
    browserState.cellLine = event.target.value; browserState.page = 1; renderBrowse();
  });
  $('browse-split').addEventListener('change', function (event) {
    browserState.split = event.target.value; browserState.page = 1; renderBrowse();
  });
  methodSelect.addEventListener('change', function (event) {
    browserState.method = event.target.value; renderBrowse();
  });
  budgetSelect.addEventListener('change', function (event) {
    browserState.budget = Number(event.target.value); renderBrowse();
  });
  overlayCheckbox.addEventListener('change', renderDetail);
  trySelect.addEventListener('change', function () {
    var rows = filtered();
    var index = rows.findIndex(function (row) { return row.id === trySelect.value; });
    if (index < 0) { return; }
    browserState.selected = rows[index].id;
    browserState.page = Math.floor(index / PER_PAGE) + 1;
    renderBrowse();
  });
  tryNext.addEventListener('click', function () {
    var rows = filtered();
    var ordered = tryOrder(rows);
    if (!ordered.length) { return; }
    var position = ordered.findIndex(function (row) { return row.id === browserState.selected; });
    browserState.selected = ordered[(position + 1) % ordered.length].id;
    var index = rows.findIndex(function (row) { return row.id === browserState.selected; });
    browserState.page = Math.floor(index / PER_PAGE) + 1;
    renderBrowse();
  });
  prevButton.addEventListener('click', function () {
    browserState.page = Math.max(1, browserState.page - 1); renderBrowse();
  });
  nextButton.addEventListener('click', function () {
    browserState.page = browserState.page + 1; renderBrowse();
  });

  /* ------------------------------------------------------------ compare */
  var known = SITE.methods.knownCase;
  var synthetic = SITE.methods.synthetic;
  var compareBudget = 25;

  function severityRange(method, budget) {
    var row = known.recovery_table.filter(function (r) { return r.method === method; })[0];
    if (!row) { return { min: null, max: null }; }
    var values = Object.keys(row.seeds).map(function (seed) { return row.seeds[seed][String(budget)]; })
      .filter(function (v) { return typeof v === 'number'; });
    return { min: Math.min.apply(null, values), max: Math.max.apply(null, values) };
  }
  function severityAtSeed(method, seed, budget) {
    var row = known.recovery_table.filter(function (r) { return r.method === method; })[0];
    return row && row.seeds[String(seed)] ? row.seeds[String(seed)][String(budget)] : null;
  }

  function rangeText(range) {
    if (range.min === null) { return '—'; }
    return range.min === range.max ? String(range.min) : range.min + '–' + range.max;
  }
  var lowRange = severityRange('low_count', 10);
  var ifRange = severityRange('isolation_forest', 10);
  var robustRange = severityRange('robust', 10);
  setText('compare-headline', 'At the same review budget the simple low-count check recovered ' +
    rangeText(lowRange) + ' of the nine reported cases inside the top 10, the robust ' +
    'statistical baseline recovered ' + rangeText(robustRange) + ', and the ' +
    'Isolation Forest recovered ' + rangeText(ifRange) + ' (across seeds 7, 42 and 101). ' +
    'On the simulated annotation-loss task the low-count check also had the best ROC AUC (' +
    dec(synthetic.seed_summary.low_count.synthetic_roc_auc.mean, 3) + ') against the Isolation Forest (' +
    dec(synthetic.seed_summary.isolation_forest.synthetic_roc_auc.mean, 3) + '). The machine-learning ' +
    'ranking did not add value over the existing rule. Read that with care: both tests are defined by ' +
    'label count, so they favour a counting rule by design and cannot show what a model adds beyond ' +
    'counting.');

  var budgetGroup = $('compare-budget-group');
  function renderBudgetButtons() {
    clear(budgetGroup);
    BUDGETS.forEach(function (budget) {
      var button = el('button', null, 'Top ' + budget);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(budget === compareBudget));
      button.addEventListener('click', function () {
        compareBudget = budget;
        Array.prototype.forEach.call(budgetGroup.querySelectorAll('button'), function (control) {
          control.setAttribute('aria-pressed', String(control === button));
        });
        renderCompare();
      });
      budgetGroup.appendChild(button);
    });
  }

  /* A horizontal bar per method: the solid part is the measured value, the
     hatched track is what is left up to the maximum. */
  function methodBars(target, rows, caption) {
    clear(target);
    var list = el('ul', 'hbars');
    list.setAttribute('aria-label', caption);
    rows.forEach(function (row) {
      var item = el('li', 'hbar' + (row.method === 'low_count' ? ' is-default' : ''));
      item.appendChild(el('span', 'hbar-label', METHOD_LABELS[row.method]));
      var track = el('span', 'hbar-track');
      var fill = el('i');
      fill.style.width = Math.max(0, Math.min(100, row.share * 100)) + '%';
      track.appendChild(fill);
      if (typeof row.marker === 'number') {
        var marker = el('span', 'hbar-marker');
        marker.style.left = (row.marker * 100) + '%';
        marker.setAttribute('aria-hidden', 'true');
        track.appendChild(marker);
      }
      item.appendChild(track);
      item.appendChild(el('span', 'hbar-value num', row.text));
      list.appendChild(item);
    });
    target.appendChild(list);
  }
  function renderMethodBars() {
    methodBars($('compare-bars'), METHOD_ORDER.map(function (method) {
      var range = severityRange(method, compareBudget);
      return { method: method, share: (range.max || 0) / known.total, text: rangeText(range) + ' / ' + n(known.total) };
    }), 'Reported cases inside the top ' + compareBudget + ', range over seeds 7, 42 and 101');
  }

  function renderCompare() {
    renderMethodBars();
    setText('compare-budget-note', 'Showing the top ' + compareBudget + ' of ' + n(known.pool_size) +
      ' review-pool candidates. A recovered case must exist in the preserved provenance, be that exact ' +
      'annotation version, and rank inside the budget.');

    setText('compare-known-intro', 'All ' + n(known.total) + ' reported identities are present: ' +
      n(known.reported_identity_present) + ' present in the preserved provenance, ' +
      n(known.canonical_matches_reported) + ' canonical versions equal to the reported version, ' +
      n(known.in_fit) + ' in the fit set and ' + n(known.in_pool) + ' in the review pool. Evidence type: ' +
      known.evidence_type + '.');

    var knownTable = $('compare-known-table');
    clear(knownTable);
    var caption = el('caption', null, 'How many of the nine reported cases each method placed inside the ' +
      'top ' + compareBudget + '. The denominator is nine examples, not every mislabelled image in the dataset.');
    knownTable.appendChild(caption);
    var head = el('thead');
    var headRow = el('tr');
    ['Method', 'Seed 7', 'Seed 42', 'Seed 101', 'Recovered at top ' + compareBudget].forEach(function (label, index) {
      var th = el('th', index ? 'num' : null, label);
      if (index) { th.scope = 'col'; }
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    knownTable.appendChild(head);
    var body = el('tbody');
    METHOD_ORDER.forEach(function (method) {
      var rowData = known.recovery_table.filter(function (r) { return r.method === method; })[0];
      if (!rowData) { return; }
      var row = el('tr');
      row.appendChild(el('th', null, METHOD_LABELS[method] + (method === 'low_count' ? ' (default view)' : '')));
      SEEDS.forEach(function (seed) {
        row.appendChild(el('td', 'num', n(severityAtSeed(method, seed, compareBudget))));
      });
      var range = severityRange(method, compareBudget);
      row.appendChild(el('td', 'num', rangeText(range) + ' of ' + n(known.total)));
      body.appendChild(row);
    });
    knownTable.appendChild(body);

    var lossText;
    if (typeof synthetic.annotations_before === 'number' && typeof synthetic.annotations_after === 'number') {
      lossText = n(synthetic.injected_images) + ' of ' + n(synthetic.pool_size) + ' held-out images were ' +
        'corrupted on purpose: each kept only about ' + pct(synthetic.retained_fraction, 0) + ' of its ' +
        'annotations, so together they went from ' + n(synthetic.annotations_before) + ' to ' +
        n(synthetic.annotations_after) + ' annotations (' +
        pct(1 - synthetic.annotations_after / synthetic.annotations_before, 0) + ' removed). ';
    } else {
      lossText = n(synthetic.injected_images) + ' of ' + n(synthetic.pool_size) + ' held-out images (' +
        pct(synthetic.injected_fraction, 0) + ' of the pool) had most of their annotations removed on ' +
        'purpose. ';
    }
    setText('compare-synthetic-intro', lossText + 'The retained annotations are the first ones in each ' +
      'image’s stored order: a deliberately structured removal scenario, not random dropout. ' +
      'Because so many labels are removed, a label count separates these images easily.');

    var synthTable = $('compare-synthetic-table');
    clear(synthTable);
    synthTable.appendChild(el('caption', null, 'Precision@K for finding the images whose labels were removed, ' +
      'and ROC AUC, on the simulated pool. Simulation metrics, not real-world error precision.'));
    var shead = el('thead');
    var sheadRow = el('tr');
    ['Method', 'Seed', 'Precision@10', 'Precision@25', 'Precision@50', 'ROC AUC'].forEach(function (label, index) {
      var th = el('th', index > 1 ? 'num' : null, label);
      if (index) { th.scope = 'col'; }
      sheadRow.appendChild(th);
    });
    shead.appendChild(sheadRow);
    synthTable.appendChild(shead);
    var sbody = el('tbody');
    METHOD_ORDER.forEach(function (method) {
      SEEDS.forEach(function (seed) {
        var key = method + '_seed' + seed;
        var metrics = synthetic.metrics[key];
        if (!metrics) { return; }
        var row = el('tr');
        if (method === 'low_count') { row.className = 'row-highlight'; }
        row.appendChild(el('th', null, METHOD_LABELS[method]));
        row.appendChild(el('td', null, String(seed)));
        row.appendChild(el('td', 'num', dec(metrics.precision_at_k.at_10, 3)));
        row.appendChild(el('td', 'num', dec(metrics.precision_at_k.at_25, 3)));
        row.appendChild(el('td', 'num', dec(metrics.precision_at_k.at_50, 3)));
        row.appendChild(el('td', 'num', dec(metrics.synthetic_roc_auc, 4)));
        sbody.appendChild(row);
      });
    });
    synthTable.appendChild(sbody);

    var casesTable = $('compare-cases-table');
    clear(casesTable);
    casesTable.appendChild(el('caption', null, 'Rank within the 3,067-row review pool at the default seed 42. ' +
      'Lower is a higher review priority.'));
    var chead = el('thead');
    var cheadRow = el('tr');
    ['Filename', 'Split', 'Labels', 'Isolation Forest', 'Robust', 'Low count', 'Random'].forEach(function (label, index) {
      var th = el('th', index > 1 ? 'num' : null, label);
      if (index) { th.scope = 'col'; }
      cheadRow.appendChild(th);
    });
    chead.appendChild(cheadRow);
    casesTable.appendChild(chead);
    var cbody = el('tbody');
    known.per_case.forEach(function (item) {
      var row = el('tr');
      row.appendChild(el('th', null, item.file_name));
      row.appendChild(el('td', null, item.split));
      row.appendChild(el('td', 'num', n(item.observed_annotations)));
      row.appendChild(el('td', 'num', n(item.ranks.isolation_forest_seed42)));
      row.appendChild(el('td', 'num', n(item.ranks.robust_seed42)));
      row.appendChild(el('td', 'num', n(item.ranks.low_count_seed42)));
      row.appendChild(el('td', 'num', n(item.ranks.random_seed42)));
      cbody.appendChild(row);
    });
    casesTable.appendChild(cbody);

    var poolsTable = $('compare-pools-table');
    clear(poolsTable);
    poolsTable.appendChild(el('caption', null, 'Two pools with two different purposes. The website table and the ' +
      'example browser use the review pool only.'));
    var phead = el('thead');
    var pheadRow = el('tr');
    ['Pool', 'Size', 'Definition', 'Used for'].forEach(function (label, index) {
      var th = el('th', index === 1 ? 'num' : null, label);
      if (index) { th.scope = 'col'; }
      pheadRow.appendChild(th);
    });
    phead.appendChild(pheadRow);
    poolsTable.appendChild(phead);
    var pbody = el('tbody');
    ['review', 'synthetic'].forEach(function (key) {
      var pool = SITE.methods.pools[key];
      var row = el('tr');
      row.appendChild(el('th', null, key === 'review' ? 'Review pool' : 'Synthetic pool'));
      row.appendChild(el('td', 'num', n(pool.size)));
      row.appendChild(el('td', null, pool.definition));
      row.appendChild(el('td', null, pool.use));
      pbody.appendChild(row);
    });
    poolsTable.appendChild(pbody);

    setText('compare-won', 'The low-count check won on every stated metric: ' + n(severityAtSeed('low_count', 42, 10)) +
      ' of 9 known cases inside the top 10, precision@K = 1.000 at all three budgets on the simulated task, ' +
      'and the best simulated ROC AUC (' + dec(synthetic.seed_summary.low_count.synthetic_roc_auc.mean, 4) + '). ' +
      'Both tests are defined by label count, so this is the expected result, not a discovery.');
    setText('compare-lost', 'The Isolation Forest lost to that rule: ' + n(severityAtSeed('isolation_forest', 42, 10)) +
      ' of 9 known cases inside the top 10 and a simulated ROC AUC of ' +
      dec(synthetic.seed_summary.isolation_forest.synthetic_roc_auc.mean, 4) + ', above the seeded random ' +
      'reference (' + dec(synthetic.seed_summary.random.synthetic_roc_auc.mean, 4) + '). It scores how ' +
      'unusual an image looks overall, mixing label statistics with image texture, so it is not built to ' +
      'find labels that disagree with their image. No hyperparameter was tuned to change the result.');
    setText('compare-limits', 'What would test a model fairly: a criterion that is not a label count, such ' +
      'as a scientist’s review of the frozen sample, or agreement between the images that were ' +
      'annotated twice.');

    /* One list, each caveat once: the evaluation's evidence notes, then the
       model card's limitations that the notes do not already cover. */
    var notes = $('compare-notes');
    clear(notes);
    function contentWords(text) {
      return text.toLowerCase().split(/[^a-z]+/).filter(function (word) { return word.length > 3; });
    }
    var listed = [];
    SITE.methods.evidenceNotes.concat(SITE.methods.limitations).forEach(function (note) {
      var words = contentWords(note);
      var repeats = listed.some(function (seen) {
        var shared = words.filter(function (word) { return seen.indexOf(word) !== -1; }).length;
        return words.length && shared / words.length >= 0.6;
      });
      if (repeats) { return; }
      listed.push(words);
      notes.appendChild(el('li', null, note));
    });
    var reviewerNote = SITE.methods.humanReview.reviewer_note.replace(/\s*Real-world precision is unmeasured\.?$/, '');
    notes.appendChild(el('li', null, reviewerNote.charAt(0).toUpperCase() + reviewerNote.slice(1) +
      (/\.$/.test(reviewerNote) ? '' : '.')));
  }
  renderBudgetButtons();
  renderCompare();

  /* ----------------------------------------------------------- rankings */
  var columns = RANK.columns;
  var index = {};
  columns.forEach(function (name, position) { index[name] = position; });
  var rankState = { query: '', cellLine: '', split: '', order: 'low_count', knownOnly: false, page: 1 };
  var RANK_PER_PAGE = 25;

  var orderSelect = $('rankings-order');
  METHOD_ORDER.forEach(function (method) {
    var option = el('option', null, METHOD_LABELS[method] + ' rank (ascending)');
    option.value = method;
    orderSelect.appendChild(option);
  });
  orderSelect.value = 'low_count';
  var rankCellLine = $('rankings-cell-line');
  SITE.methods.model.fitted_cell_lines.forEach(function (name) {
    var option = el('option', null, name);
    option.value = name;
    rankCellLine.appendChild(option);
  });

  function orderRank(row, method) {
    var key = method === 'low_count' ? 'low_count_rank'
      : method === 'robust' ? 'robust_rank'
        : method === 'isolation_forest' ? 'if_rank_seed42' : 'random_rank_seed42';
    var value = row[index[key]];
    return typeof value === 'number' ? value : Infinity;
  }

  function rankRows() {
    var query = rankState.query.trim().toLowerCase();
    var rows = RANK.rows.filter(function (row) {
      if (query && String(row[index.file_name]).toLowerCase().indexOf(query) === -1) { return false; }
      if (rankState.cellLine && row[index.cell_line] !== rankState.cellLine) { return false; }
      if (rankState.split && row[index.split] !== rankState.split) { return false; }
      if (rankState.knownOnly && !row[index.is_known_case]) { return false; }
      return true;
    });
    rows.sort(function (a, b) {
      var ra = orderRank(a, rankState.order);
      var rb = orderRank(b, rankState.order);
      if (ra !== rb) { return ra - rb; }
      return String(a[index.file_name]) < String(b[index.file_name]) ? -1 : 1;
    });
    return rows;
  }

  function illustratedIds() {
    var ids = {};
    EXAMPLES.forEach(function (example) { ids[example.id] = example; });
    return ids;
  }
  var ILLUSTRATED = illustratedIds();

  function renderRankings() {
    var rows = rankRows();
    var pages = Math.max(1, Math.ceil(rows.length / RANK_PER_PAGE));
    if (rankState.page > pages) { rankState.page = pages; }
    var start = (rankState.page - 1) * RANK_PER_PAGE;
    var slice = rows.slice(start, start + RANK_PER_PAGE);

    var table = $('rankings-table');
    clear(table);
    table.appendChild(el('caption', null, 'Complete projection of the ' + n(rows.length) + ' matching rows of ' +
      'the ' + n(RANK.rows.length) + '-row review pool. Units: labels are a count, areas are pixels^2, ' +
      'dimensions are pixels, pixel statistics are 8-bit intensity (0-255, unrescaled). Rows marked ' +
      '“not illustrated” have no image preview in this bundle; their numbers are complete.'));
    var head = el('thead');
    var headRow = el('tr');
    ['#', 'Filename', 'Split', 'Cell line', 'Labels', 'Isolation Forest', 'Robust', 'Low count', 'Random', 'Illustrated']
      .forEach(function (label, position) {
        var th = el('th', position >= 4 && position <= 8 ? 'num' : null, label);
        if (position) { th.scope = 'col'; }
        headRow.appendChild(th);
      });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el('tbody');
    slice.forEach(function (row) {
      var tr = el('tr');
      var pid = row[index.split] + '_' + row[index.image_id];
      if (ILLUSTRATED[pid]) { tr.className = 'row-highlight'; }
      tr.appendChild(el('td', 'num', n(orderRank(row, rankState.order))));
      tr.appendChild(el('th', null, String(row[index.file_name])));
      tr.appendChild(el('td', null, String(row[index.split])));
      tr.appendChild(el('td', null, String(row[index.cell_line])));
      tr.appendChild(el('td', 'num', n(row[index.n_annotations])));
      tr.appendChild(el('td', 'num', n(row[index.if_rank_seed42])));
      tr.appendChild(el('td', 'num', n(row[index.robust_rank])));
      tr.appendChild(el('td', 'num', n(row[index.low_count_rank])));
      tr.appendChild(el('td', 'num', n(row[index.random_rank_seed42])));
      tr.appendChild(el('td', null, ILLUSTRATED[pid]
        ? 'Yes — ' + ILLUSTRATED[pid].finding_label : 'Not illustrated'));
      body.appendChild(tr);
    });
    table.appendChild(body);

    setText('rankings-count', n(rows.length) + ' of ' + n(RANK.rows.length) + ' review-pool rows match · ' +
      'ordered by ' + METHOD_LABELS[rankState.order] + ' · highlighted rows have an image in Image review.');
    setText('rankings-page', 'Page ' + rankState.page + ' of ' + pages);
    $('rankings-prev').disabled = rankState.page <= 1;
    $('rankings-next').disabled = rankState.page >= pages;
  }

  setText('rankings-intro', 'Every one of the ' + n(RANK.rows.length) + ' review-pool candidates, with its ' +
    'rank in all four methods. Search by filename, filter by cell line and split, and order by any method. ' +
    'The full table is also downloadable below. Rows without an image in Image review simply have no ' +
    'preview in this bundle; their numbers are complete.');

  $('rankings-search').addEventListener('input', function (event) {
    rankState.query = event.target.value; rankState.page = 1; renderRankings();
  });
  $('rankings-cell-line').addEventListener('change', function (event) {
    rankState.cellLine = event.target.value; rankState.page = 1; renderRankings();
  });
  $('rankings-split').addEventListener('change', function (event) {
    rankState.split = event.target.value; rankState.page = 1; renderRankings();
  });
  orderSelect.addEventListener('change', function (event) {
    rankState.order = event.target.value; renderRankings();
  });
  $('rankings-known').addEventListener('change', function (event) {
    rankState.knownOnly = event.target.checked; rankState.page = 1; renderRankings();
  });
  $('rankings-prev').addEventListener('click', function () {
    rankState.page = Math.max(1, rankState.page - 1); renderRankings();
  });
  $('rankings-next').addEventListener('click', function () {
    rankState.page = rankState.page + 1; renderRankings();
  });
  renderRankings();

  /* --------------------------------------------------------- technical */
  setText('source-file-count', n(Object.keys(audit.source_files).length));
  setText('source-preview-count', n(SITE.provenance.preview_png_count || 0));
  var provTable = $('provenance-table');
  clear(provTable);
  provTable.appendChild(el('caption', null, 'Every verified file behind this site — the inputs it ' +
    'reads and the display previews rendered from them — with its size and SHA-256. All paths are ' +
    'repository-relative.'));
  var phead = el('thead');
  var pheadRow = el('tr');
  ['Input', 'Role', 'Path', 'Bytes', 'SHA-256'].forEach(function (label, position) {
    var th = el('th', position === 3 ? 'num' : null, label);
    if (position) { th.scope = 'col'; }
    pheadRow.appendChild(th);
  });
  phead.appendChild(pheadRow);
  provTable.appendChild(phead);
  var pbody = el('tbody');
  SITE.provenance.files.forEach(function (file) {
    var row = el('tr');
    row.appendChild(el('th', null, file.label));
    row.appendChild(el('td', null, file.role));
    row.appendChild(el('td', 'hash', file.path));
    row.appendChild(el('td', 'num', n(file.bytes)));
    row.appendChild(el('td', 'hash', file.sha256));
    pbody.appendChild(row);
  });
  provTable.appendChild(pbody);

  var sourceTable = $('source-table');
  clear(sourceTable);
  sourceTable.appendChild(el('caption', null, 'The three COCO annotation files as recorded by the audit. ' +
    'The ranking provenance carries the same hashes.'));
  var shead = el('thead');
  var sheadRow = el('tr');
  ['Split', 'Path inside the data root', 'Bytes', 'SHA-256'].forEach(function (label, position) {
    var th = el('th', position === 2 ? 'num' : null, label);
    if (position) { th.scope = 'col'; }
    sheadRow.appendChild(th);
  });
  shead.appendChild(sheadRow);
  sourceTable.appendChild(shead);
  var sbody = el('tbody');
  Object.keys(audit.source_files).forEach(function (split) {
    var source = audit.source_files[split];
    var row = el('tr');
    row.appendChild(el('th', null, split));
    row.appendChild(el('td', 'hash', source.path));
    row.appendChild(el('td', 'num', n(source.bytes)));
    row.appendChild(el('td', 'hash', source.sha256));
    sbody.appendChild(row);
  });
  sourceTable.appendChild(sbody);

  var provenance = SITE.provenance;
  setText('hash-chain', 'Feature manifest SHA-256 ' + provenance.feature_manifest_sha256 +
    '; warehouse manifest SHA-256 ' + provenance.warehouse_manifest_sha256 + '. The annotation ' +
    'aggregation was reproduced on a deterministic bounded sample: maximum absolute difference ' +
    dec(provenance.annotation_reproduction.annotation_reproduction_max_abs_diff, 12) + ' in pandas and ' +
    dec(provenance.annotation_reproduction.annotation_reproduction_max_abs_diff_sql, 12) + ' in SQL. ' +
    (provenance.preview_manifest_sha256
      ? 'Preview manifest SHA-256 ' + provenance.preview_manifest_sha256 +
        '; the illustrated example set it was rendered from has SHA-256 ' +
        provenance.preview_curated_sha256 + ' and is byte-identical to this site\'s ' +
        'curated-examples.json. '
      : '') +
    provenance.note);

  var review = SITE.methods.humanReview;
  setText('review-status', 'Status: ' + review.status.replace(/_/g, ' ') + '. The frozen sample holds ' +
    n(review.records) + ' review-pool candidates in four disjoint strata (' + review.strata.join(', ') +
    '). Real-world precision: ' + (review.real_world_precision === null ? 'unmeasured (no reviewer supplied)' :
      String(review.real_world_precision)) + '. ' + review.sampling_caveat + ' ' + review.reviewer_note +
    ' The sample is downloadable with its judgment fields blank.');

  var limTable = $('limitations-table');
  clear(limTable);
  limTable.appendChild(el('caption', null, 'Published limitations from the ranking model card and the ' +
    'evaluation evidence notes.'));
  var lhead = el('thead');
  var lheadRow = el('tr');
  ['Source', 'Limitation'].forEach(function (label, position) {
    var th = el('th', null, label);
    if (position) { th.scope = 'col'; }
    lheadRow.appendChild(th);
  });
  lhead.appendChild(lheadRow);
  limTable.appendChild(lhead);
  var lbody = el('tbody');
  SITE.methods.limitations.forEach(function (text) {
    var row = el('tr');
    row.appendChild(el('th', null, 'model card'));
    row.appendChild(el('td', 'wrap', text));
    lbody.appendChild(row);
  });
  SITE.methods.evidenceNotes.forEach(function (text) {
    var row = el('tr');
    row.appendChild(el('th', null, 'evaluation'));
    row.appendChild(el('td', 'wrap', text));
    lbody.appendChild(row);
  });
  limTable.appendChild(lbody);

  var criteria = $('review-criteria');
  clear(criteria);
  review.review_criteria.forEach(function (text) { criteria.appendChild(el('li', null, text)); });

  var model = SITE.methods.model;
  setText('model-config', 'Fitted cell lines: ' + model.fitted_cell_lines.join(', ') + '. Protocol: budgets ' +
    BUDGETS.join('/') + ', default seed ' + DEFAULT_SEED + ', sensitivity seeds ' + SEEDS.join('/') +
    ', Isolation Forest ' + model.protocol.isolation_forest.n_estimators + ' estimators with ' +
    model.protocol.isolation_forest.max_samples + ' samples, contamination ' +
    model.protocol.isolation_forest.contamination + '. Preprocessing: ' + model.protocol.preprocessing +
    '. ' + model.global_fallback_rule + ' Feature selection excludes identifiers, bookkeeping flags and ' +
    'acquisition context, leaving ' + model.feature_selection.model_features.length + ' model features (' +
    model.feature_selection.robust_features.length + ' for the robust baseline).');

  var partTable = $('partition-table');
  clear(partTable);
  var pahead = el('thead');
  var paheadRow = el('tr');
  ['Cell line', 'Fit images', 'Hold-out images', 'Acquisition groups', 'Known-case records excluded']
    .forEach(function (label, position) {
      var th = el('th', position ? 'num' : null, label);
      if (position) { th.scope = 'col'; }
      paheadRow.appendChild(th);
    });
  pahead.appendChild(paheadRow);
  partTable.appendChild(pahead);
  var pabody = el('tbody');
  SITE.methods.partition.cells.forEach(function (cell) {
    var row = el('tr');
    row.appendChild(el('th', null, cell.cell_line));
    row.appendChild(el('td', 'num', n(cell.n_fit_images)));
    row.appendChild(el('td', 'num', n(cell.n_holdout_images)));
    row.appendChild(el('td', 'num', n(cell.n_groups)));
    row.appendChild(el('td', 'num', n(cell.n_known_case_records_excluded)));
    pabody.appendChild(row);
  });
  var totalRow = el('tr');
  totalRow.appendChild(el('th', null, 'All cell lines'));
  totalRow.appendChild(el('td', 'num', n(SITE.methods.partition.n_fit_images)));
  totalRow.appendChild(el('td', 'num', ''));
  totalRow.appendChild(el('td', 'num', ''));
  totalRow.appendChild(el('td', 'num', ''));
  pabody.appendChild(totalRow);
  partTable.appendChild(pabody);
  setText('model-config', $('model-config').textContent + ' Partition caveat: ' +
    SITE.methods.partition.caveat);

  var glossary = $('glossary');
  clear(glossary);
  SITE.glossary.forEach(function (item) {
    var wrap = el('div');
    wrap.appendChild(el('dt', null, item.term));
    wrap.appendChild(el('dd', null, item.meaning));
    glossary.appendChild(wrap);
  });

  var bundleNotes = $('bundle-notes');
  clear(bundleNotes);
  [
    'This bundle holds ' + n(EXAMPLES.length) + ' illustrated examples chosen by the documented rule (' +
      SITE.exampleSelection.rule.map(function (step) {
        return step.limit + ' × ' + step.description;
      }).join('; ') + ').',
    'Image previews: ' + (SITE.meta.preview_status === 'delivered'
      ? 'included for all ' + n(EXAMPLES.length) + ' illustrated examples — downsampled display PNGs ' +
        '(longest side at most ' + n((SITE.provenance.preview_render || {}).longest_side || 640) +
        ' px). ' + (SITE.provenance.preview_labels_count
          ? n(SITE.provenance.preview_labels_count) + ' dense images also have a transparent label layer ' +
            'with every supplied outline; the other images draw their outlines directly.'
          : 'At most ' + n(SITE.meta.outline_cap) + ' supplied outlines are drawn per dense image.')
      : 'pending a bounded server-side render; the page labels them as not included rather than showing ' +
        'the wrong image.'),
    'It does not contain the original TIFF images, the database, the fitted model files or any local ' +
      'filesystem path; the only images in the bundle are the downsampled display previews.',
    'It shows no invented health score, no invented percentage, and no working “run audit” control.',
    'It contains no fabricated expert judgment: every review-sample judgment field is blank because no ' +
      'scientific reviewer has been supplied.',
    'Real-world precision and recall are not claimed anywhere in this bundle. The known-case numbers are ' +
      'a regression exercise on nine examples.',
    'A comprehension check with a person unfamiliar with the field has not been performed; the five ' +
      'questions above are design goals, not measured usability results.'
  ].forEach(function (text) { bundleNotes.appendChild(el('li', null, text)); });

  setText('scope-text', 'Independent snapshot verification, a bounded feature build, a fit-only review ' +
    'ranking, an honest fixed-budget evaluation, a repeatable audit pipeline, automated checks, local ' +
    'release tooling and this interface are implemented. Reviewed policy exports and a scientific review ' +
    'of the flagged images remain to be done. This website does not run a new audit and does not change ' +
    'the dataset.');

  setText('footer-note', 'Site built from ' + n(provenance.files.length) + ' verified files ' +
    '(each listed with its SHA-256 in Technical details). ' + provenance.chain.join(' '));

  /* ---------------------------------------------------------- dashboard */
  /* KPI tiles and charts. Each figure is derived from the same data file as the
     tables, so a tile and its table can never disagree. */
  function setCount(id, value, decimals) {
    var node = $(id);
    if (!node || typeof value !== 'number' || !isFinite(value)) { return; }
    node.dataset.count = String(value);
    if (decimals) { node.dataset.decimals = String(decimals); }
    node.textContent = formatCount(node, value);
  }
  function setChip(id, text, tone) {
    var node = $(id);
    if (!node) { return; }
    node.textContent = text;
    if (tone) { node.className = 'chip ' + tone; }
  }
  function setBar(id, numerator, denominator, label) {
    var bar = $(id);
    if (!bar || !denominator) { return; }
    bar.querySelector('i').dataset.pct = String((numerator / denominator) * 100);
    bar.setAttribute('aria-label', label);
  }
  var splitByName = {};
  audit.splits.forEach(function (split) { splitByName[split.split] = split; });
  var valFiles = splitByName.val ? splitByName.val.distinct_filenames : null;

  setCount('ov-labels', c.annotations);
  setText('ov-labels-note', 'in ' + n(c.image_records) + ' image records');
  setChip('ov-files-chip', (c.missing_image_filenames ? '' : '\u2713 ') + n(c.missing_image_filenames) + ' missing images',
    c.missing_image_filenames ? 'down' : 'up');
  var splitTiles = $('ov-splits');
  clear(splitTiles);
  [['train', 'Learning'], ['val', 'Checking'], ['test', 'Held back']].forEach(function (pair) {
    var split = splitByName[pair[0]];
    if (!split) { return; }
    var tile = el('div', 'split-tile');
    tile.setAttribute('role', 'listitem');
    tile.appendChild(el('span', 'split-name', pair[1] + ' \u00b7 ' + pair[0]));
    tile.appendChild(el('b', 'num', n(split.image_records)));
    tile.appendChild(el('span', 'split-share num', pct(split.annotations / c.annotations, 0) + ' of labels'));
    splitTiles.appendChild(tile);
  });

  setCount('intro-coverage', c.distinct_filenames);
  setChip('kpi-coverage-chip', pct(c.local_image_files / c.distinct_filenames, 0) + ' matched');
  setCount('intro-overlap', audit.overlap.train_val);
  if (valFiles) { setChip('kpi-overlap-chip', pct(audit.overlap.train_val / valFiles, 1) + ' of val'); }
  setCount('intro-sparse', audit.known_sparse_cases.total);
  setChip('kpi-sparse-chip', '\u2264 ' + n(c.low_count_threshold) + ' labels');
  setCount('kpi-alternate', audit.findings.by_type.alternate_annotation_version.count);

  if (valFiles) {
    setChip('ov-leak-chip', pct(audit.overlap.train_val / valFiles, 1), 'down');
    setText('ov-leak-n', n(audit.overlap.train_val));
    setText('ov-leak-d', n(valFiles));
    setBar('ov-leak-bar', audit.overlap.train_val, valFiles,
      n(audit.overlap.train_val) + ' of ' + n(valFiles) + ' validation photos also appear in training');
  }
  setText('ov-leak-note', 'The held-back test split shares ' + n(audit.overlap.train_test + audit.overlap.val_test) +
    ' filenames with the other two; only validation scores are affected.');
  setText('ov-rec-note', 'Reported upstream in issue #21; each one is present in this snapshot with ' +
    '\u2264 ' + n(c.low_count_threshold) + ' supplied labels.');
  setChip('ov-rec-chip', n(audit.known_sparse_cases.recovered) + ' / ' + n(audit.known_sparse_cases.total),
    audit.known_sparse_cases.recovered === audit.known_sparse_cases.total ? 'up' : 'down');
  setText('ov-rec-n', n(audit.known_sparse_cases.recovered));
  setText('ov-rec-d', n(audit.known_sparse_cases.total));
  setBar('ov-rec-bar', audit.known_sparse_cases.recovered, audit.known_sparse_cases.total,
    n(audit.known_sparse_cases.recovered) + ' of ' + n(audit.known_sparse_cases.total) +
    ' reported sparse-label images recovered');

  /* Column chart: reported cases inside the first BUDGETS[0] at the default seed. */
  (function () {
    var budget = BUDGETS[0];
    setText('ov-chart-meta', 'top ' + budget);
    setText('ov-chart-sub', 'The ' + n(known.total) + ' upstream-reported sparse images, placed in each method\u2019s ' +
      'first ' + budget + ' of ' + n(known.pool_size) + ' (seed ' + DEFAULT_SEED + ').');
    var short = { low_count: 'Count', robust: 'Robust', isolation_forest: 'ML', random: 'Random' };
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 260 176');
    svg.setAttribute('role', 'img');
    var described = [];
    var defs = document.createElementNS(ns, 'defs');
    defs.innerHTML = '<pattern id="ov-hatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">' +
      '<rect width="6" height="6" class="hatch-bg"/><line x1="0" y1="0" x2="0" y2="6" class="hatch-line"/></pattern>';
    svg.appendChild(defs);
    var top = 22, bottom = 148, height = bottom - top;
    [0, Math.round(known.total / 2), known.total].forEach(function (tick) {
      var y = bottom - (tick / known.total) * height;
      var line = document.createElementNS(ns, 'line');
      line.setAttribute('x1', '22'); line.setAttribute('x2', '258');
      line.setAttribute('y1', y); line.setAttribute('y2', y);
      line.setAttribute('class', 'chart-grid');
      svg.appendChild(line);
      var label = document.createElementNS(ns, 'text');
      label.setAttribute('x', '0'); label.setAttribute('y', y + 4);
      label.setAttribute('class', 'chart-axis');
      label.textContent = String(tick);
      svg.appendChild(label);
    });
    METHOD_ORDER.forEach(function (method, position) {
      var value = severityAtSeed(method, DEFAULT_SEED, budget) || 0;
      described.push(METHOD_LABELS[method] + ' ' + value + ' of ' + known.total);
      var x = 38 + position * 56, w = 34;
      var track = document.createElementNS(ns, 'rect');
      track.setAttribute('x', x); track.setAttribute('y', top);
      track.setAttribute('width', w); track.setAttribute('height', height);
      track.setAttribute('rx', '8'); track.setAttribute('fill', 'url(#ov-hatch)');
      svg.appendChild(track);
      var barHeight = (value / known.total) * height;
      if (barHeight > 0) {
        var bar = document.createElementNS(ns, 'rect');
        bar.setAttribute('x', x); bar.setAttribute('y', bottom - barHeight);
        bar.setAttribute('width', w); bar.setAttribute('height', barHeight);
        bar.setAttribute('rx', '8');
        bar.setAttribute('class', 'chart-bar' + (method === 'low_count' ? ' is-best' : ''));
        bar.style.setProperty('--h', String(barHeight));
        svg.appendChild(bar);
      }
      var valueLabel = document.createElementNS(ns, 'text');
      valueLabel.setAttribute('x', x + w / 2); valueLabel.setAttribute('y', Math.min(bottom - barHeight, bottom) - 6);
      valueLabel.setAttribute('text-anchor', 'middle'); valueLabel.setAttribute('class', 'chart-value');
      valueLabel.textContent = String(value);
      if (barHeight >= height - 2) { valueLabel.setAttribute('y', top + 16); valueLabel.setAttribute('class', 'chart-value on-bar'); }
      svg.appendChild(valueLabel);
      var name = document.createElementNS(ns, 'text');
      name.setAttribute('x', x + w / 2); name.setAttribute('y', '168');
      name.setAttribute('text-anchor', 'middle'); name.setAttribute('class', 'chart-axis');
      name.textContent = short[method];
      svg.appendChild(name);
    });
    svg.setAttribute('aria-label', 'Reported cases inside the top ' + budget + ': ' + described.join(', ') + '.');
    var chart = $('ov-chart');
    clear(chart);
    chart.appendChild(svg);
  })();

  /* Flagged images: the audit's findings among the illustrated examples. */
  (function () {
    var table = $('ov-flagged');
    var caption = table.querySelector('caption');
    clear(table);
    table.appendChild(caption);
    var flagged = EXAMPLES.filter(isFlagged).sort(function (a, b) {
      if (a.is_known_case !== b.is_known_case) { return a.is_known_case ? -1 : 1; }
      if (a.n_annotations !== b.n_annotations) { return a.n_annotations - b.n_annotations; }
      return a.file_name < b.file_name ? -1 : 1;
    });
    var head = el('thead');
    var headRow = el('tr');
    ['Image', 'Cell line', 'Split', 'Labels', 'Finding', ''].forEach(function (label, position) {
      var th = el('th', position === 3 ? 'num' : null, label);
      th.scope = 'col';
      if (!label) { th.appendChild(el('span', 'sr-only', 'Open')); }
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el('tbody');
    flagged.slice(0, 6).forEach(function (example) {
      var row = el('tr');
      row.appendChild(el('td', 'id', example.file_name));
      row.appendChild(el('td', null, example.cell_line));
      row.appendChild(el('td', null, example.split));
      row.appendChild(el('td', 'num amt', n(example.n_annotations)));
      var finding = el('td');
      finding.appendChild(el('span', 'status ' + (example.finding_type === 'alternate_annotation_version' ? 'neutral' : 'warn'),
        example.finding_label));
      row.appendChild(finding);
      var open = el('td', 'open');
      var button = el('button', 'btn btn-small btn-secondary', 'View');
      button.type = 'button';
      button.setAttribute('aria-label', 'View ' + example.file_name + ' in the image review');
      button.addEventListener('click', function () { openExample(example.id); });
      open.appendChild(button);
      row.appendChild(open);
      body.appendChild(row);
    });
    table.appendChild(body);
    var foot = el('tfoot');
    var footRow = el('tr');
    var footCell = el('td', 'muted', 'Showing ' + n(Math.min(6, flagged.length)) + ' of ' + n(flagged.length) +
      ' flagged illustrated images · ' + n(audit.findings.total) + ' audit findings in total');
    footCell.colSpan = 6;
    footRow.appendChild(footCell);
    foot.appendChild(footRow);
    table.appendChild(foot);
  })();
  function openExample(id) {
    browserState.query = ''; browserState.finding = ''; browserState.cellLine = ''; browserState.split = '';
    $('browse-search').value = ''; findingSelect.value = ''; cellLineSelect.value = ''; $('browse-split').value = '';
    var rows = filtered();
    var position = rows.findIndex(function (row) { return row.id === id; });
    if (position < 0) { return; }
    browserState.selected = id;
    browserState.page = Math.floor(position / PER_PAGE) + 1;
    renderBrowse();
    selectTab($('tab-try-it'));
  }

  function kpiTiles(target, tiles) {
    clear(target);
    tiles.forEach(function (tile, position) {
      var node = el('div', 'tile' + (position === 0 ? ' hero' : ''));
      var top = el('div', 'top', tile.label);
      node.appendChild(top);
      var value = el('p', 'kpi-val num');
      var figure = el('span', null, tile.text);
      if (typeof tile.count === 'number') {
        figure.dataset.count = String(tile.count);
        if (tile.decimals) { figure.dataset.decimals = String(tile.decimals); }
        if (tile.suffix) { figure.dataset.suffix = tile.suffix; }
      }
      value.appendChild(figure);
      node.appendChild(value);
      var line = el('p', 'chip-line');
      line.appendChild(el('span', 'chip' + (tile.tone ? ' ' + tile.tone : ''), tile.chip));
      line.appendChild(document.createTextNode(' '));
      line.appendChild(el('span', 'muted', tile.note));
      node.appendChild(line);
      target.appendChild(node);
    });
  }
  var lowTop = severityRange('low_count', BUDGETS[0]);
  var forestTop = severityRange('isolation_forest', BUDGETS[0]);
  var lowAuc = synthetic.seed_summary.low_count.synthetic_roc_auc.mean;
  var forestAuc = synthetic.seed_summary.isolation_forest.synthetic_roc_auc.mean;
  var randomAuc = synthetic.seed_summary.random.synthetic_roc_auc.mean;
  kpiTiles($('methods-kpis'), [
    { label: 'Count rule · reported cases', text: rangeText(lowTop) + ' / ' + n(known.total),
      chip: 'top ' + BUDGETS[0], note: 'every seed' },
    { label: 'Isolation Forest (ML) · reported cases', text: rangeText(forestTop) + ' / ' + n(known.total),
      chip: '\u2193 ' + rangeText({ min: lowTop.min - forestTop.max, max: lowTop.max - forestTop.min }) + ' vs count', tone: 'down',
      note: 'top ' + BUDGETS[0] },
    { label: 'Count rule · simulated AUC', text: dec(lowAuc, 3), count: lowAuc, decimals: 3,
      chip: 'simulation', tone: 'neutral', note: 'mean of 3 seeds' },
    { label: 'Isolation Forest · simulated AUC', text: dec(forestAuc, 3), count: forestAuc, decimals: 3,
      chip: '\u2193 ' + dec(lowAuc - forestAuc, 3), tone: 'down', note: 'random ' + dec(randomAuc, 3) }
  ]);
  methodBars($('compare-auc'), METHOD_ORDER.map(function (method) {
    var auc = synthetic.seed_summary[method].synthetic_roc_auc.mean;
    return { method: method, share: auc, marker: 0.5, text: dec(auc, 3) };
  }), 'Simulated ROC AUC per method, mean over seeds; the marker shows chance at 0.5');

  kpiTiles($('data-kpis'), [
    { label: 'Review pool', text: n(SITE.methods.pools.review.size), count: SITE.methods.pools.review.size,
      chip: 'every non-fit image', note: 'ranked by 4 methods' },
    { label: 'Audit findings', text: n(audit.findings.total), count: audit.findings.total,
      chip: n(Object.keys(audit.findings.by_type).length) + ' types', tone: 'neutral', note: 'with evidence' },
    { label: 'Frozen review sample', text: n(SITE.methods.humanReview.records), count: SITE.methods.humanReview.records,
      chip: 'awaiting review', tone: 'neutral', note: 'judgments blank' },
    { label: 'Verified files', text: n(SITE.provenance.files.length), count: SITE.provenance.files.length,
      chip: 'SHA-256', tone: 'up', note: 'one release run' }
  ]);

  /* -------------------------------------------------------------- start */
  renderBrowse();
  syncTabs(true);
})();
