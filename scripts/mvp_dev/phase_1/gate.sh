#!/usr/bin/env bash
# Phase 1 gate — §17.B (multi-tenant) + §17.H (file permission) smoke test.
#
# Invoked by phase_1/run.sh (server already up on :18902) but is also
# runnable standalone: if no server is listening on :18902 it launches
# the MVP binary itself, runs the 14 scenarios, then tears it down on EXIT.
#
# Each scenario prints `PASS <name>` or `FAIL <name> expected=X got=Y`.
# Exits 0 if all PASS, 1 if any FAIL.

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
LOG_DIR="/opt/OpenMontage_Voicebox/logs/mvp_dev"
mkdir -p "${LOG_DIR}"
GATE_LOG="${LOG_DIR}/gate-phase_1-$(date +%Y%m%d-%H%M%S).log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_1 gate start $(date -Iseconds) ==="

PASS_COUNT=0
FAIL_COUNT=0

ok()  { echo "PASS $1"; PASS_COUNT=$((PASS_COUNT+1)); }
bad() { echo "FAIL $1 expected=$2 got=$3"; FAIL_COUNT=$((FAIL_COUNT+1)); }

# status gate (matches tasks.yaml contract)
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[gate] STUB — tasks.yaml status=${status} (expected READY)"
    exit 0
fi

BIN="/tmp/frameflow-bff-mvp-p1"
if [ ! -x "${BIN}" ]; then
    echo "[gate] FAIL: ${BIN} not built — run.sh must run first"
    exit 1
fi

PORT="${MVP_PORT:-18902}"
BASE="http://127.0.0.1:${PORT}"
export WEIXIN_MOCK_AUTH=1
export MVP_PORT="${PORT}"
DB_PATH="${MVP_DB_PATH:-/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db}"
export MVP_DB_PATH="${DB_PATH}"

OWN_PID=""  # only set if we launched the binary in this gate run

# kill any stale binary from a previous failed run (matches run.sh behaviour)
pkill -f "frameflow-bff-mvp-p1" 2>/dev/null || true
sleep 0.3

# Reuse an already-running server (e.g. launched by run.sh) or start one.
if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
    echo "[gate] server already up on :${PORT} — reusing"
else
    echo "[gate] launching ${BIN} on :${PORT}"
    "${BIN}" > "${LOG_DIR}/phase_1-gate-server.log" 2>&1 &
    OWN_PID=$!
fi

