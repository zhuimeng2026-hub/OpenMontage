#!/usr/bin/env bash
# Run today's regression tests for video_compose / scene_detect fixes
# across 3 parallel buckets. Each bucket writes its own log under
# logs/regression/, plus a state file under logs/regression/state/ that
# drives --resume.
#
# Modes:
#   (default / --fresh)  Run every bucket from scratch.
#   --resume              Skip buckets whose last run was a clean green.
#                         Re-run buckets that failed or were interrupted
#                         (so a cron killed by OOM or system reboot can
#                         pick up where it left off, not from zero).
#
# Invoked by cron at 01:00 (see crontab). Designed to survive session exit
# and run without an attached terminal.

set -u  # do not `set -e` — we want every bucket's exit code captured, not a
        # single bucket failure aborting the whole summary.

REPO_ROOT="/opt/OpenMontage_Voicebox"
LOG_DIR="${REPO_ROOT}/logs/regression"
STATE_DIR="${LOG_DIR}/state"
DATE_TAG="$(date +%Y%m%d-%H%M%S)"
SUMMARY_LOG="${LOG_DIR}/summary-${DATE_TAG}.log"

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

# ---- Mode parsing ----
MODE="fresh"
for arg in "$@"; do
    case "${arg}" in
        --resume) MODE="resume" ;;
        --fresh)  MODE="fresh" ;;
        -h|--help)
            echo "usage: $0 [--fresh|--resume]" >&2
            exit 0 ;;
        *)
            echo "unknown arg: ${arg}" >&2
            exit 2 ;;
    esac
done

