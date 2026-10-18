#!/usr/bin/env bash
#
# tools/run_release.sh — the single reproducible LIVECell release entrypoint.
#
# One command runs the whole chain in order, each stage into a fresh timestamped
# run directory that is never reused:
#
#   audit -> warehouse -> features -> rank (seed 42, budgets 10 25 50)
#         -> evaluate -> site build -> site check -> portable package
#
# NO CONTAINER IS USED OR ASSUMED. Docker, podman and buildah are not available
# on the data server (inspected), so every stage runs directly in the project
# virtualenv inside the existing `run-job` launcher (1536 MiB RAM cap, 1024 MiB
# throttle, 256 MiB swap, 1 CPU, one job at a time, 64 tasks, 30-minute timeout).
# No image is built and none is claimed. See docs/12_release.md.
#
# Usage (on the data server, from the project directory):
#
#   tools/run_release.sh --data-root data/raw --out-root data/runs
#   tools/run_release.sh validate --data-root data/raw --out-root data/runs
#   tools/run_release.sh package --run data/runs/<UTC>-<hash>
#   tools/run_release.sh verify-package --zip <path/to/release.zip>
#   tools/run_release.sh list --out-root data/runs
#
# Run layout: <out-root>/<UTC timestamp>-<input-hash prefix>/ containing
# audit/ warehouse/ features/ ranking/ evaluation/ site-seed/ site/ site-inputs/
# provenance/ records/ logs/ release/ and a COMPLETED marker written last.
# Immutable run IDs: an existing run directory is never overwritten; a failed
# run is left for inspection and a new run is required.
#
# Exit codes: 0 success, 2 usage/preflight/validation error, otherwise the failed
# stage's exit code. Every stage log is kept under <run>/logs/.
#
set -euo pipefail

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd -- "${SCRIPT_PATH}/.." && pwd -P)"
DRIVER="${SCRIPT_PATH}/release_tools.py"

COMMAND=""
COMMAND_SET=0
DATA_ROOT=""
OUT_ROOT=""
RUN_DIR=""
ZIP_PATH=""
PYTHON=""
RUN_JOB=""
LAUNCHER="run-job"
JOB_MODE="per-stage"
SEED=42
BUDGETS="10 25 50"
WITH_PREVIEWS=1
TIFF_IDENTITY=1
PACKAGE=1
MIN_FREE_GB=5
ALLOW_EXTERNAL_OUT_ROOT=0
DRY_RUN=0
FORCE_PACKAGE=0
SHARED_ARGS=()

info() { printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2; }
die() { printf '%s: error: %s\n' "$SCRIPT_NAME" "$*" >&2; exit 2; }

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
  cat <<'EOF'