cleanup () {
    if [ -n "${OWN_PID}" ] && kill -0 "${OWN_PID}" 2>/dev/null; then
        kill "${OWN_PID}" 2>/dev/null || true
        sleep 0.3
        kill -9 "${OWN_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Wait for /healthz (only if we started it ourselves)
if [ -n "${OWN_PID}" ]; then
    ready=0
    for i in $(seq 1 30); do
        if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
            ready=1
            echo "[gate] /healthz ok after ${i} attempt(s) (own server)"
            break
        fi
        sleep 0.3
    done
    if [ "${ready}" -ne 1 ]; then
        echo "[gate] FAIL: /healthz never became ready; binary log:"
        tail -n 30 "${LOG_DIR}/phase_1-gate-server.log" 2>&1
        exit 1
    fi
fi

# JSON helper: jget <file> '.a.b'  →  prints the value or empty string.
jget () {
    local file="$1"; local path="$2"
    if command -v jq >/dev/null 2>&1; then
        jq -r "${path} // empty" < "${file}"
    else
        python3 - "$file" "$path" <<'PYEOF'
import json, sys
path = sys.argv[2].lstrip('.')
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(''); sys.exit(0)
v = d
for k in path.split('.'):
    if k == '':
        continue
    if isinstance(v, list):
        try:
            v = v[int(k)]
        except Exception:
            v = None
            break
    elif isinstance(v, dict):
        v = v.get(k)
    else:
        v = None
        break
print('' if v is None else v)
PYEOF
    fi
}

TMP=/tmp/gate_p1.$$
mkdir -p "${TMP}"
trap 'rm -rf "${TMP}"; cleanup' EXIT

# ---- Test 1: no-jwt-401 ----------------------------------------------
echo "[test] no-jwt-401: POST /api/tenants without Authorization"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" -d '{"name":"x"}')
if [ "${CODE}" = "401" ]; then ok "no-jwt-401"; else bad "no-jwt-401" "401" "${CODE}"; fi

# ---- Test 2: no-tenant-header-401 ------------------------------------
# Login Alice first so we have a token to omit X-Tenant-Id from.
curl -s -X POST "${BASE}/api/auth/login" -H "Content-Type: application/json" \
    -d '{"code":"ALICE_TEST"}' -o "${TMP}/alice_login.json"
ALICE_TOKEN=$(jget "${TMP}/alice_login.json" '.token')

echo "[test] no-tenant-header-401: POST /api/tenants/:id/members with JWT but no X-Tenant-Id"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/tenants/tn_x/members" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -d '{"user_id":"u_x"}')
if [ "${CODE}" = "401" ]; then ok "no-tenant-header-401"; else bad "no-tenant-header-401" "401" "${CODE}"; fi

# ---- Test 3: login-alice-200 -----------------------------------------
echo "[test] login-alice-200"
curl -s -X POST "${BASE}/api/auth/login" -H "Content-Type: application/json" \
    -d '{"code":"ALICE_TEST"}' -o "${TMP}/alice_login.json"
ALICE_TOKEN=$(jget "${TMP}/alice_login.json" '.token')
ALICE_IU=$(jget "${TMP}/alice_login.json" '.internal_user_id')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/auth/login" \
    -H "Content-Type: application/json" -d '{"code":"ALICE_TEST"}')
if [ "${CODE}" = "200" ] && [ -n "${ALICE_TOKEN}" ] && [ -n "${ALICE_IU}" ]; then
    ok "login-alice-200"
else
    bad "login-alice-200" "200" "${CODE} (token=${#ALICE_TOKEN} iu=${ALICE_IU})"
fi

# ---- Test 4: login-bob-200 -------------------------------------------
echo "[test] login-bob-200"
curl -s -X POST "${BASE}/api/auth/login" -H "Content-Type: application/json" \
    -d '{"code":"BOB_TEST"}' -o "${TMP}/bob_login.json"
BOB_TOKEN=$(jget "${TMP}/bob_login.json" '.token')
BOB_IU=$(jget "${TMP}/bob_login.json" '.internal_user_id')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/auth/login" \
    -H "Content-Type: application/json" -d '{"code":"BOB_TEST"}')
if [ "${CODE}" = "200" ] && [ -n "${BOB_TOKEN}" ] && [ -n "${BOB_IU}" ]; then
    ok "login-bob-200"
else
    bad "login-bob-200" "200" "${CODE} (token=${#BOB_TOKEN} iu=${BOB_IU})"
fi

# ---- Test 5: create-t1-200 -------------------------------------------
echo "[test] create-t1-200"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -d '{"name":"Alice Studio"}' -o "${TMP}/t1.json"
T1_ID=$(jget "${TMP}/t1.json" '.id')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -d '{"name":"Alice Studio"}')
if [ "${CODE}" = "200" ] && [ -n "${T1_ID}" ]; then
    ok "create-t1-200"
else
    bad "create-t1-200" "200" "${CODE} (t1_id=${T1_ID})"
fi

# ---- Test 6: create-t2-200 -------------------------------------------
echo "[test] create-t2-200"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -d '{"name":"Bob Studio"}' -o "${TMP}/t2.json"
T2_ID=$(jget "${TMP}/t2.json" '.id')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -d '{"name":"Bob Studio"}')
if [ "${CODE}" = "200" ] && [ -n "${T2_ID}" ]; then
    ok "create-t2-200"
else
    bad "create-t2-200" "200" "${CODE} (t2_id=${T2_ID})"
fi

# ---- Test 7: seed-file-acl (idempotent — DELETE then INSERT) ---------
echo "[test] seed-file-acl"
sqlite3 "${DB_PATH}" <<SQL
DELETE FROM file_acl WHERE file_key IN ('t1-asset-001','t2-asset-001');
INSERT OR REPLACE INTO file_acl (file_key, tenant_id, uploaded_by, media_type) VALUES
  ('t1-asset-001', '${T1_ID}', '${ALICE_IU}', 'image'),
  ('t2-asset-001', '${T2_ID}', '${BOB_IU}', 'image');
SQL
R1=$(sqlite3 "${DB_PATH}" "SELECT tenant_id FROM file_acl WHERE file_key='t1-asset-001'")
R2=$(sqlite3 "${DB_PATH}" "SELECT tenant_id FROM file_acl WHERE file_key='t2-asset-001'")
if [ "${R1}" = "${T1_ID}" ] && [ "${R2}" = "${T2_ID}" ]; then
    ok "seed-file-acl"