# Source the venv if it exists, otherwise fall back to system python.
if [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
    PY="${REPO_ROOT}/.venv/bin/python"
    export PATH="${REPO_ROOT}/.venv/bin:${PATH}"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
    PY="${VIRTUAL_ENV}/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

cd "${REPO_ROOT}" || {
    echo "[FATAL] could not cd to ${REPO_ROOT}" >&2
    exit 2
}

echo "[$(date -Iseconds)] orchestrator start mode=${MODE}" | tee "${SUMMARY_LOG}"
echo "repo=${REPO_ROOT} python=${PY}" | tee -a "${SUMMARY_LOG}"

# ---- State-file helpers ----
# State file format (one per bucket):
#   {
#     "last_run_started_at":  ISO-8601,
#     "last_run_finished_at": ISO-8601,
#     "last_run_exit_code":   int,
#     "last_run_log":         path,
#     "interrupted":          bool,
#     "mode":                 "fresh" | "resume"
#   }
# On --resume, a bucket is skipped only when its state file shows
# last_run_exit_code == 0 AND interrupted == false. Anything else
# (no state file, non-zero exit, interrupted) re-runs from scratch.
state_get_exit () {
    local bucket="$1"
    local state_file="${STATE_DIR}/${bucket}.json"
    if [ ! -f "${state_file}" ]; then
        echo "missing"
        return
    fi
    "${PY}" -c "
import json, sys
try:
    s = json.load(open('${state_file}'))
    print(s.get('last_run_exit_code', 'missing'))
except Exception as e:
    print('corrupt')
" 2>/dev/null || echo "corrupt"
}

state_write () {
    local bucket="$1"
    local rc="$2"
    local started_at="$3"
    local finished_at="$4"
    local log_path="$5"
    local interrupted="$6"
    local mode="$7"
    "${PY}" - "${STATE_DIR}/${bucket}.json" <<EOF
import json, sys
state = {
    "last_run_started_at":  "${started_at}",
    "last_run_finished_at": "${finished_at}",
    "last_run_exit_code":   ${rc},
    "last_run_log":         "${log_path}",
    "interrupted":          ${interrupted},
    "mode":                 "${mode}",
}
with open(sys.argv[1], "w") as f:
    json.dump(state, f, indent=2)
EOF
}

# ---- Bucket runner ----
# Args:
#   $1  bucket name      (used in log + state filenames)
#   $2  started-at ISO   (passed through to state)
#   $@  rest are pytest targets + flags
run_bucket () {
    local name="$1"
    local started_at="$2"
    shift 2

    local log="${LOG_DIR}/bucket-${name}-${DATE_TAG}.log"
    local status="${LOG_DIR}/bucket-${name}-${DATE_TAG}.status"
    local state="${STATE_DIR}/${name}.json"

    # --resume: skip green buckets.
    if [ "${MODE}" = "resume" ]; then
        local last_exit
        last_exit="$(state_get_exit "${name}")"
        if [ "${last_exit}" = "0" ]; then
            echo "[$(date -Iseconds)] bucket=${name} SKIP (last run was green; state=${state})" | tee -a "${SUMMARY_LOG}"
            echo "skipped" > "${status}"
            return 0
        fi
        echo "[$(date -Iseconds)] bucket=${name} RESUME (last exit=${last_exit})" | tee -a "${SUMMARY_LOG}"
    else
        echo "[$(date -Iseconds)] bucket=${name} start log=${log}" | tee -a "${SUMMARY_LOG}"
    fi

    # Write an "in_progress" state BEFORE running so that, if we get killed
    # mid-bucket, the next --resume knows we didn't finish.
    state_write "${name}" "-1" "${started_at}" "" "${log}" "true" "${MODE}"

    # Wrap pytest so its exit code is captured separately from tee. The
    # `( ... )` subshell isolates the trap; SIGTERM/SIGINT mark the state
    # file as interrupted.
    (
        # trap -- ignore SIGTERM/SIGINT in the subshell so the orchestrator's
        # trap handler can still aggregate exit codes; we mark interrupted
        # via the exit code (pytest uses 2 for interrupted, 130 for SIGINT).
        "${PY}" -m pytest -v --tb=short "$@" 2>&1
        echo $? > "${status}"
    ) | tee "${log}"

    local rc
    rc="$(cat "${status}" 2>/dev/null || echo "?")"
    local finished_at
    finished_at="$(date -Iseconds)"
    local interrupted="false"
    if [[ "${rc}" =~ ^[0-9]+$ ]] && [ "${rc}" -ge 2 ]; then
        # pytest exit codes: 0 pass, 1 failures, 2 interrupted, 3 internal,
        # 4 pytest error, 5 no tests collected. Treat 2+ as "interrupted".
        interrupted="true"
    fi

    state_write "${name}" "${rc}" "${started_at}" "${finished_at}" "${log}" "${interrupted}" "${MODE}"

    echo "[$(date -Iseconds)] bucket=${name} exit=${rc} interrupted=${interrupted} log=${log}" >> "${SUMMARY_LOG}"
}

# Orchestrator-level trap: if the script itself is killed (SIGINT/SIGTERM),
# we cannot recover the per-bucket state files because they are written
# inside the subshell that just got killed. The per-bucket state files we
# wrote at "in_progress" stay on disk with last_run_exit_code=-1 and
# interrupted=true, so --resume will correctly re-run them.
trap 'echo "[$(date -Iseconds)] orchestrator interrupted (SIGINT/SIGTERM)" >> "${SUMMARY_LOG}"' INT TERM

# ---- Bucket 1: video_compose fixes (compose_target cascade + from=NaN) ----
# Today: eedf74b fix(video_compose): respect edit_decisions.compose_target
#        9266752 fix(video_compose): from=NaN 全链路
run_bucket "1-video-compose" "$(date -Iseconds)" \
    tests/regression/test_resolve_fps_cascade.py \
    tests/regression/test_resolve_compose_target_cascade.py \
    tests/test_custom_composition_contract.py \
    tests/tools/test_video_compose_vertical.py \
    tests/test_video_compose_node_path.py \
    tests/test_video_compose_remotion_progress.py &

# ---- Bucket 2: scene_detect (tool + MCP exposure + template-remix robustness) ----
# Today: b061a71 feat(mcp): expose scene_detect via MCP
#        47f363f feat: add template remix pipeline and robust scene detection
#        b53df7c test: add local template remix verification script
run_bucket "2-scene-detect" "$(date -Iseconds)" \
    tests/regression/test_mcp_scene_detect_wrapper.py \
    tests/tools/test_scene_detect_long_video.py \
    tests/tools/test_scene_detect_lavfi_escape.py \
    tests/contracts/test_phase2_contracts.py &

# ---- Bucket 3: pipeline + governance contracts (cross-cutting smoke) ----
# Catches contract drift introduced by today's edits to pipeline manifests,
# tool contracts, and selection routing. Pre-existing failures (see tasks
# #8, #9) live here and keep nightly exit=1 until they're fixed — that's
# intentional, not noise.
run_bucket "3-contracts" "$(date -Iseconds)" \
    tests/contracts/test_phase1_contracts.py \
    tests/contracts/test_phase3_contracts.py \
    tests/contracts/test_runtime_presentation_contract.py \
    tests/contracts/test_phase2_comparison.py &

# Wait for all 3 buckets. `wait` with no args waits for every background
# child; we already tee'd the exit codes into per-bucket status files so
# we don't need $? from wait itself.
wait

echo "[$(date -Iseconds)] all buckets finished" >> "${SUMMARY_LOG}"

# ---- Summary ----
total_pass=0
total_skip=0
total_fail=0
overall=0
for bucket in 1-video-compose 2-scene-detect 3-contracts; do
    status_file="${LOG_DIR}/bucket-${bucket}-${DATE_TAG}.status"
    rc="$(cat "${status_file}" 2>/dev/null || echo "?")"
    if [ "${rc}" = "skipped" ]; then
        total_skip=$((total_skip + 1))
        echo "bucket=${bucket} SKIPPED (resumed from prior green state)" | tee -a "${SUMMARY_LOG}"
        continue
    fi
    echo "bucket=${bucket} exit=${rc}" | tee -a "${SUMMARY_LOG}"
    if [[ "${rc}" =~ ^[0-9]+$ ]]; then
        if [ "${rc}" -eq 0 ]; then
            total_pass=$((total_pass + 1))
        else
            total_fail=$((total_fail + 1))
            overall=1
        fi
    else
        total_fail=$((total_fail + 1))
        overall=1
    fi
done

# Count actual test pass/fail from per-bucket logs (pytest -v output).
# Skipped buckets have no fresh log for this run; report their last-run
# counts from state instead.
for log in "${LOG_DIR}"/bucket-*-"${DATE_TAG}".log; do
    [ -f "${log}" ] || continue
    pass_count="$(grep -cE '^tests/.* PASSED' "${log}" 2>/dev/null || echo 0)"
    fail_count="$(grep -cE '^tests/.* FAILED' "${log}" 2>/dev/null || echo 0)"
    echo "  $(basename "${log}"): passed=${pass_count} failed=${fail_count}" >> "${SUMMARY_LOG}"
done

echo "result: buckets_ok=${total_pass} buckets_skipped=${total_skip} buckets_failed=${total_fail} overall_exit=${overall}" | tee -a "${SUMMARY_LOG}"
echo "[$(date -Iseconds)] orchestrator done" >> "${SUMMARY_LOG}"

# Rotate: keep the last 30 summary logs, drop older.
find "${LOG_DIR}" -name 'summary-*.log' -mtime +30 -delete 2>/dev/null
find "${LOG_DIR}" -name 'bucket-*.log' -mtime +30 -delete 2>/dev/null
find "${LOG_DIR}" -name 'bucket-*.status' -mtime +30 -delete 2>/dev/null

exit "${overall}"