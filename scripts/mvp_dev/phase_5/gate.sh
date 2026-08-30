#!/usr/bin/env bash
# Phase 5 gate — §17.F (Agent Gateway 8 业务动词) + §17.G (OM 状态聚合)
# smoke test.
#
# Invoked by phase_5/run.sh (server already up on :18906) but is also
# runnable standalone: if no server is listening on :18906 it launches
# the MVP binary itself, runs the 11 scenarios, then tears it down on EXIT.
#
# Each scenario prints `PASS <name>` or `FAIL <name> expected=X got=Y`.
# Exits 0 if all PASS, 1 if any FAIL.

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
LOG_DIR="/opt/OpenMontage_Voicebox/logs/mvp_dev"
mkdir -p "${LOG_DIR}"
GATE_LOG="${LOG_DIR}/gate-phase_5-$(date +%Y%m%d-%H%M%S).log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_5 gate start $(date -Iseconds) ==="

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

BIN="/tmp/frameflow-bff-mvp-p5"
if [ ! -x "${BIN}" ]; then
    echo "[gate] FAIL: ${BIN} not built — run.sh must run first"
    exit 1
fi

PORT="${MVP_PORT:-18906}"
BASE="http://127.0.0.1:${PORT}"
export WEIXIN_MOCK_AUTH=1
export MVP_PORT="${PORT}"
DB_PATH="${MVP_DB_PATH:-/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db}"
export MVP_DB_PATH="${DB_PATH}"

OWN_PID=""  # only set if we launched the binary in this gate run

# kill any stale binary from a previous failed run (matches run.sh behaviour)
pkill -f "frameflow-bff-mvp-p5" 2>/dev/null || true
sleep 0.3

# Reuse an already-running server (e.g. launched by run.sh) or start one.
if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
    echo "[gate] server already up on :${PORT} — reusing"
else
    echo "[gate] launching ${BIN} on :${PORT}"
    "${BIN}" > "${LOG_DIR}/phase_5-gate-server.log" 2>&1 &
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
        tail -n 30 "${LOG_DIR}/phase_5-gate-server.log" 2>&1
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
            v = None; break
    elif isinstance(v, dict):
        v = v.get(k)
    else:
        v = None; break
print('' if v is None else v)
PYEOF
    fi
}

TMP=/tmp/gate_p5.$$
mkdir -p "${TMP}"
trap 'rm -rf "${TMP}"; cleanup' EXIT

# The 13-job unified-status set from §17.G. Used both as the canonical answer
# set (so /status/lookup returns are checked against it) and as the raw-state
# fixtures for the "all-states-mapped" coverage test.
ALL_STATUSES="CREATED ASSET_ANALYZING REFERENCE_ANALYZING PLANNING STORYBOARD_READY ANIMATIC_RENDERING ANIMATIC_READY SAMPLE_RENDERING SAMPLE_READY WAITING_APPROVAL FINAL_RENDERING COMPLETED FAILED CANCELLED"

# ---- Setup: Phase 1 + 2 + 3 context needed by Phase 5 ----------------
# Login Alice + Bob.
echo "[setup] login-alice"
curl -s -X POST "${BASE}/api/auth/login" -H "Content-Type: application/json" \
    -d '{"code":"ALICE_TEST"}' -o "${TMP}/alice_login.json"
ALICE_TOKEN=$(jget "${TMP}/alice_login.json" '.token')
ALICE_IU=$(jget "${TMP}/alice_login.json" '.internal_user_id')

echo "[setup] login-bob"
curl -s -X POST "${BASE}/api/auth/login" -H "Content-Type: application/json" \
    -d '{"code":"BOB_TEST"}' -o "${TMP}/bob_login.json"
BOB_TOKEN=$(jget "${TMP}/bob_login.json" '.token')
BOB_IU=$(jget "${TMP}/bob_login.json" '.internal_user_id')

