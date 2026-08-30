#!/usr/bin/env bash
# scripts/mvp_dev/orchestrator.sh
# OpenMontage 商品视频 MVP §17 阶段化开发 — 终端离线运行的 cron 入口。
#
# Phase 0 → 5 串行推进,每个 Phase 内部 run.sh + gate.sh。
# --resume (默认):跳过最近已绿的 phase(--fresh / 非绿 / interrupted 都会重跑)。
# --dry-run       :只打印将要做什么,不执行任何 run.sh / gate.sh。
# --only N        :只跑指定 phase(0..5)。
# --from N        :从指定 phase 开始(包含)。
#
# 任何 gate 失败或 phase exit != 0:halt,写 state.interrupted=true,
# crontab 行不删,等下次 cron 启动再处理。
#
# 设计参照 scripts/regression/run_today_fixes.sh 现有 cron 模式。

set -u

REPO_ROOT="/opt/OpenMontage_Voicebox"
LOG_DIR="${REPO_ROOT}/logs/mvp_dev"
STATE_DIR="${LOG_DIR}/state"
ORCH_LOG="${LOG_DIR}/cron-stdout.log"
DATE_TAG="$(date +%Y%m%d-%H%M%S)"
SUMMARY_LOG="${LOG_DIR}/summary-${DATE_TAG}.log"

# Phase 之间间隔(秒) — 给 DB 迁移 flush + 上次跑的进程清理。
PHASE_GAP="${MVP_DEV_PHASE_GAP:-300}"

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

MODE="resume"
ONLY_PHASE=""
FROM_PHASE=0
PARALLEL=0
while [ $# -gt 0 ]; do
    arg="$1"
    case "${arg}" in
        --resume) MODE="resume"; shift ;;
        --fresh)  MODE="fresh";  shift ;;
        --dry-run) MODE="dry-run"; shift ;;
        --only)   ONLY_PHASE="${2:-}"; shift 2 ;;
        --from)   FROM_PHASE="${2:-0}"; shift 2 ;;
        --parallel) PARALLEL=1; shift ;;
        -h|--help)
            cat <<USAGE
usage: $0 [--resume|--fresh|--dry-run] [--only N] [--from N] [--parallel]
  --resume    skip phases whose last run was green + not interrupted + finished <24h ago
  --fresh     re-run every phase from scratch (ignore state files)
  --dry-run   print plan only, do not execute any run.sh or gate.sh
  --only N    run only phase N (0..5), then exit
  --from N    start from phase N (inclusive)
  --parallel  run all READY phases concurrently (background processes, wait at end).
              Useful when phases are independent; serial remains the default for
              safety since MVP phases have ordering deps (B needs A, etc.)
USAGE
            exit 0 ;;
        *)
            echo "[FATAL] unknown arg: ${arg}" >&2
            exit 2 ;;
    esac
done

# ---- 防御:repo 不干净直接拒跑(cron 模式可绕过) ----
if [ "${MODE}" != "dry-run" ]; then
    if [ -z "${MVP_DEV_ALLOW_DIRTY:-}" ]; then
        if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
            echo "[FATAL] repo not clean — refusing to run. Stage or stash your changes first." >&2
            echo "       (or set MVP_DEV_ALLOW_DIRTY=1 to override — used by cron_runner.sh)" >&2
            git -C "${REPO_ROOT}" status --porcelain >&2
            exit 2
        fi
    fi
fi

echo "[$(date -Iseconds)] orchestrator start mode=${MODE} from=${FROM_PHASE} only=${ONLY_PHASE:-<all>}" \
    | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"

# ---- State-file helpers ----
state_get () {
    local phase="$1"
    local state_file="${STATE_DIR}/phase_${phase}.json"
    if [ ! -f "${state_file}" ]; then
        echo "missing"
        return
    fi
    # 只读 last_run_exit_code / last_gate_exit_code / interrupted / finished_at
    "${REPO_ROOT}/.venv/bin/python" - "${state_file}" <<'PYEOF' 2>/dev/null || echo "invalid"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(f"{d.get('last_run_exit_code')}|{d.get('last_gate_exit_code')}|{d.get('interrupted')}|{d.get('last_run_finished_at','')}")
except Exception as e:
    print("invalid")
PYEOF
}

state_should_skip () {
    local phase="$1"
    if [ "${MODE}" = "fresh" ] || [ "${MODE}" = "dry-run" ]; then
        echo "no"
        return
    fi
    local s
    s="$(state_get "${phase}")"
    if [ "${s}" = "missing" ] || [ "${s}" = "invalid" ]; then
        echo "no"
        return
    fi
    IFS='|' read -r run_exit gate_exit interrupted finished_at <<<"${s}"
    if [ "${run_exit}" != "0" ] || [ "${gate_exit}" != "0" ] || [ "${interrupted}" = "True" ]; then
        echo "no"
        return
    fi
    # finished_at 必须 < 24h ago 才视为"最近已绿";超过 24h 强制重跑防漂移。
    if [ -n "${finished_at}" ]; then
        local finished_epoch now_epoch
        finished_epoch="$(date -d "${finished_at}" +%s 2>/dev/null || echo 0)"
        now_epoch="$(date +%s)"
        if (( now_epoch - finished_epoch > 86400 )); then
            echo "no"
            return
        fi
    fi
    echo "yes"
}