Options:
  --data-root PATH        raw data root (annotations/ + images/); required for run/validate
  --out-root PATH         output root (default: <repo>/data/runs)
  --repo PATH             repository root (default: this script's parent directory)
  --python PATH           project virtualenv python (default: <repo>/.venv/bin/python)
  --run-job PATH          launcher path (default: $HOME/livecell-server/run-job)
  --launcher MODE         run-job | direct        (default: run-job)
  --job-mode MODE         per-stage | single      (default: per-stage)
  --single-job            shorthand for --job-mode single (assumes the caller used run-job)
  --seed N                ranking seed (default: 42)
  --budgets "A B C"       review budgets (default: "10 25 50")
  --with-previews         render the bounded preview set (default)
  --no-previews           skip the preview stage; the site then declares previews pending
  --tiff-identity         hash every raw image for the run ID (default)
  --no-tiff-identity      skip per-image hashing (COCO hashes + image inventory only)
  --no-package            stop after the verified chain (skip the portable ZIP)
  --min-free-gb N         free-space requirement for the output root (default: 5)
  --allow-external-out-root
                          allow an --out-root outside the repository (relative site
                          provenance paths may then expose private directory names)
  --dry-run               print the planned stages and exit
  --force                 package: overwrite an existing archive for the same run
  --validate-only         alias for the `validate` subcommand
  -h, --help              this help

Subcommands: run (default), validate, package, verify-package, list
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    run|validate|package|verify-package|list)
      [ "$COMMAND_SET" -eq 0 ] || die "more than one subcommand given ('$1')"
      COMMAND="$1"; COMMAND_SET=1; shift ;;
    --validate-only)
      [ "$COMMAND_SET" -eq 0 ] || die "more than one subcommand given"
      COMMAND="validate"; COMMAND_SET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --data-root)        [ "$#" -gt 1 ] || die "--data-root needs a value"; DATA_ROOT="$2"; shift 2 ;;
    --data-root=*)      DATA_ROOT="${1#*=}"; shift ;;
    --out-root)         [ "$#" -gt 1 ] || die "--out-root needs a value"; OUT_ROOT="$2"; shift 2 ;;
    --out-root=*)       OUT_ROOT="${1#*=}"; shift ;;
    --run)              [ "$#" -gt 1 ] || die "--run needs a value"; RUN_DIR="$2"; shift 2 ;;
    --run=*)            RUN_DIR="${1#*=}"; shift ;;
    --zip)              [ "$#" -gt 1 ] || die "--zip needs a value"; ZIP_PATH="$2"; shift 2 ;;
    --zip=*)            ZIP_PATH="${1#*=}"; shift ;;
    --repo)             [ "$#" -gt 1 ] || die "--repo needs a value"; REPO_ROOT="$(cd -- "$2" && pwd -P)"; shift 2 ;;
    --repo=*)           REPO_ROOT="$(cd -- "${1#*=}" && pwd -P)"; shift ;;
    --python)           [ "$#" -gt 1 ] || die "--python needs a value"; PYTHON="$2"; shift 2 ;;
    --python=*)         PYTHON="${1#*=}"; shift ;;
    --run-job)          [ "$#" -gt 1 ] || die "--run-job needs a value"; RUN_JOB="$2"; shift 2 ;;
    --run-job=*)        RUN_JOB="${1#*=}"; shift ;;
    --launcher)         [ "$#" -gt 1 ] || die "--launcher needs a value"; LAUNCHER="$2"; shift 2 ;;
    --launcher=*)       LAUNCHER="${1#*=}"; shift ;;
    --job-mode)         [ "$#" -gt 1 ] || die "--job-mode needs a value"; JOB_MODE="$2"; shift 2 ;;
    --job-mode=*)       JOB_MODE="${1#*=}"; shift ;;
    --single-job)       JOB_MODE="single"; shift ;;
    --seed)             [ "$#" -gt 1 ] || die "--seed needs a value"; SEED="$2"; shift 2 ;;
    --seed=*)           SEED="${1#*=}"; shift ;;
    --budgets)          [ "$#" -gt 1 ] || die "--budgets needs a value"; BUDGETS="$2"; shift 2 ;;
    --budgets=*)        BUDGETS="${1#*=}"; shift ;;
    --with-previews)    WITH_PREVIEWS=1; shift ;;
    --no-previews)      WITH_PREVIEWS=0; shift ;;
    --tiff-identity)    TIFF_IDENTITY=1; shift ;;
    --no-tiff-identity) TIFF_IDENTITY=0; shift ;;
    --no-package)       PACKAGE=0; shift ;;
    --package)          PACKAGE=1; shift ;;
    --min-free-gb)      [ "$#" -gt 1 ] || die "--min-free-gb needs a value"; MIN_FREE_GB="$2"; shift 2 ;;
    --min-free-gb=*)    MIN_FREE_GB="${1#*=}"; shift ;;
    --allow-external-out-root) ALLOW_EXTERNAL_OUT_ROOT=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --force)            FORCE_PACKAGE=1; shift ;;
    *) die "unknown argument '$1' (see --help)" ;;
  esac
done