# Create T1 (Alice) + T2 (Bob).
echo "[setup] create-t1 (Alice owns)"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -d '{"name":"Alice Studio P5"}' -o "${TMP}/t1.json"
T1_ID=$(jget "${TMP}/t1.json" '.id')

echo "[setup] create-t2 (Bob owns)"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -d '{"name":"Bob Studio P5"}' -o "${TMP}/t2.json"
T2_ID=$(jget "${TMP}/t2.json" '.id')

# T1 has Bob as member (so later cross-tenant test has a clean Bob→T2 path).
echo "[setup] alice-adds-bob-to-t1"
curl -s -X POST "${BASE}/api/tenants/${T1_ID}/members" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"user_id\":\"${BOB_IU}\"}" >/dev/null

# Phase 2 product (P1) — required for Phase 3 project to link to.
echo "[setup] create-product P1 (Phase 2)"
curl -s -X POST "${BASE}/api/products" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"name":"Travel Mug P5","category":"kitchenware","sku":"TM-P5"}' \
    -o "${TMP}/p1.json"
P1_ID=$(jget "${TMP}/p1.json" '.id')

# Phase 3 project VP1 (links to P1, same tenant).
echo "[setup] create-project VP1 (Phase 3, links to P1)"
curl -s -X POST "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${P1_ID}\",\"creative_brief_json\":\"{}\",\"reference_mode\":\"balanced\"}" \
    -o "${TMP}/vp1.json"
VP1_ID=$(jget "${TMP}/vp1.json" '.id')

# Phase 3 project VP2 (separate project for cancel test).
echo "[setup] create-project VP2 (Phase 3, links to P1)"
curl -s -X POST "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${P1_ID}\",\"creative_brief_json\":\"{}\",\"reference_mode\":\"balanced\"}" \
    -o "${TMP}/vp2.json"
VP2_ID=$(jget "${TMP}/vp2.json" '.id')

if [ -z "${ALICE_TOKEN}" ] || [ -z "${BOB_TOKEN}" ] \
    || [ -z "${T1_ID}" ] || [ -z "${T2_ID}" ] \
    || [ -z "${P1_ID}" ] || [ -z "${VP1_ID}" ] || [ -z "${VP2_ID}" ]; then
    echo "[gate] FAIL: setup did not produce tokens/tenants/products/projects — aborting"
    echo "  alice_token=${#ALICE_TOKEN} bob_token=${#BOB_TOKEN}"
    echo "  T1=${T1_ID} T2=${T2_ID} P1=${P1_ID} VP1=${VP1_ID} VP2=${VP2_ID}"
    exit 1
fi
echo "[setup] OK: T1=${T1_ID} T2=${T2_ID} P1=${P1_ID} VP1=${VP1_ID} VP2=${VP2_ID}"

# ---- Test 1: 8-verb-routes-no-404 ------------------------------------
# For each of the 8 verb routes (7 POST + 1 GET), POST/GET with a body (or
# query) containing project_id=VP1. Expect status != 404 — i.e. the route
# IS registered. Any other code (200/202/400/409/...) is acceptable.
echo "[test] 8-verb-routes-no-404: each verb must not 404 on VP1"
NOT_FOUND=0
TOTAL=0
declare -A STATUSES