state_write () {
    local phase="$1" run_exit="$2" gate_exit="$3" interrupted="$4" diff_file="$5"
    local state_file="${STATE_DIR}/phase_${phase}.json"
    "${REPO_ROOT}/.venv/bin/python" - "${state_file}" "${phase}" "${run_exit}" "${gate_exit}" "${interrupted}" "${diff_file}" "${MODE}" <<'PYEOF'
import json, sys, datetime
path, phase, run_exit, gate_exit, interrupted, diff_file, mode = sys.argv[1:8]
now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
# 保留已存在的 files_changed(若 state 已存在)
try:
    prev = json.load(open(path))
except Exception:
    prev = {}
data = {
    "phase": int(phase),
    "last_run_started_at": prev.get("last_run_started_at", now),
    "last_run_finished_at": now,
    "last_run_exit_code": int(run_exit),
    "last_gate_exit_code": int(gate_exit),
    "diff_file": diff_file,
    "interrupted": interrupted == "True",
    "files_changed": prev.get("files_changed", []),
    "mode": mode,
}
json.dump(data, open(path, "w"), indent=2, ensure_ascii=False)
PYEOF
}

# ---- Phase dispatcher ----
run_one_phase () {
    local phase="$1"
    local phase_dir="${REPO_ROOT}/scripts/mvp_dev/phase_${phase}"
    local run_sh="${phase_dir}/run.sh"
    local gate_sh="${phase_dir}/gate.sh"
    local tasks_yaml="${phase_dir}/tasks.yaml"

    if [ ! -f "${run_sh}" ] || [ ! -f "${gate_sh}" ] || [ ! -f "${tasks_yaml}" ]; then
        echo "[FATAL] phase ${phase}: missing run.sh / gate.sh / tasks.yaml under ${phase_dir}" >&2
        return 2
    fi

    local diff_file="${LOG_DIR}/diff-phase_${phase}-${DATE_TAG}.txt"
    : > "${diff_file}"

    # dry-run:只打印计划,不执行
    if [ "${MODE}" = "dry-run" ]; then
        echo "  [dry-run] would execute: ${run_sh} --${MODE}"
        echo "  [dry-run] would gate:     ${gate_sh}"
        echo "  [dry-run] diff_file:      ${diff_file}"
        return 0
    fi

    # tasks.yaml 还没填的话,run.sh 应当 self-refuse(看 phase_0/run.sh 模板)
    echo "[$(date -Iseconds)] phase ${phase}: run start" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"

    local run_exit=0 gate_exit=0
    bash "${run_sh}" "--${MODE}" "${diff_file}" 2>&1 | tee -a "${SUMMARY_LOG}"
    run_exit="${PIPESTATUS[0]}"

    if [ "${run_exit}" -ne 0 ]; then
        echo "[$(date -Iseconds)] phase ${phase}: run FAILED exit=${run_exit}" \
            | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
        state_write "${phase}" "${run_exit}" 0 false "${diff_file}"
        return "${run_exit}"
    fi

    echo "[$(date -Iseconds)] phase ${phase}: gate start" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    bash "${gate_sh}" 2>&1 | tee -a "${SUMMARY_LOG}"
    gate_exit="${PIPESTATUS[0]}"

    if [ "${gate_exit}" -ne 0 ]; then
        echo "[$(date -Iseconds)] phase ${phase}: gate FAILED exit=${gate_exit}" \
            | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
        state_write "${phase}" 0 "${gate_exit}" false "${diff_file}"
        return "${gate_exit}"
    fi

    state_write "${phase}" 0 0 false "${diff_file}"
    echo "[$(date -Iseconds)] phase ${phase}: GREEN" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    return 0
}

