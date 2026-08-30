#!/usr/bin/env bash
# Phase 2 — §17.C — Product / Asset
# 由 orchestrator.sh 调用,带两个参数:mode (--resume|--fresh) + diff_file 路径。
#
# 行为:
#   1. 如果 tasks.yaml status != READY,直接退出 0 — 表示该 phase 还没开工。
#   2. 否则执行本 phase 的实际开发任务(schema 迁移、handler、test 等)。
#   3. 写入 diff_file(${2})— orchestrator 会把它归档到 logs/mvp_dev/。

set -u
REPO_ROOT="/opt/OpenMontage_Voicebox"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] run.sh expects --resume or --fresh as $1" >&2
    exit 2
fi

# 检查 tasks.yaml 是否已填写
status=$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")
if [ "${status}" != "READY" ]; then
    echo "[phase ${0##*/phase_}] STUB — tasks.yaml status=${status}, skipping"
    {
        echo "phase ${0##*/phase_} skipped: status=${status} (not READY)"
        echo "fill scripts/mvp_dev/${0##*/}/tasks.yaml then re-run orchestrator"
    } > "${DIFF_FILE}"
    exit 0
fi

echo "[phase ${0##*/phase_}] running in mode=${MODE}"
{
    echo "=== phase ${0##*/phase_} diff (mode=${MODE}) ==="
    echo "started: $(date -Iseconds)"
    echo "scope: $(grep '^scope:' ${TASKS})"
    echo ""
    echo "[TODO] 替换本行为实际的代码改动逻辑:"
    echo "  - 执行 sql_migrations"
    echo "  - 创建/修改 files_to_create / files_to_modify 列出的 Go 文件"
    echo "  - 跑 go_tests 列出的测试"
} > "${DIFF_FILE}"

# TODO: 实际执行 — 由填好 tasks.yaml 的人实现。
# 这里是占位,exit 0 让 orchestrator 走 gate 检查。
exit 0
