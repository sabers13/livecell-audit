LIVECell review ranking — download notes
========================================

ranking-pool.csv
  Every row of the 3,067-image review pool with its rank in each method.
  Units: n_annotations is a count; area features are pixels^2; bbox and
  image dimensions are pixels; pixel statistics are 8-bit intensity
  (0-255) used as stored, with no rescaling; time_hours is hours.
  Priority columns are relative review priorities, NOT probabilities
  that a label is wrong.

ranking-summary.json, model-card.json, evaluation-summary.json,
known-cases.json, review-sample.json, audit-findings.json
  The verified source documents this site projects, copied unchanged so a
  reader can check every number. The review sample's judgment fields are
  blank: no scientific reviewer has been supplied and no expert judgment is
  claimed.