# ---- Auto-commit helper (cron mode only) ----
auto_commit_phase () {
    local phase="$1"
    local phase_label="$2"
    if [ -z "${MVP_DEV_ALLOW_DIRTY:-}" ]; then
        return 0  # interactive mode: don't auto-commit
    fi
    # Only commit if there's something to commit
    if [[ -z "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
        return 0
    fi
    git -C "${REPO_ROOT}" add -A
    local commit_msg="mvp_dev: ${phase_label} phase ${phase} green at $(date -Iseconds)"
    if git -C "${REPO_ROOT}" commit -m "${commit_msg}" --no-verify 2>>"${ORCH_LOG}" >>"${ORCH_LOG}"; then
        local sha
        sha="$(git -C "${REPO_ROOT}" log -1 --format=%h 2>/dev/null)"
        echo "[$(date -Iseconds)] phase ${phase}: auto-commit ${sha}" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    else
        echo "[$(date -Iseconds)] phase ${phase}: auto-commit FAILED (continuing)" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    fi
}

# ---- Main loop ----
PHASES=("0" "1" "2" "3" "4" "5")
if [ -n "${ONLY_PHASE}" ]; then
    PHASES=("${ONLY_PHASE}")
fi

if [ "${PARALLEL}" = "1" ] && [ "${MODE}" != "dry-run" ]; then
    # ---- 并行模式:所有 READY phase 同时拉起 ----
    echo "[$(date -Iseconds)] orchestrator PARALLEL mode — launching all READY phases concurrently" \
        | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    declare -A PIDS
    launched=0
    for phase in "${PHASES[@]}"; do
        if [ "${phase}" -lt "${FROM_PHASE}" ]; then continue; fi
        skip="$(state_should_skip "${phase}")"
        if [ "${skip}" = "yes" ]; then
            echo "[$(date -Iseconds)] phase ${phase}: skip (recently green)" \
                | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
            continue
        fi
        # 后台跑 run.sh + gate.sh,记 PID
        (
            diff_file="${LOG_DIR}/diff-phase_${phase}-${DATE_TAG}.txt"
            : > "${diff_file}"
            bash "${REPO_ROOT}/scripts/mvp_dev/phase_${phase}/run.sh" "--${MODE}" "${diff_file}" \
                > "${LOG_DIR}/parallel-phase_${phase}-${DATE_TAG}.log" 2>&1
            r=$?
            if [ $r -ne 0 ]; then
                echo "run_exit=${r}" > "${LOG_DIR}/parallel-phase_${phase}-${DATE_TAG}.exit"
                exit $r
            fi
            bash "${REPO_ROOT}/scripts/mvp_dev/phase_${phase}/gate.sh" \
                >> "${LOG_DIR}/parallel-phase_${phase}-${DATE_TAG}.log" 2>&1
            g=$?
            if [ $g -ne 0 ]; then
                echo "gate_exit=${g}" > "${LOG_DIR}/parallel-phase_${phase}-${DATE_TAG}.exit"
                exit $g
            fi
            echo "green" > "${LOG_DIR}/parallel-phase_${phase}-${DATE_TAG}.exit"
            exit 0
        ) &
        PIDS[$phase]=$!
        launched=$((launched + 1))
        echo "[$(date -Iseconds)] phase ${phase}: launched pid=${PIDS[$phase]}" \
            | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    done

    # 等所有
    overall_exit=0
    for phase in "${!PIDS[@]}"; do
        pid="${PIDS[$phase]}"
        if wait "${pid}"; then
            state_write "${phase}" 0 0 false "${LOG_DIR}/diff-phase_${phase}-${DATE_TAG}.txt"
            echo "[$(date -Iseconds)] phase ${phase}: GREEN" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
            auto_commit_phase "${phase}" "parallel"
        else
            we=$?
            # 读详细 exit 信息
            extra="$(cat "${LOG_DIR}/parallel-phase_${phase}-${DATE_TAG}.exit" 2>/dev/null || echo unknown)"
            echo "[$(date -Iseconds)] phase ${phase}: FAILED (${extra}) wait_exit=${we}" \
                | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
            state_write "${phase}" 0 0 true "${LOG_DIR}/diff-phase_${phase}-${DATE_TAG}.txt"
            overall_exit=1
        fi
    done
    if [ ${overall_exit} -ne 0 ]; then
        echo "[$(date -Iseconds)] orchestrator HALT (parallel)" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
        exit 1
    fi
    echo "[$(date -Iseconds)] orchestrator DONE (parallel)" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
    exit 0
fi

# ---- 串行模式(默认)----
for phase in "${PHASES[@]}"; do
    if [ "${phase}" -lt "${FROM_PHASE}" ]; then
        continue
    fi
    skip="$(state_should_skip "${phase}")"
    if [ "${skip}" = "yes" ] && [ "${MODE}" != "dry-run" ]; then
        echo "[$(date -Iseconds)] phase ${phase}: skip (recently green)" \
            | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
        continue
    fi
    if ! run_one_phase "${phase}"; then
        echo "[$(date -Iseconds)] orchestrator HALT on phase ${phase}" \
            | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
        # 中断也写 state.interrupted=true,留给下次重跑
        state_write "${phase}" 0 0 true "${LOG_DIR}/diff-phase_${phase}-${DATE_TAG}.txt"
        exit 1
    fi
    # 串行模式:auto-commit 由 orchestrator 跑
    auto_commit_phase "${phase}" "serial"
    # phase 之间间隔,给 DB 迁移 flush + 上次跑的进程清理。
    # dry-run 不需要 sleep — 也不应该 sleep,会卡住 cron。
    if [ -z "${ONLY_PHASE}" ] && [ "${phase}" != "${PHASES[-1]}" ] && [ "${MODE}" != "dry-run" ]; then
        echo "[$(date -Iseconds)] sleeping ${PHASE_GAP}s before next phase..." \
            | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
        sleep "${PHASE_GAP}"
    fi
done

echo "[$(date -Iseconds)] orchestrator DONE" | tee -a "${ORCH_LOG}" "${SUMMARY_LOG}"
