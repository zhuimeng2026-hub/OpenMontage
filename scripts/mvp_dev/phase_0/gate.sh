#!/usr/bin/env bash
# Phase 0 gate — §17.A 微信身份 端到端冒烟 (REAL)
#
# 流程:
#   1. 启动 /tmp/frameflow-bff-mvp (cmd/mvp 独立 binary) 后台
#   2. 等 /healthz 返回 200
#   3. POST /api/auth/login (WEIXIN_MOCK_AUTH=1) → 拿 JWT
#   4. GET /api/me/jwt 带 Bearer → 验 internal_user_id 字段
#   5. 杀进程
# 任意步骤失败 exit 1。

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
LOG_DIR="/opt/OpenMontage_Voicebox/logs/mvp_dev"
mkdir -p "${LOG_DIR}"
GATE_LOG="${LOG_DIR}/gate-phase_0-$(date +%Y%m%d-%H%M%S).log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_0 gate start $(date -Iseconds) ==="

status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[gate] STUB — tasks.yaml status=${status}"
    exit 0
fi

BIN="/tmp/frameflow-bff-mvp"
if [ ! -x "${BIN}" ]; then
    echo "[gate] FAIL: ${BIN} not built — run.sh must run first"
    exit 1
fi

PORT="${MVP_PORT:-18901}"  # 18901 避开 8900(frameflow-bff) / 8901(tweak_server uvicorn)
BASE="http://127.0.0.1:${PORT}"
export WEIXIN_MOCK_AUTH=1
export MVP_PORT="${PORT}"
export MVP_DB_PATH="${MVP_DB_PATH:-/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db}"

PID_FILE="/tmp/frameflow-bff-mvp.${PORT}.pid"
LOG_FILE="/tmp/frameflow-bff-mvp.${PORT}.log"

# ---- 启动 binary ----
echo "[gate] launching ${BIN} on :${PORT}"
"${BIN}" > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"
PID="$(cat "${PID_FILE}")"

cleanup () {
    if kill -0 "${PID}" 2>/dev/null; then
        kill "${PID}" 2>/dev/null || true
        sleep 0.5
        kill -9 "${PID}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
}
trap cleanup EXIT

# ---- 等 /healthz ----
echo "[gate] waiting for /healthz ..."
ready=0
for i in $(seq 1 30); do
    if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
        ready=1
        echo "[gate] /healthz ok after ${i} attempt(s)"
        break
    fi
    sleep 0.3
done
if [ "${ready}" -ne 1 ]; then
    echo "[gate] FAIL: /healthz never became ready; binary log:"
    tail -n 30 "${LOG_FILE}" 2>&1
    exit 1
fi

# ---- POST /api/auth/login ----
TEST_CODE="MOCK_${RANDOM}"
echo "[gate] POST /api/auth/login with code=${TEST_CODE}"
login_resp="$(curl -fsS -X POST "${BASE}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"code\":\"${TEST_CODE}\"}" \
    --max-time 5 2>&1)" || {
    echo "[gate] FAIL: login request errored: ${login_resp}"
    tail -n 20 "${LOG_FILE}" 2>&1
    exit 1
}
echo "[gate] login response: ${login_resp}"

# 解析 token + internal_user_id(jq 优先;退化到 grep)
if command -v jq >/dev/null 2>&1; then
    jwt="$(echo "${login_resp}" | jq -r '.token // empty')"
    iu="$(echo "${login_resp}" | jq -r '.internal_user_id // empty')"
else
    jwt="$(echo "${login_resp}" | grep -oE '"token":"[^"]+"' | head -1 | cut -d'"' -f4)"
    iu="$(echo "${login_resp}" | grep -oE '"internal_user_id":"[^"]+"' | head -1 | cut -d'"' -f4)"
fi

if [ -z "${jwt}" ]; then
    echo "[gate] FAIL: login response missing 'token'"
    exit 1
fi
if [ -z "${iu}" ]; then
    echo "[gate] FAIL: login response missing 'internal_user_id'"
    exit 1
fi
echo "[gate] token length: ${#jwt} chars; internal_user_id: ${iu}"

# ---- GET /api/me/jwt ----
echo "[gate] GET /api/me/jwt with Bearer token"
me_resp="$(curl -fsS "${BASE}/api/me/jwt" \
    -H "Authorization: Bearer ${jwt}" \
    --max-time 5 2>&1)" || {
    echo "[gate] FAIL: me request errored: ${me_resp}"
    exit 1
}
echo "[gate] me response: ${me_resp}"

# 验字段
if command -v jq >/dev/null 2>&1; then
    me_iu="$(echo "${me_resp}" | jq -r '.internal_user_id // empty')"
    me_openid="$(echo "${me_resp}" | jq -r '.openid // empty')"
else
    me_iu="$(echo "${me_resp}" | grep -oE '"internal_user_id":"[^"]+"' | head -1 | cut -d'"' -f4)"
    me_openid="$(echo "${me_resp}" | grep -oE '"openid":"[^"]+"' | head -1 | cut -d'"' -f4)"
fi

if [ -z "${me_iu}" ]; then
    echo "[gate] FAIL: me response missing 'internal_user_id'"
    exit 1
fi
if [ "${me_iu}" != "${iu}" ]; then
    echo "[gate] FAIL: me.internal_user_id (${me_iu}) != login.internal_user_id (${iu})"
    exit 1
fi
expected_openid="mock_openid_${TEST_CODE}"
if [ "${me_openid}" != "${expected_openid}" ]; then
    echo "[gate] FAIL: me.openid (${me_openid}) != expected (${expected_openid})"
    exit 1
fi

# ---- 验 schema 持久化 ----
echo "[gate] schema verify: wechat_users has internal_user_id row"
db_row="$(sqlite3 "${MVP_DB_PATH}" "SELECT internal_user_id FROM wechat_users WHERE openid='${expected_openid}'" 2>&1)"
if [ -z "${db_row}" ]; then
    echo "[gate] FAIL: no row in wechat_users for openid=${expected_openid}"
    exit 1
fi
if [ "${db_row}" != "${iu}" ]; then
    echo "[gate] FAIL: db.internal_user_id (${db_row}) != login.internal_user_id (${iu})"
    exit 1
fi

echo "[gate] PASS — login + me + schema persistence all green"
exit 0
