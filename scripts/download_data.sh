#!/usr/bin/env bash
# Download the LIVECell dataset (≈2.1 GB) from the public Sartorius S3 bucket.
# Resumable (curl -C -). Safe to re-run. License: CC BY-NC 4.0.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="https://livecell-dataset.s3.eu-central-1.amazonaws.com/LIVECell_dataset_2021"
OUT="$ROOT/data/raw"
mkdir -p "$OUT/annotations"
files=(
  "annotations/LIVECell/livecell_coco_val.json"
  "annotations/LIVECell/livecell_coco_test.json"
  "annotations/LIVECell/livecell_coco_train.json"
  "images.zip"
)
for f in "${files[@]}"; do
  dest="$OUT/$( [[ $f == annotations/* ]] && echo "annotations/$(basename "$f")" || echo "$(basename "$f")" )"
  echo "[$(date +%T)] -> $f"
  curl -fL --retry 5 --retry-delay 5 -C - -o "$dest" "$BASE/$f"
done
echo "[$(date +%T)] Downloads done. Verifying sizes..."
ls -l "$OUT" "$OUT/annotations"
echo "Expected bytes: images.zip=1241715651 train=551131789 val=97272542 test=260794837"
if [[ ! -d "$OUT/images" ]]; then
  echo "[$(date +%T)] Unzipping images.zip ..."
  unzip -q "$OUT/images.zip" -d "$OUT/"
fi
echo "[$(date +%T)] ALL DONE"