[ -n "$COMMAND" ] || COMMAND="run"
case "$JOB_MODE" in per-stage|single) ;; *) die "--job-mode must be per-stage or single" ;; esac
case "$LAUNCHER" in run-job|direct) ;; *) die "--launcher must be run-job or direct" ;; esac

[ -n "$PYTHON" ] || PYTHON="${REPO_ROOT}/.venv/bin/python"
[ -n "$RUN_JOB" ] || RUN_JOB="${HOME}/livecell-server/run-job"
[ -n "$OUT_ROOT" ] || OUT_ROOT="${REPO_ROOT}/data/runs"
[ "$LAUNCHER" = "direct" ] && JOB_MODE="single"

[ -f "$DRIVER" ] || die "release driver not found at $DRIVER"

# Run one driver command, streaming it to the console and into a log file.
# Every Python invocation on the data server is expected to run inside the
# launcher, which applies the resource caps. `direct`/single keeps the caller's
# own launcher job as the only wrapper: a nested run-job call would deadlock on
# the launcher lock.
run_driver() {
  local logfile="$1"; shift
  local rc=0
  mkdir -p "$(dirname -- "$logfile")"
  if [ "$LAUNCHER" = "direct" ] || [ "$JOB_MODE" = "single" ]; then
    "$@" 2>&1 | tee -a "$logfile" || rc=${PIPESTATUS[0]}
  else
    "$RUN_JOB" "$@" 2>&1 | tee -a "$logfile" || rc=${PIPESTATUS[0]}
  fi
  return "$rc"
}

build_shared_args() {
  SHARED_ARGS=(
    --repo "$REPO_ROOT"
    --data-root "$DATA_ROOT"
    --out-root "$OUT_ROOT"
    --python "$PYTHON"
    --seed "$SEED"
    --budgets $BUDGETS
    --launcher "$LAUNCHER"
  )
  if [ "$WITH_PREVIEWS" -eq 1 ]; then
    SHARED_ARGS+=(--with-previews)
  else
    SHARED_ARGS+=(--no-previews)
  fi
  [ "$TIFF_IDENTITY" -eq 1 ] && SHARED_ARGS+=(--tiff-identity)
  return 0
}

require_data_root() {
  [ -n "$DATA_ROOT" ] || die "--data-root is required for '$COMMAND'"
  [ -d "$DATA_ROOT" ] || die "data root does not exist: $DATA_ROOT"
  DATA_ROOT="$(cd -- "$DATA_ROOT" && pwd -P)"
  local split
  for split in train val test; do
    [ -f "${DATA_ROOT}/annotations/livecell_coco_${split}.json" ] \
      || die "raw annotation file missing: ${DATA_ROOT}/annotations/livecell_coco_${split}.json"
  done
  [ -d "${DATA_ROOT}/images" ] || die "raw images directory missing: ${DATA_ROOT}/images"
}

stage_list() {
  local names="audit warehouse features ranking evaluation"
  # The provisional site build exists only to feed the bounded preview render.
  [ "$WITH_PREVIEWS" -eq 1 ] && names="$names site-seed previews"
  printf '%s site site-check' "$names"
}

# --------------------------------------------------------------------------- #
# preflight (no stage is started and no heavy hashing happens here)
# --------------------------------------------------------------------------- #

require_runtime() {
  [ -x "$PYTHON" ] || die "project virtualenv python is not executable: ${PYTHON}
  create it once with:  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"
  if [ "$LAUNCHER" = "run-job" ]; then
    [ -x "$RUN_JOB" ] || die "launcher not found or not executable: ${RUN_JOB}
  pass --run-job <path>, or --launcher direct to run without the resource caps (only when
  the caller already wrapped this script in run-job)."
  fi
  if [ -n "${INVOCATION_ID:-}" ] && [ "$JOB_MODE" != "single" ]; then
    die "this already looks like a launcher job (INVOCATION_ID is set): nested run-job calls
  are refused by the launcher lock. Rerun inside the job with --single-job, or start this
  entrypoint from a plain shell so each stage gets its own capped job."
  fi
  if ! command -v node >/dev/null 2>&1; then
    info "note: node was not found. tools/check_review_site.py records the JavaScript
  syntax check as SKIPPED-missing-node; the site-check stage still passes. Install node
  to restore node --check coverage for assets/review.js and the data globals."
  fi
}