else
    bad "seed-file-acl" "T1=${T1_ID}/T2=${T2_ID}" "T1=${R1}/T2=${R2}"
fi

# ---- Test 8: cross-tenant-addmember-403 -----------------------------
# Bob is in T2; tries to add Alice's IU as a member of T1.
echo "[test] cross-tenant-addmember-403"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/tenants/${T1_ID}/members" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T2_ID}" \
    -d "{\"user_id\":\"${ALICE_IU}\"}")
if [ "${CODE}" = "403" ]; then ok "cross-tenant-addmember-403"; else bad "cross-tenant-addmember-403" "403" "${CODE}"; fi

# ---- Test 9: alice-adds-bob-200 --------------------------------------
echo "[test] alice-adds-bob-200"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/tenants/${T1_ID}/members" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"user_id\":\"${BOB_IU}\"}")
if [ "${CODE}" = "200" ]; then ok "alice-adds-bob-200"; else bad "alice-adds-bob-200" "200" "${CODE}"; fi

# ---- Test 10: bob-sign-t1-200 ----------------------------------------
echo "[test] bob-sign-t1-200"
curl -s -G "${BASE}/api/files/sign" \
    --data-urlencode "key=t1-asset-001" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" -o "${TMP}/sign.json"
SIGN_URL=$(jget "${TMP}/sign.json" '.url')
SIGN_EXP=$(jget "${TMP}/sign.json" '.exp')
SIGN_SIG=$(jget "${TMP}/sign.json" '.sig')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -G "${BASE}/api/files/sign" \
    --data-urlencode "key=t1-asset-001" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
if [ "${CODE}" = "200" ] && [ -n "${SIGN_URL}" ] && [ -n "${SIGN_EXP}" ] && [ -n "${SIGN_SIG}" ]; then
    ok "bob-sign-t1-200"
else
    bad "bob-sign-t1-200" "200" "${CODE} (url=${SIGN_URL})"
fi

# ---- Test 11: bob-serve-valid-200 ------------------------------------
echo "[test] bob-serve-valid-200"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}${SIGN_URL}")
if [ "${CODE}" = "200" ]; then ok "bob-serve-valid-200"; else bad "bob-serve-valid-200" "200" "${CODE}"; fi

# ---- Test 12: bob-serve-tampered-403 --------------------------------
# Drop last 2 hex chars, append "00" → guaranteed different sig.
echo "[test] bob-serve-tampered-403"
TAMPERED_SIG="${SIGN_SIG%??}00"
TAMPERED_URL="${BASE}/api/files/t1-asset-001?exp=${SIGN_EXP}&sig=${TAMPERED_SIG}"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${TAMPERED_URL}")
if [ "${CODE}" = "403" ]; then ok "bob-serve-tampered-403"; else bad "bob-serve-tampered-403" "403" "${CODE}"; fi

# ---- Test 13: bob-serve-expired-403 ----------------------------------
echo "[test] bob-serve-expired-403"
curl -s -G "${BASE}/api/files/sign" \
    --data-urlencode "key=t1-asset-001" \
    --data-urlencode "ttl_seconds=1" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" -o "${TMP}/sign_exp.json"
EXP_URL=$(jget "${TMP}/sign_exp.json" '.url')
EXP_SIG=$(jget "${TMP}/sign_exp.json" '.sig')
EXP_EXP=$(jget "${TMP}/sign_exp.json" '.exp')
echo "[gate]   ttl=1s, sleeping 2s so sig expires..."
sleep 2
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}${EXP_URL}")
if [ "${CODE}" = "403" ]; then ok "bob-serve-expired-403"; else bad "bob-serve-expired-403" "403" "${CODE}"; fi

# ---- Test 14: bob-sign-t2-403 ----------------------------------------
# Bob is in T1 (his X-Tenant-Id); the file t2-asset-001 belongs to T2 → 403.
echo "[test] bob-sign-t2-403"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -G "${BASE}/api/files/sign" \
    --data-urlencode "key=t2-asset-001" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
if [ "${CODE}" = "403" ]; then ok "bob-sign-t2-403"; else bad "bob-sign-t2-403" "403" "${CODE}"; fi

# ---- Summary ---------------------------------------------------------
echo "=== phase_1 gate done PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} $(date -Iseconds) ==="
if [ "${FAIL_COUNT}" -gt 0 ]; then exit 1; fi
exit 0