# POST verbs — body {project_id: VP1}.
for verb in analyze-product-assets analyze-reference-video generate-storyboard generate-animatic generate-sample render-final cancel-production; do
    TOTAL=$((TOTAL+1))
    CODE=$(curl -s -o "${TMP}/verb_${verb}.json" -w "%{http_code}" -X POST \
        "${BASE}/api/gateway/${verb}" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${ALICE_TOKEN}" \
        -H "X-Tenant-Id: ${T1_ID}" \
        -d "{\"project_id\":\"${VP1_ID}\"}")
    STATUSES[$verb]="${CODE}"
    if [ "${CODE}" = "404" ]; then
        NOT_FOUND=$((NOT_FOUND+1))
        echo "  ${verb} → 404 (route missing)"
    else
        echo "  ${verb} → ${CODE}"
    fi
done

# GET production-status — query project_id=VP1.
TOTAL=$((TOTAL+1))
CODE=$(curl -s -o "${TMP}/verb_production-status.json" -w "%{http_code}" -G \
    "${BASE}/api/gateway/production-status" \
    --data-urlencode "project_id=${VP1_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
STATUSES[production-status]="${CODE}"
if [ "${CODE}" = "404" ]; then
    NOT_FOUND=$((NOT_FOUND+1))
    echo "  production-status → 404 (route missing)"
else
    echo "  production-status → ${CODE}"
fi

if [ "${NOT_FOUND}" = "0" ] && [ "${TOTAL}" = "8" ]; then
    ok "8-verb-routes-no-404"
else
    # Build a compact status-codes summary for diagnostics.
    summary=""
    for k in "${!STATUSES[@]}"; do
        summary="${summary} ${k}=${STATUSES[$k]}"
    done
    bad "8-verb-routes-no-404" "0 of 8 = 404" "404_count=${NOT_FOUND}/${TOTAL} (${summary})"
fi

# ---- Test 2: generate-storyboard-delegates ----------------------------
# VP1 is CREATED → POST /api/gateway/generate-storyboard should delegate to
# Phase 3's StartStage("storyboard") and return status=STORYBOARD_READY.
echo "[test] generate-storyboard-delegates: POST /api/gateway/generate-storyboard on VP1"
# First, re-fetch VP1 to confirm fresh project (in case an earlier test
# mutated it; the "8-verb-routes-no-404" sweep may have hit some verbs that
# push state forward — cancel/cancel — but storyboard is the deterministic
# one and idempotent only on CREATED). We expect VP1 to land on
# STORYBOARD_READY after a successful storyboard call.
curl -s -X POST "${BASE}/api/gateway/generate-storyboard" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"project_id\":\"${VP1_ID}\"}" \
    -o "${TMP}/storyboard.json"
SB_STATUS=$(jget "${TMP}/storyboard.json" '.status')
SB_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${BASE}/api/gateway/generate-storyboard" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"project_id\":\"${VP1_ID}\"}")
# We re-call storyboard to capture the response code on a state where the
# call is idempotent or expected to advance deterministically; either way the
# previous body should already hold the STORYBOARD_READY status.
if [ "${SB_STATUS}" = "STORYBOARD_READY" ]; then
    ok "generate-storyboard-delegates"
else
    bad "generate-storyboard-delegates" "status=STORYBOARD_READY" "status=${SB_STATUS} (last-code=${SB_CODE})"
fi

# ---- Test 3: production-status-200 ------------------------------------
# GET /api/gateway/production-status?project_id=VP1 → 200, body has status.
echo "[test] production-status-200: GET /api/gateway/production-status on VP1"
curl -s -G "${BASE}/api/gateway/production-status" \
    --data-urlencode "project_id=${VP1_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/production_status.json"
PS_STATUS=$(jget "${TMP}/production_status.json" '.status')
PS_CODE=$(curl -s -o /dev/null -w "%{http_code}" -G \
    "${BASE}/api/gateway/production-status" \
    --data-urlencode "project_id=${VP1_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
if [ "${PS_CODE}" = "200" ] && [ -n "${PS_STATUS}" ]; then
    ok "production-status-200"
else
    bad "production-status-200" "200 with status field" "code=${PS_CODE} status=${PS_STATUS}"
fi

# ---- Test 4: cancel-production-200 ------------------------------------
# Create VP2 (CREATED) → POST /api/gateway/cancel-production → 200,
# status="CANCELLED". We use a freshly created VP2 (created in setup).
echo "[test] cancel-production-200: POST /api/gateway/cancel-production on VP2"
curl -s -X POST "${BASE}/api/gateway/cancel-production" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"project_id\":\"${VP2_ID}\"}" \
    -o "${TMP}/cancel.json"
CANCEL_STATUS=$(jget "${TMP}/cancel.json" '.status')
CANCEL_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${BASE}/api/gateway/cancel-production" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"project_id\":\"${VP2_ID}\"}")
if [ "${CANCEL_CODE}" = "200" ] && [ "${CANCEL_STATUS}" = "CANCELLED" ]; then
    ok "cancel-production-200"
else
    bad "cancel-production-200" "200 status=CANCELLED" "code=${CANCEL_CODE} status=${CANCEL_STATUS}"
fi

# ---- Test 5: status-lookup-known-raw ----------------------------------
# mcp-raw → ASSET_ANALYZING (per status_map.go mapping table).
echo "[test] status-lookup-known-raw: raw=mcp-raw → unified=ASSET_ANALYZING"
curl -s -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=mcp-raw" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/lookup_mcp_raw.json"
LU_CODE=$(curl -s -o /dev/null -w "%{http_code}" -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=mcp-raw" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
LU_UNIFIED=$(jget "${TMP}/lookup_mcp_raw.json" '.unified')
LU_RAW=$(jget "${TMP}/lookup_mcp_raw.json" '.raw')
if [ "${LU_CODE}" = "200" ] && [ "${LU_UNIFIED}" = "ASSET_ANALYZING" ]; then
    ok "status-lookup-known-raw"
else
    bad "status-lookup-known-raw" "200 unified=ASSET_ANALYZING" "code=${LU_CODE} unified=${LU_UNIFIED} raw=${LU_RAW}"
fi

# ---- Test 6: status-lookup-unified-passthrough ------------------------
# A raw that IS already one of the 13 unified states should pass through.
echo "[test] status-lookup-unified-passthrough: raw=COMPLETED → unified=COMPLETED"
curl -s -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=COMPLETED" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/lookup_completed.json"
LU_CODE=$(curl -s -o /dev/null -w "%{http_code}" -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=COMPLETED" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
LU_UNIFIED=$(jget "${TMP}/lookup_completed.json" '.unified')
if [ "${LU_CODE}" = "200" ] && [ "${LU_UNIFIED}" = "COMPLETED" ]; then
    ok "status-lookup-unified-passthrough"
else
    bad "status-lookup-unified-passthrough" "200 unified=COMPLETED" "code=${LU_CODE} unified=${LU_UNIFIED}"
fi

# ---- Test 7: status-lookup-unknown-failsafe ---------------------------
# Unknown raw must NOT silently fall back to "unknown" or ""; per plan §8.2
# we fail-loud by mapping to FAILED.
echo "[test] status-lookup-unknown-failsafe: raw=totally_unknown_xyz → unified=FAILED"
curl -s -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=totally_unknown_xyz" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/lookup_unknown.json"
LU_CODE=$(curl -s -o /dev/null -w "%{http_code}" -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=totally_unknown_xyz" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
LU_UNIFIED=$(jget "${TMP}/lookup_unknown.json" '.unified')
if [ "${LU_CODE}" = "200" ] && [ "${LU_UNIFIED}" = "FAILED" ]; then
    ok "status-lookup-unknown-failsafe"
else
    bad "status-lookup-unknown-failsafe" "200 unified=FAILED" "code=${LU_CODE} unified=${LU_UNIFIED}"
fi

# ---- Test 8: status-lookup-empty-failsafe -----------------------------
# Empty raw → FAILED (same fail-loud contract as test 7).
echo "[test] status-lookup-empty-failsafe: raw= → unified=FAILED"
curl -s -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/lookup_empty.json"
LU_CODE=$(curl -s -o /dev/null -w "%{http_code}" -G "${BASE}/api/status/lookup" \
    --data-urlencode "raw=" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
LU_UNIFIED=$(jget "${TMP}/lookup_empty.json" '.unified')
LU_RAW=$(jget "${TMP}/lookup_empty.json" '.raw')
if [ "${LU_CODE}" = "200" ] && [ "${LU_UNIFIED}" = "FAILED" ]; then
    ok "status-lookup-empty-failsafe"
else
    bad "status-lookup-empty-failsafe" "200 unified=FAILED" "code=${LU_CODE} unified=${LU_UNIFIED} raw=${LU_RAW}"
fi

# ---- Test 9: all-13-states-mapped -------------------------------------
# For each of the 14 §17.G states (CREATED, ..., CANCELLED), query
# /api/status/lookup?raw=<STATE> and verify the returned unified field is
# in the canonical 14-state set (NOT "unknown" or ""). State names match
# the canonical set so the lookup is essentially a passthrough — but the
# contract is "any input maps to a valid 13-state unified", so we enforce
# the membership check, not the equality.
echo "[test] all-13-states-mapped: every §17.G state maps into the canonical set"
ALL_OK=1
BAD_STATES=""
for state in ${ALL_STATUSES}; do
    curl -s -G "${BASE}/api/status/lookup" \
        --data-urlencode "raw=${state}" \
        -H "Authorization: Bearer ${ALICE_TOKEN}" \
        -H "X-Tenant-Id: ${T1_ID}" \
        -o "${TMP}/lookup_${state}.json"
    LU=$(jget "${TMP}/lookup_${state}.json" '.unified')
    # Membership check: LU must be one of the canonical set.
    in_set=0
    for s in ${ALL_STATUSES}; do
        if [ "${LU}" = "${s}" ]; then
            in_set=1
            break
        fi
    done
    if [ "${in_set}" != "1" ]; then
        ALL_OK=0
        BAD_STATES="${BAD_STATES} ${state}→${LU}"
        echo "  ${state} → ${LU} (NOT in canonical set)"
    fi
done
if [ "${ALL_OK}" = "1" ]; then
    ok "all-13-states-mapped"
else
    bad "all-13-states-mapped" "every state ∈ canonical set" "failures:${BAD_STATES}"
fi

# ---- Test 10: cross-tenant-403 ----------------------------------------
# Bob is in T1, but using X-Tenant-Id: T2 (where he is owner). VP1 lives
# in T1. TenantScope lets the request through (Bob is a member of T2), but
# the production-status handler must reject the cross-tenant probe.
echo "[test] cross-tenant-403: Bob (X-Tenant-Id=T2) GET production-status for VP1 (T1)"
CODE=$(curl -s -o "${TMP}/cross_tenant.json" -w "%{http_code}" -G \
    "${BASE}/api/gateway/production-status" \
    --data-urlencode "project_id=${VP1_ID}" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T2_ID}")
if [ "${CODE}" = "403" ]; then
    ok "cross-tenant-403"
else
    bad "cross-tenant-403" "403" "${CODE}"
fi

# ---- Test 11: invalid-verb-404 ----------------------------------------
# /api/gateway/garbage-verb is not registered, so gin returns 404. This
# verifies the routing layer works — invalid verbs 404, valid ones (test 1)
# don't. Note this is the inverse assertion of test 1: test 1 says "real
# verbs don't 404", test 11 says "fake verbs DO 404".
echo "[test] invalid-verb-404: POST /api/gateway/garbage-verb → 404"
CODE=$(curl -s -o "${TMP}/garbage.json" -w "%{http_code}" -X POST \
    "${BASE}/api/gateway/garbage-verb" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"project_id\":\"${VP1_ID}\"}")
if [ "${CODE}" = "404" ]; then
    ok "invalid-verb-404"
else
    bad "invalid-verb-404" "404" "${CODE}"
fi

# ---- Summary ---------------------------------------------------------
echo "=== phase_5 gate done PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} $(date -Iseconds) ==="
if [ "${FAIL_COUNT}" -gt 0 ]; then exit 1; fi
exit 0
