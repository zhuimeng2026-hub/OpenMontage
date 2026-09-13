#!/usr/bin/env bash
# scripts/mvp_dev/cron_runner.sh
# 系统 cron 的实际入口 — 自删除 one-shot 包装。
#
# 设计:
#   - 由 `crontab -e` 加一行(脚本注释里写有命令)触发一次
#   - 设 MVP_DEV_ALLOW_DIRTY=1 绕过 repo-dirty 防御(由 orchestrator 内部 auto-commit)
#   - 用 nohup + setsid 让进程脱离任何会话绑定(即使本 Claude 会话退出也不影响)
#   - 跑完后用 crontab -l | grep -v 自己 | crontab -  自删除 cron 行
#   - 全部输出汇总到 /opt/OpenMontage_Voicebox/logs/mvp_dev/cron.log
#
# 用法(在终端先 dry-run):
#   bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/cron_runner.sh --dry-run
# 装机:
#   /opt/OpenMontage_Voicebox/scripts/mvp_dev/install_cron.sh

set -u

REPO_ROOT="/opt/OpenMontage_Voicebox"
RUNNER_SELF="$(cd "$(dirname "$0")" && pwd)/cron_runner.sh"
LOG_DIR="${REPO_ROOT}/logs/mvp_dev"
CRON_LOG="${LOG_DIR}/cron.log"
mkdir -p "${LOG_DIR}"

# 不管父会话是否还活着 — 用 setsid + nohup 切到新 session
# 但 cron_runner 本身就被 cron 启动,已经脱离会话,这里 nohup 是 belt-and-braces
# 真正兜底是:ORCH 内部的 setsid nohup(防止 orchestrator 子进程被信号牵连)

MODE="real"
for arg in "$@"; do
    case "${arg}" in
        --dry-run) MODE="dry-run" ;;
        -h|--help)
            cat <<USAGE
usage: $0 [--dry-run]
  --dry-run  run orchestrator in dry-run mode (no actual code changes)
USAGE
            exit 0 ;;
        *)
            echo "[FATAL] unknown arg: ${arg}" >&2
            exit 2 ;;
    esac
done

{
    echo ""
    echo "============================================================"
    echo "[$(date -Iseconds)] cron_runner start mode=${MODE}"
    echo "pid=$$ ppid=${PPID:-?} sid=$(ps -o sid= -p $$ 2>/dev/null | tr -d ' ')"
    echo "============================================================"
} | tee -a "${CRON_LOG}"

# ---- 跑 orchestrator ----
export MVP_DEV_ALLOW_DIRTY=1
if [ "${MODE}" = "dry-run" ]; then
    "${REPO_ROOT}/scripts/mvp_dev/orchestrator.sh" --dry-run --parallel 2>&1
    rc=$?
else
    "${REPO_ROOT}/scripts/mvp_dev/orchestrator.sh" --resume --parallel 2>&1
    rc=$?
fi

{
    echo "[$(date -Iseconds)] orchestrator exit=${rc}"
} | tee -a "${CRON_LOG}"

# ---- 自删除 cron 行(只删包含 cron_runner.sh 标识的行)----
{
    echo "[$(date -Iseconds)] self-removing cron entry"
} | tee -a "${CRON_LOG}"
( crontab -l 2>/dev/null | grep -v 'mvp_dev/cron_runner.sh' | crontab - ) 2>&1 | tee -a "${CRON_LOG}"

# 退出码透传
exit ${rc}
