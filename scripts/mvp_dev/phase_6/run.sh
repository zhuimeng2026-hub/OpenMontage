#!/usr/bin/env bash
# Phase 6 — §21 + §22 — 实际预览渲染接入 OpenMontage MCP
#
# 由 orchestrator.sh 调用:bash phase_6/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. ALTER TABLE production_jobs ADD COLUMN artifacts_json(IF NOT EXISTS 兼容)
#   2. 校验 mvpclient + jobsvc + cmd/mvp 改动文件存在(本仓库 committed source)
#   3. go build → /tmp/frameflow-bff-mvp-p6
#   4. 启 stub MCP server(端口 18910,纯 python http.server)+ BFF(:18907)
#   5. 跑 gate.sh
#   6. 收尾关掉 stub + BFF
#
# 与 Phase 0-5 不同:Phase 6 的所有 Go 代码都在 git 里(本次 commit 一并入库),
# run.sh 不再 heredoc 写代码,只做 ALTER + build + 启动 + gate。这样避免
# 重复 ~600 行 Go 代码;若需重做,从 git 重新 checkout 即可。
#
# 设计要点:
#   - 必须 MCP_BASE_URL 指向 stub server 才能跑通 gate(主路径);
#     MCP_BASE_URL 留空 → 503(fail-loud,plan §8.2)。
#   - render 失败由 stub MCP 返回 failed 触发,验证 Refund 路径。
#   - state machine 是异步的;gate 用轮询 status 直到 *_READY / FAILED。

set -u
set -o pipefail
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"

REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_6-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_6 run.sh start $(date -Iseconds) mode=${1:-?} ==="

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_6] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_6 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. 校验代码改动 ----
echo "[phase_6] step 1: verify file presence"
for f in \
    "internal/mvpclient/types.go" \
    "internal/mvpclient/client.go" \
    "internal/mvpclient/poller.go" \
    "internal/jobsvc/runner.go" \
    "internal/jobsvc/store.go" \
    "cmd/mvp/main.go" \
    "cmd/mvp/handlers_project.go" \
    "cmd/mvp/db.go"; do
    if [ ! -f "${f}" ]; then
        echo "[phase_6] FATAL: missing ${f}" >&2
        echo "phase_6 FAIL: missing ${f}" > "${DIFF_FILE}"
        exit 3
    fi
done
# quick sanity check: runner.go imports mvpclient
if ! grep -q 'mvpclient' "internal/jobsvc/runner.go"; then
    echo "[phase_6] FATAL: runner.go does not reference mvpclient — Phase 6 not landed?" >&2
    echo "phase_6 FAIL: runner.go missing mvpclient reference" > "${DIFF_FILE}"
    exit 3
fi
echo "[phase_6] file verify OK"

# ---- 2. schema 迁移 ----
echo "[phase_6] step 2: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
mkdir -p "${BFF}/data"
# SQLite <3.35 ALTER ADD COLUMN 会报 duplicate column;3.35+ 直接支持。
# 我们用 shell try-style:if 语句分两个分支。
add_col_ok=$(sqlite3 "${DB_PATH}" \
    "ALTER TABLE production_jobs ADD COLUMN artifacts_json TEXT DEFAULT NULL;" 2>&1) || true
case "${add_col_ok}" in
    *"duplicate column name"*)
        echo "[phase_6] artifacts_json already exists, skipping"
        ;;
    "")
        echo "[phase_6] artifacts_json added"
        ;;
    *)
        echo "[phase_6] WARN: ${add_col_ok}"
        ;;
esac
echo "[phase_6] schema verify:"
sqlite3 "${DB_PATH}" "PRAGMA table_info(production_jobs);" | grep -E "artifacts_json|external_run_id|om_project_id" | sort -u

# ---- 3. go build ----
echo "[phase_6] step 3: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p6"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ] || [ ! -x "${BIN}" ]; then
    echo "[phase_6] build FAILED exit=${build_exit}"
    echo "phase_6 FAIL: build" > "${DIFF_FILE}"
    exit 4
fi
echo "[phase_6] build OK → ${BIN}"

# ---- 4. 启动 stub MCP + BFF ----
echo "[phase_6] step 4: launch stub MCP server (:18910) and BFF (:18907)"
pkill -f frameflow-bff-mvp-p6 2>/dev/null || true
pkill -f "mcp_stub_server.py" 2>/dev/null || true
sleep 0.5

# stub MCP server — pure python http.server in a background subshell
STUB_PORT=18910
nohup "${PHASE_DIR}/mcp_stub_server.py" "${STUB_PORT}" \
    > "${REPO_ROOT}/logs/mvp_dev/phase_6-stub.log" 2>&1 &
STUB_PID=$!
sleep 0.5

WEIXIN_MOCK_AUTH=1 MVP_PORT=18907 MVP_DB_PATH="${DB_PATH}" \
    MCP_BASE_URL="http://127.0.0.1:${STUB_PORT}/mcp" \
    MCP_API_TOKEN="gate-test-token" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_6-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_6] stub pid=${STUB_PID} server pid=${SERVER_PID}"

# 等 /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18907/healthz" >/dev/null 2>&1; then
        echo "[phase_6] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_6] /healthz never came up" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_6-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    kill ${STUB_PID} 2>/dev/null || true
    exit 5
fi

# ---- 5. 跑 gate ----
echo "[phase_6] step 5: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 6. 收尾 ----
echo "[phase_6] step 6: cleanup"
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true
kill ${STUB_PID} 2>/dev/null || true
wait ${STUB_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_6] gate FAILED exit=${GATE_EXIT}"
    echo "phase_6 FAIL: gate exit=${GATE_EXIT}" > "${DIFF_FILE}"
    exit 1
fi

echo "[phase_6] DONE — gate green, server stopped"
echo "phase_6 green at $(date -Iseconds)" > "${DIFF_FILE}"
exit 0