require_output_root() {
  case "$OUT_ROOT" in
    "${DATA_ROOT}"|"${DATA_ROOT}"/*) die "--out-root must not be inside --data-root" ;;
  esac
  if [ ! -d "$OUT_ROOT" ]; then
    mkdir -p "$OUT_ROOT" || die "cannot create --out-root: $OUT_ROOT"
  fi
  OUT_ROOT="$(cd -- "$OUT_ROOT" && pwd -P)"
  [ -w "$OUT_ROOT" ] || die "--out-root is not writable: $OUT_ROOT"

  local free_kb=""
  free_kb="$(df -Pk -- "$OUT_ROOT" 2>/dev/null | awk 'NR == 2 { print $4 }')" || free_kb=""
  if [ -n "$free_kb" ] && [ "$free_kb" -lt $((MIN_FREE_GB * 1024 * 1024)) ]; then
    die "only $((free_kb / 1024 / 1024)) GiB free under ${OUT_ROOT}; a run needs about
  ${MIN_FREE_GB} GiB (override with --min-free-gb only after checking the disk)"
  fi

  local out_rel="" marker=""
  out_rel="$(realpath --relative-to="$REPO_ROOT" -- "$OUT_ROOT" 2>/dev/null || printf '%s' "$OUT_ROOT")"
  case "$out_rel" in
    ..|../*|/*)
      for marker in /home/ /Users/ saber_s13 firstmate livecell-mvp; do
        if [ "$ALLOW_EXTERNAL_OUT_ROOT" -eq 0 ] && printf '%s' "$OUT_ROOT" | grep -qF -- "$marker"; then
          die "--out-root is outside the repository and contains the private marker '${marker}':
  ${OUT_ROOT}
  The built site records input paths relative to the repository, so an external output root
  can publish private directory names. Use an --out-root inside the project (for example
  data/runs), or pass --allow-external-out-root to accept that risk explicitly."
        fi
      done
      ;;
  esac
}

print_plan() {
  info "command:      $COMMAND"
  info "repository:   $REPO_ROOT"
  info "data root:    ${DATA_ROOT:-<required>}"
  info "output root:  $OUT_ROOT"
  info "python:       $PYTHON"
  info "launcher:     $LAUNCHER (job mode: $JOB_MODE)"
  info "seed/budgets: $SEED / $BUDGETS"
  if [ "$WITH_PREVIEWS" -eq 1 ]; then info "previews:     yes"; else info "previews:     no"; fi
  if [ "$TIFF_IDENTITY" -eq 1 ]; then info "tiff identity: yes (per-image hashes feed the run ID)"; else info "tiff identity: no"; fi
  info "stages:       $(stage_list)"
  info "run id:       <UTC timestamp>-<input-hash prefix>"
}

write_failure() {
  local stage="$1" rc="$2"
  local record="${RUN_DIR}/records/run-failed.json"
  mkdir -p "${RUN_DIR}/records"
  {
    printf '{\n  "status": "failed",\n'
    printf '  "stage": "%s",\n' "$stage"
    printf '  "failed_utc": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "exit_code": %s,\n' "$rc"
    printf '  "log": "%s",\n' "${RUN_DIR}/logs/${stage}.log"
    printf '  "note": "the run directory is preserved for inspection and is never reused"\n}\n'
  } > "$record"
  info "FAILED at stage '${stage}' (exit ${rc})."
  info "  stage log:      ${RUN_DIR}/logs/${stage}.log"
  info "  launcher log:   ${RUN_DIR}/logs/${stage}.launcher.log"
  info "  failure record: ${record}"
  info "Fix the cause and start a NEW run: this run directory is left untouched, and the"
  info "launcher limits must not be raised automatically — investigate first."
  exit 1
}

run_chain() {
  local preflight_log="${OUT_ROOT}/_incoming/preflight.log"
  info "preflight: running the driver's environment/input checks (no stage is started)"
  run_driver "$preflight_log" "$PYTHON" "$DRIVER" preflight "${SHARED_ARGS[@]}" \
    || die "preflight failed; see ${preflight_log}"

  local incoming="${OUT_ROOT}/_incoming"
  mkdir -p "$incoming"
  local stamp fingerprint stage_count stage
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  fingerprint="${incoming}/input-fingerprint-${stamp}.json"
  stage_count="$(stage_list | wc -w)"

  info "fingerprinting the raw inputs before the run ID is minted (hashes the three COCO"
  if [ "$TIFF_IDENTITY" -eq 1 ]; then
    info "  files and every raw image; this is stage 0 of ${stage_count})"
  else
    info "  files and inventories the image set; this is stage 0 of ${stage_count})"
  fi
  local fp_args=("$PYTHON" "$DRIVER" fingerprint "${SHARED_ARGS[@]}" --out "$fingerprint")
  [ "$TIFF_IDENTITY" -eq 1 ] && fp_args+=(--tiff-manifest "${incoming}/tiff-identity-${stamp}.json")
  run_driver "${incoming}/fingerprint-${stamp}.log" "${fp_args[@]}" \
    || die "input fingerprinting failed; the raw data root may be incomplete or unreadable"

  local input_hash run_id
  input_hash="$("$PYTHON" - "$fingerprint" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1]))["input_hash"]["sha256"])
PY
)"
  [ -n "$input_hash" ] || die "could not read the input hash from ${fingerprint}"
  run_id="${stamp}-${input_hash:0:12}"
  RUN_DIR="${OUT_ROOT}/${run_id}"
  if [ -e "$RUN_DIR" ]; then
    die "run directory already exists: ${RUN_DIR}
  Run IDs are immutable and completed outputs are never overwritten. Package the existing
  run instead:  ${SCRIPT_NAME} package --run ${RUN_DIR}"
  fi
  mkdir -p "$RUN_DIR/provenance" "$RUN_DIR/records" "$RUN_DIR/logs"
  mv "$fingerprint" "$RUN_DIR/provenance/input-fingerprint.json"
  if [ -f "${incoming}/tiff-identity-${stamp}.json" ]; then
    mv "${incoming}/tiff-identity-${stamp}.json" "$RUN_DIR/provenance/tiff-identity.json"
  fi
  info "run id:        ${run_id}"
  info "run directory: ${RUN_DIR}"

  run_driver "$RUN_DIR/logs/run-start.launcher.log" \
    "$PYTHON" "$DRIVER" run-start "${SHARED_ARGS[@]}" --run "$RUN_DIR" \
    || write_failure "run-start" $?

  for stage in $(stage_list); do
    info "stage ${stage}: starting (stage log: ${RUN_DIR}/logs/${stage}.log)"
    local rc=0
    run_driver "$RUN_DIR/logs/${stage}.launcher.log" \
      "$PYTHON" "$DRIVER" stage "${SHARED_ARGS[@]}" --run "$RUN_DIR" --stage "$stage" || rc=$?
    [ "$rc" -eq 0 ] || write_failure "$stage" "$rc"
  done

  info "verifying the finished chain (markers, manifests, cross-links, output hashes)"
  local vrc=0
  run_driver "$RUN_DIR/logs/verify-chain.launcher.log" \
    "$PYTHON" "$DRIVER" verify-chain "${SHARED_ARGS[@]}" --run "$RUN_DIR" || vrc=$?
  [ "$vrc" -eq 0 ] || write_failure "verify-chain" "$vrc"

  local crc=0
  run_driver "$RUN_DIR/logs/run-complete.launcher.log" \
    "$PYTHON" "$DRIVER" run-complete "${SHARED_ARGS[@]}" --run "$RUN_DIR" || crc=$?
  [ "$crc" -eq 0 ] || write_failure "run-complete" "$crc"
  info "run complete: ${RUN_DIR}/COMPLETED"

  if [ "$PACKAGE" -eq 1 ]; then
    package_run "$RUN_DIR"
  else
    info "packaging skipped (--no-package); package later with: ${SCRIPT_NAME} package --run ${RUN_DIR}"
  fi
  info "release run finished: ${RUN_DIR}"
}

package_run() {
  local run="$1"
  local out_root
  out_root="$(dirname -- "$run")"
  local log="${run}/logs/package.launcher.log"
  local extra=()
  [ "$FORCE_PACKAGE" -eq 1 ] && extra+=(--force)
  local data_args=()
  [ -n "$DATA_ROOT" ] && data_args=(--data-root "$DATA_ROOT")
  info "packaging the portable release ZIP from ${run}"
  local prc=0
  run_driver "$log" "$PYTHON" "$DRIVER" package \
    --run "$run" --repo "$REPO_ROOT" --out-root "$out_root" --python "$PYTHON" \
    "${data_args[@]}" "${extra[@]}" || prc=$?
  [ "$prc" -eq 0 ] || die "packaging or package verification failed; see ${log}"
  local zip_file
  zip_file="$(ls -1t "${run}/release/"*.zip 2>/dev/null | head -1 || true)"
  [ -n "$zip_file" ] || die "no release archive found under ${run}/release/"
  info "release archive: ${zip_file}"
  if [ -f "${zip_file}.sha256" ]; then
    info "  sha256: $(cut -d' ' -f1 "${zip_file}.sha256")"
  fi
  info "  receipt: $(dirname -- "$run")/$(basename -- "$run")/records/release-receipt.json"
}

# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

case "$COMMAND" in
  run|validate)
    require_data_root
    require_runtime
    require_output_root
    build_shared_args
    print_plan
    if [ "$DRY_RUN" -eq 1 ]; then
      info "dry run: nothing was started"
      exit 0
    fi
    if [ "$COMMAND" = "validate" ]; then
      info "preflight: running the driver's environment/input checks (no stage is started)"
      run_driver "${OUT_ROOT}/_incoming/preflight.log" "$PYTHON" "$DRIVER" preflight "${SHARED_ARGS[@]}" \
        || die "preflight failed"
      info "validation passed: environment and inputs look usable (no stage was started)"
      exit 0
    fi
    run_chain
    ;;
  package)
    require_runtime
    [ -n "$RUN_DIR" ] || die "package needs --run <run directory>"
    [ -d "$RUN_DIR" ] || die "run directory not found: ${RUN_DIR}"
    RUN_DIR="$(cd -- "$RUN_DIR" && pwd -P)"
    OUT_ROOT="$(dirname -- "$RUN_DIR")"
    [ -f "${RUN_DIR}/COMPLETED" ] || die "${RUN_DIR} has no pipeline COMPLETED marker:
  finish and verify the run first (${SCRIPT_NAME} run --data-root ... --out-root ${OUT_ROOT})"
    package_run "$RUN_DIR"
    ;;
  verify-package)
    require_runtime
    [ -n "$ZIP_PATH" ] || die "verify-package needs --zip <archive>"
    run_driver /dev/null "$PYTHON" "$DRIVER" verify-package --zip "$ZIP_PATH" --repo "$REPO_ROOT" \
      --data-root "${DATA_ROOT:-/nonexistent}" || die "archive verification failed"
    ;;
  list)
    require_runtime
    run_driver /dev/null "$PYTHON" "$DRIVER" list --out-root "$OUT_ROOT" || die "listing runs failed"
    ;;
  *) die "unknown command '${COMMAND}'" ;;
esac
