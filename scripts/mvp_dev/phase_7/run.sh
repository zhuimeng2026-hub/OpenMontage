#!/usr/bin/env bash
# Phase 7 — §17.D — WAITING_APPROVAL + approve 端点
#
# 由 orchestrator.sh 调用:bash phase_7/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. ALTER TABLE video_projects ADD COLUMN approved_by / approved_at
#   2. 校验 jobsvc/states.go 加 approve 触发器
#   3. 校验 handlers_project.go Approve handler 存在
#   4. 校验 main.go 挂 /api/video-projects/:id/approve 路由
#   5. go build → /tmp/frameflow-bff-mvp-p7
#   6. 启 stub MCP server(--succeed-render flag 让 render 成功)+ BFF(:18907)
#   7. 跑 gate.sh
#
# 与 Phase 6 同样的简洁模式:代码全在 git 里,run.sh 只做 ALTER + build +
# 启动 + gate。

set -u
set -o pipefail
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"

REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_7-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_7 run.sh start $(date -Iseconds) mode=${1:-?} ==="

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_7] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_7 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. 校验代码改动 ----
echo "[phase_7] step 1: verify file presence"
for f in \
    "internal/jobsvc/states.go" \
    "internal/jobsvc/types.go" \
    "cmd/mvp/main.go" \
    "cmd/mvp/handlers_project.go" \
    "cmd/mvp/db.go"; do
    if [ ! -f "${f}" ]; then
        echo "[phase_7] FATAL: missing ${f}" >&2
        echo "phase_7 FAIL: missing ${f}" > "${DIFF_FILE}"
        exit 3
    fi
done
# Sanity: states.go 加了 approve 触发器
if ! grep -q 'case "approve"' "internal/jobsvc/states.go"; then
    echo "[phase_7] FATAL: states.go missing approve trigger — Phase 7 not landed?" >&2
    echo "phase_7 FAIL: states.go missing approve trigger" > "${DIFF_FILE}"
    exit 3
fi
# Sanity: handlers_project.go 有 Approve handler
if ! grep -q 'func (h \*ProjectHandler) Approve' "cmd/mvp/handlers_project.go"; then
    echo "[phase_7] FATAL: handlers_project.go missing Approve handler" >&2
    echo "phase_7 FAIL: Approve handler missing" > "${DIFF_FILE}"
    exit 3
fi
# Sanity: main.go 挂了 /approve 路由
if ! grep -q '"/video-projects/:id/approve"' "cmd/mvp/main.go"; then
    echo "[phase_7] FATAL: main.go missing /approve route" >&2
    echo "phase_7 FAIL: /approve route missing" > "${DIFF_FILE}"
    exit 3
fi
echo "[phase_7] file verify OK"

# ---- 2. schema 迁移 ----
echo "[phase_7] step 2: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
mkdir -p "${BFF}/data"
for col in approved_by approved_at; do
    msg=$(sqlite3 "${DB_PATH}" \
        "ALTER TABLE video_projects ADD COLUMN ${col} TEXT DEFAULT NULL;" 2>&1) || true
    case "${msg}" in
        *"duplicate column"*) echo "[phase_7] ${col} already exists, skipping" ;;
        "")                   echo "[phase_7] ${col} added" ;;
        *)                    echo "[phase_7] WARN(${col}): ${msg}" ;;
    esac
done
echo "[phase_7] schema verify:"
sqlite3 "${DB_PATH}" "PRAGMA table_info(video_projects);" | grep -E "approved_by|approved_at" | sort -u

# ---- 3. go build ----
echo "[phase_7] step 3: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p7"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ] || [ ! -x "${BIN}" ]; then
    echo "[phase_7] build FAILED exit=${build_exit}"
    echo "phase_7 FAIL: build" > "${DIFF_FILE}"
    exit 4
fi
echo "[phase_7] build OK → ${BIN}"

# ---- 4. 启 stub MCP + BFF ----
echo "[phase_7] step 4: launch stub MCP (:18911 --succeed-render) and BFF (:18907)"
pkill -f frameflow-bff-mvp-p7 2>/dev/null || true
pkill -f "phase_7/mcp_stub_server.py" 2>/dev/null || true
sleep 0.5

STUB_PORT=18911  # Phase 7 用独立端口避免与 Phase 6 stub 冲突
nohup "${PHASE_DIR}/mcp_stub_server.py" "${STUB_PORT}" --succeed-render \
    > "${REPO_ROOT}/logs/mvp_dev/phase_7-stub.log" 2>&1 &
STUB_PID=$!
sleep 0.5

WEIXIN_MOCK_AUTH=1 MVP_PORT=18907 MVP_DB_PATH="${DB_PATH}" \
    MCP_BASE_URL="http://127.0.0.1:${STUB_PORT}/mcp" \
    MCP_API_TOKEN="gate-test-token" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_7-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_7] stub pid=${STUB_PID} server pid=${SERVER_PID}"

# 等 /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18907/healthz" >/dev/null 2>&1; then
        echo "[phase_7] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_7] /healthz never came up" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_7-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    kill ${STUB_PID} 2>/dev/null || true
    exit 5
fi

# ---- 5. 跑 gate ----
echo "[phase_7] step 5: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 6. 收尾 ----
echo "[phase_7] step 6: cleanup"
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true
kill ${STUB_PID} 2>/dev/null || true
wait ${STUB_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_7] gate FAILED exit=${GATE_EXIT}"
    echo "phase_7 FAIL: gate exit=${GATE_EXIT}" > "${DIFF_FILE}"
    exit 1
fi

echo "[phase_7] DONE — gate green, server stopped"
echo "phase_7 green at $(date -Iseconds)" > "${DIFF_FILE}"
exit 0