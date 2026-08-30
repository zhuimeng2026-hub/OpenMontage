#!/usr/bin/env bash
# Phase 4 gate — §17.E (Quota: reserve / consume / refund) smoke test.
#
# Invoked by phase_4/run.sh (server already up on :18905) but is also
# runnable standalone: if no server is listening on :18905 it launches
# the MVP binary itself, runs the 11 scenarios, then tears it down on EXIT.
#
# New Phase 4 API (2026-08-30 refactor): consume / refund take only
# `{amount}` (no reservation_id). Reservation rows are still written to
# the ledger but the HTTP surface is simpler:
#
#   GET  /api/quota                 → {tenant_id, available_credits,
#                                     reserved_credits, consumed_credits, tier}
#   POST /api/quota/reserve         → {amount, job_id}
#                                    → 200 {reservation_id, amount}  /  402
#   POST /api/quota/consume         → {amount}
#                                    → 200 {status:"consumed", amount}  /  409
#   POST /api/quota/refund          → {amount}
#                                    → 200 {status:"refunded", amount}  /  409
#   POST /api/video-projects/:id/render  (auto-reserves 50 credits)
#
# Each scenario prints `PASS <name>` or `FAIL <name> expected=X got=Y`.
# Exits 0 if all PASS, 1 if any FAIL.

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
LOG_DIR="/opt/OpenMontage_Voicebox/logs/mvp_dev"
mkdir -p "${LOG_DIR}"
GATE_LOG="${LOG_DIR}/gate-phase_4-$(date +%Y%m%d-%H%M%S).log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_4 gate start $(date -Iseconds) ==="

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

BIN="/tmp/frameflow-bff-mvp-p4"
if [ ! -x "${BIN}" ]; then
    echo "[gate] FAIL: ${BIN} not built — run.sh must run first"
    exit 1
fi

PORT="${MVP_PORT:-18905}"
BASE="http://127.0.0.1:${PORT}"
export WEIXIN_MOCK_AUTH=1
export MVP_PORT="${PORT}"
DB_PATH="${MVP_DB_PATH:-/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db}"
export MVP_DB_PATH="${DB_PATH}"

OWN_PID=""  # only set if we launched the binary in this gate run

# kill any stale binary from a previous failed run (matches run.sh behaviour)
pkill -f "frameflow-bff-mvp-p4" 2>/dev/null || true
sleep 0.3

# Reuse an already-running server (e.g. launched by run.sh) or start one.
if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
    echo "[gate] server already up on :${PORT} — reusing"
else
    echo "[gate] launching ${BIN} on :${PORT}"
    "${BIN}" > "${LOG_DIR}/phase_4-gate-server.log" 2>&1 &
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
        tail -n 30 "${LOG_DIR}/phase_4-gate-server.log" 2>&1
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

# Count helper: returns length of a JSON array, or 0.
jcount () {
    local file="$1"; local path="$2"
    if command -v jq >/dev/null 2>&1; then
        jq -r "${path} | length" < "${file}"
    else
        python3 - "$file" "$path" <<'PYEOF'
import json, sys
path = sys.argv[2].lstrip('.')
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print('0'); sys.exit(0)
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
print('0' if v is None else len(v))
PYEOF
    fi
}

# project_status <project_id> → prints current status via GET /status.
project_status () {
    curl -s -X GET "${BASE}/api/video-projects/$1/status" \
        -H "Authorization: Bearer ${ALICE_TOKEN}" \
        -H "X-Tenant-Id: ${T1_ID}" \
        -o "${TMP}/status_probe.json"
    jget "${TMP}/status_probe.json" '.status'
}

# wait_status <project_id> <expected> [max_attempts]
# Polls GET /status until it reads <expected>; the MVP runner takes ~400ms.
# Prints whatever status it last observed.
wait_status () {
    local pid="$1"; local want="$2"; local tries="${3:-20}"
    local seen=""
    for _ in $(seq 1 "${tries}"); do
        seen="$(project_status "${pid}")"
        if [ "${seen}" = "${want}" ]; then
            break
        fi
        sleep 0.3
    done
    echo "${seen}"
}

TMP=/tmp/gate_p4.$$
mkdir -p "${TMP}"
trap 'rm -rf "${TMP}"; cleanup' EXIT

# Unique-per-run suffix so repeated gate runs never collide on tenant /
# product names (the DB is cumulative across phases and runs).
STAMP="$(date +%Y%m%d%H%M%S)-$$"

# Prepare fake jpeg-like bytes for the Phase 2 asset upload.
echo "fake jpeg bytes for hero_01" > "${TMP}/hero_01.jpg"

# ---- Setup (Phase 1): login Alice + Bob, create T1 + T2 --------------
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

echo "[setup] create-t1 (Alice owns)"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -d "{\"name\":\"Alice Studio P4 ${STAMP}\"}" -o "${TMP}/t1.json"
T1_ID=$(jget "${TMP}/t1.json" '.id')

echo "[setup] create-t2 (Bob owns)"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -d "{\"name\":\"Bob Studio P4 ${STAMP}\"}" -o "${TMP}/t2.json"
T2_ID=$(jget "${TMP}/t2.json" '.id')

# Add Bob as a member of T1 so cross-tenant denial is about the *scope*
# header (T2), not about Bob being a total stranger.
echo "[setup] alice-adds-bob-to-t1"
curl -s -X POST "${BASE}/api/tenants/${T1_ID}/members" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"user_id\":\"${BOB_IU}\"}" >/dev/null

if [ -z "${ALICE_TOKEN}" ] || [ -z "${BOB_TOKEN}" ] || [ -z "${T1_ID}" ] || [ -z "${T2_ID}" ]; then
    echo "[gate] FAIL: Phase 1 setup did not produce tokens/tenants — aborting"
    exit 1
fi
echo "[setup] alice_iu=${ALICE_IU} bob_iu=${BOB_IU} t1=${T1_ID} t2=${T2_ID}"

# ---- Setup (Phase 2): product P1 in T1 + one asset -------------------
echo "[setup] create-product-p1"
curl -s -X POST "${BASE}/api/products" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"name\":\"Travel Mug P4 ${STAMP}\",\"category\":\"kitchenware\",\"sku\":\"TM-P4-${STAMP}\"}" \
    -o "${TMP}/product.json"
PRODUCT_ID=$(jget "${TMP}/product.json" '.id')

echo "[setup] upload-asset-to-p1"
curl -s -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/hero_01.jpg;filename=hero_01.jpg" \
    -o "${TMP}/asset_hero.json"
HERO_ASSET_ID=$(jget "${TMP}/asset_hero.json" '.asset_id')
HERO_FILE_KEY=$(jget "${TMP}/asset_hero.json" '.file_key')

if [ -z "${PRODUCT_ID}" ] || [ -z "${HERO_FILE_KEY}" ]; then
    echo "[gate] FAIL: Phase 2 setup did not produce product/asset — aborting"
    echo "        product=$(cat "${TMP}/product.json" 2>/dev/null)"
    echo "        asset=$(cat "${TMP}/asset_hero.json" 2>/dev/null)"
    exit 1
fi
echo "[setup] product=${PRODUCT_ID} asset=${HERO_ASSET_ID} file_key=${HERO_FILE_KEY}"

# ---- Setup (Phase 3): project VP1 in T1, advance to SAMPLE_READY ----
echo "[setup] create-video-project-vp1"
CODE=$(curl -s -o "${TMP}/vp1.json" -w "%{http_code}" -X POST "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${PRODUCT_ID}\"}")
VP1_ID=$(jget "${TMP}/vp1.json" '.id')
if [ "${CODE}" != "200" ] || [ -z "${VP1_ID}" ]; then
    echo "[gate] FAIL: create VP1 failed — code=${CODE} body=$(cat "${TMP}/vp1.json" 2>/dev/null)"
    exit 1
fi
echo "[setup] vp1=${VP1_ID}"

# Storyboard (Phase 4 inlined handler jumps straight to STORYBOARD_READY).
echo "[setup] start-storyboard"
CODE=$(curl -s -o "${TMP}/vp1_sb.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${VP1_ID}/storyboard" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
S1=$(wait_status "${VP1_ID}" "STORYBOARD_READY" 20)
if [ "${CODE}" != "200" ] || [ "${S1}" != "STORYBOARD_READY" ]; then
    echo "[gate] FAIL: STORYBOARD_READY not reached — code=${CODE} status=${S1}"
    exit 1
fi
echo "[setup] storyboard ok"

# Animatic → wait for ANIMATIC_READY.
echo "[setup] start-animatic"
CODE=$(curl -s -o "${TMP}/vp1_an.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${VP1_ID}/animatic" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
S2=$(wait_status "${VP1_ID}" "ANIMATIC_READY" 20)
if [ "${CODE}" != "200" ] || [ "${S2}" != "ANIMATIC_READY" ]; then
    echo "[gate] FAIL: ANIMATIC_READY not reached — code=${CODE} status=${S2}"
    exit 1
fi
echo "[setup] animatic ok"

# Sample → wait for SAMPLE_READY.
echo "[setup] start-sample"
CODE=$(curl -s -o "${TMP}/vp1_sa.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${VP1_ID}/sample" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
S3=$(wait_status "${VP1_ID}" "SAMPLE_READY" 20)
if [ "${CODE}" != "200" ] || [ "${S3}" != "SAMPLE_READY" ]; then
    echo "[gate] FAIL: SAMPLE_READY not reached — code=${CODE} status=${S3}"
    exit 1
fi
echo "[setup] sample ok"

# A SECOND project, still in CREATED, for the render test.
# /render reserves 50 credits before writing the job row — we need a
# project that's still in CREATED so /render is legal (Phase 4 inlined
# handler advances status to COMPLETED, leaving it terminal).
echo "[setup] create-second-project-vp_render (still CREATED)"
CODE=$(curl -s -o "${TMP}/vp_render.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${PRODUCT_ID}\"}")
VP_RENDER_ID=$(jget "${TMP}/vp_render.json" '.id')
if [ "${CODE}" != "200" ] || [ -z "${VP_RENDER_ID}" ]; then
    echo "[gate] FAIL: create VP_RENDER failed — code=${CODE} body=$(cat "${TMP}/vp_render.json" 2>/dev/null)"
    exit 1
fi
echo "[setup] vp_render=${VP_RENDER_ID}"

# ---- Test 1: get-quota-200 ------------------------------------------
# Read initial Alice quota. Free tier = 100 available / 0 reserved / 0 consumed.
echo "[test] get-quota-200: Alice GET /api/quota"
CODE=$(curl -s -o "${TMP}/quota0.json" -w "%{http_code}" -X GET "${BASE}/api/quota" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
Q0_AVAIL=$(jget "${TMP}/quota0.json" '.available_credits')
Q0_RESV=$(jget "${TMP}/quota0.json" '.reserved_credits')
Q0_CONS=$(jget "${TMP}/quota0.json" '.consumed_credits')
Q0_TIER=$(jget "${TMP}/quota0.json" '.tier')
Q0_TENANT=$(jget "${TMP}/quota0.json" '.tenant_id')
if [ "${CODE}" = "200" ] \
    && [ -n "${Q0_AVAIL}" ] \
    && [ -n "${Q0_RESV}" ] \
    && [ -n "${Q0_CONS}" ] \
    && [ "${Q0_TENANT}" = "${T1_ID}" ] \
    && [ -n "${Q0_TIER}" ]; then
    ok "get-quota-200"
else
    bad "get-quota-200" "200, tenant=T1, all 3 balance fields" \
        "code=${CODE} avail=${Q0_AVAIL} resv=${Q0_RESV} cons=${Q0_CONS} tier=${Q0_TIER} tenant=${Q0_TENANT}"
fi

# ---- Test 2: reserve-10-200 -----------------------------------------
# Single curl captures body + status code — never double-call (would
# double-spend against the available balance).
echo "[test] reserve-10-200: POST /api/quota/reserve {amount:10, job_id}"
CODE=$(curl -s -o "${TMP}/reserve1.json" -w "%{http_code}" -X POST \
    "${BASE}/api/quota/reserve" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"amount":10,"job_id":"jb_t1"}')
RESV1_AMOUNT=$(jget "${TMP}/reserve1.json" '.amount')
RESV1_RID=$(jget "${TMP}/reserve1.json" '.reservation_id')
if [ "${CODE}" = "200" ] && [ "${RESV1_AMOUNT}" = "10" ]; then
    ok "reserve-10-200"
else
    bad "reserve-10-200" "200 + amount=10" "code=${CODE} rid=${RESV1_RID} amount=${RESV1_AMOUNT}"
fi

# ---- Test 3: reserve-decreases-available ----------------------------
# Re-read /quota: avail dropped by 10, resv grew by 10, cons unchanged.
echo "[test] reserve-decreases-available: avail < pre, resv > pre, cons unchanged"
CODE=$(curl -s -o "${TMP}/quota1.json" -w "%{http_code}" -X GET "${BASE}/api/quota" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
Q1_AVAIL=$(jget "${TMP}/quota1.json" '.available_credits')
Q1_RESV=$(jget "${TMP}/quota1.json" '.reserved_credits')
Q1_CONS=$(jget "${TMP}/quota1.json" '.consumed_credits')
if [ "${CODE}" = "200" ] \
    && [ -n "${Q1_AVAIL}" ] && [ -n "${Q1_RESV}" ] \
    && awk "BEGIN { exit !(${Q1_AVAIL} < ${Q0_AVAIL}) }" \
    && awk "BEGIN { exit !(${Q1_RESV} > ${Q0_RESV}) }" \
    && [ "${Q1_CONS}" = "${Q0_CONS}" ]; then
    ok "reserve-decreases-available"
else
    bad "reserve-decreases-available" "avail < prev, resv > prev, cons unchanged" \
        "code=${CODE} avail=${Q0_AVAIL}->${Q1_AVAIL} resv=${Q0_RESV}->${Q1_RESV} cons=${Q0_CONS}->${Q1_CONS}"
fi
PRE_CONSUME_AVAIL="${Q1_AVAIL}"
PRE_CONSUME_RESV="${Q1_RESV}"
PRE_CONSUME_CONS="${Q1_CONS}"

# ---- Test 4: reserve-insufficient-402 -------------------------------
echo "[test] reserve-insufficient-402: amount=99999 → 402"
CODE=$(curl -s -o "${TMP}/reserve_insuf.json" -w "%{http_code}" -X POST \
    "${BASE}/api/quota/reserve" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"amount":99999,"job_id":"jb_too_big"}')
if [ "${CODE}" = "402" ]; then
    ok "reserve-insufficient-402"
else
    bad "reserve-insufficient-402" "402" "${CODE} body=$(cat "${TMP}/reserve_insuf.json" 2>/dev/null)"
fi

# ---- Test 5: consume-10-200 -----------------------------------------
# New API: consume takes only {amount} — no reservation_id. It moves 10
# from reserved → consumed (matches our reserve above).
echo "[test] consume-10-200: POST /api/quota/consume {amount:10}"
CODE=$(curl -s -o "${TMP}/consume1.json" -w "%{http_code}" -X POST \
    "${BASE}/api/quota/consume" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"amount":10}')
CONS_STATUS=$(jget "${TMP}/consume1.json" '.status')
CONS_AMOUNT=$(jget "${TMP}/consume1.json" '.amount')
if [ "${CODE}" = "200" ] && [ "${CONS_STATUS}" = "consumed" ] && [ "${CONS_AMOUNT}" = "10" ]; then
    ok "consume-10-200"
else
    bad "consume-10-200" "200 + status=consumed + amount=10" \
        "code=${CODE} status=${CONS_STATUS} amount=${CONS_AMOUNT}"
fi

# ---- Test 6: consume-decreases-reserved -----------------------------
# Re-read /quota: resv dropped by 10, cons grew by 10, avail unchanged
# (consume does NOT touch available — it only moves reserved→consumed).
echo "[test] consume-decreases-reserved: resv < pre, cons > pre"
CODE=$(curl -s -o "${TMP}/quota2.json" -w "%{http_code}" -X GET "${BASE}/api/quota" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
Q2_AVAIL=$(jget "${TMP}/quota2.json" '.available_credits')
Q2_RESV=$(jget "${TMP}/quota2.json" '.reserved_credits')
Q2_CONS=$(jget "${TMP}/quota2.json" '.consumed_credits')
if [ "${CODE}" = "200" ] \
    && [ -n "${Q2_RESV}" ] && [ -n "${Q2_CONS}" ] \
    && awk "BEGIN { exit !(${Q2_RESV} < ${PRE_CONSUME_RESV}) }" \
    && awk "BEGIN { exit !(${Q2_CONS} > ${PRE_CONSUME_CONS}) }"; then
    ok "consume-decreases-reserved"
else
    bad "consume-decreases-reserved" "resv went down, cons went up" \
        "code=${CODE} resv=${PRE_CONSUME_RESV}->${Q2_RESV} cons=${PRE_CONSUME_CONS}->${Q2_CONS} avail=${Q2_AVAIL}"
fi
POST_CONSUME_AVAIL="${Q2_AVAIL}"
POST_CONSUME_RESV="${Q2_RESV}"

# ---- Test 7: refund-10-200 ------------------------------------------
# Reserve another 10 so we have something in reserved to refund.
echo "[test] refund-10-200: reserve+refund {amount:10}"
CODE=$(curl -s -o "${TMP}/reserve2.json" -w "%{http_code}" -X POST \
    "${BASE}/api/quota/reserve" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"amount":10,"job_id":"jb_refund_target"}')
RESV2_AMOUNT=$(jget "${TMP}/reserve2.json" '.amount')
RESV2_CODE="${CODE}"

CODE=$(curl -s -o "${TMP}/refund1.json" -w "%{http_code}" -X POST \
    "${BASE}/api/quota/refund" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"amount":10}')
REFUND_STATUS=$(jget "${TMP}/refund1.json" '.status')
REFUND_AMOUNT=$(jget "${TMP}/refund1.json" '.amount')
if [ "${RESV2_CODE}" = "200" ] && [ "${RESV2_AMOUNT}" = "10" ] \
    && [ "${CODE}" = "200" ] && [ "${REFUND_STATUS}" = "refunded" ] && [ "${REFUND_AMOUNT}" = "10" ]; then
    ok "refund-10-200"
else
    bad "refund-10-200" "200 reserve + 200 refund {status:refunded, amount:10}" \
        "reserve_code=${RESV2_CODE} reserve_amount=${RESV2_AMOUNT} refund_code=${CODE} refund_status=${REFUND_STATUS} refund_amount=${REFUND_AMOUNT}"
fi

# ---- Test 8: refund-restores-available ------------------------------
# After reserve-10 + refund-10 the net effect on the quota is zero — avail
# and resv both end where they were. Verify the invariant:
#   available + reserved + consumed == initial_grant (100)
# AND that Q3 == POST_CONSUME (net zero change after reserve+refund cycle).
echo "[test] refund-restores-available: avail+resv+cons == 100 invariant + Q3 matches POST_CONSUME"
CODE=$(curl -s -o "${TMP}/quota3.json" -w "%{http_code}" -X GET "${BASE}/api/quota" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
Q3_AVAIL=$(jget "${TMP}/quota3.json" '.available_credits')
Q3_RESV=$(jget "${TMP}/quota3.json" '.reserved_credits')
Q3_CONS=$(jget "${TMP}/quota3.json" '.consumed_credits')
SUM=$(awk "BEGIN { print ${Q3_AVAIL} + ${Q3_RESV} + ${Q3_CONS} }")
if [ "${CODE}" = "200" ] \
    && [ -n "${Q3_AVAIL}" ] && [ -n "${Q3_RESV}" ] \
    && awk "BEGIN { exit !(${SUM} == 100) }" \
    && awk "BEGIN { exit !(${Q3_AVAIL} == ${POST_CONSUME_AVAIL}) }" \
    && awk "BEGIN { exit !(${Q3_RESV} == ${POST_CONSUME_RESV}) }"; then
    ok "refund-restores-available"
else
    bad "refund-restores-available" "sum=100 + Q3 matches POST_CONSUME" \
        "code=${CODE} avail=${POST_CONSUME_AVAIL}->${Q3_AVAIL} resv=${POST_CONSUME_RESV}->${Q3_RESV} cons=${Q3_CONS} sum=${SUM}"
fi
PRE_RENDER_AVAIL="${Q3_AVAIL}"
PRE_RENDER_RESV="${Q3_RESV}"

# ---- Test 9: render-auto-reserve-200 --------------------------------
# /render on the CREATED VP_RENDER auto-reserves 50 credits, writes the
# job row, and advances the project to COMPLETED. Single curl captures
# body + status code (200 or 202 both acceptable per the new contract).
echo "[test] render-auto-reserve-200: POST /render on VP_RENDER"
CODE=$(curl -s -o "${TMP}/render.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${VP_RENDER_ID}/render" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
RENDER_STATUS=$(jget "${TMP}/render.json" '.status')
RENDER_JOB=$(jget "${TMP}/render.json" '.job_id')
if [ "${CODE}" = "200" ] || [ "${CODE}" = "202" ]; then
    ok "render-auto-reserve-200"
else
    bad "render-auto-reserve-200" "200 or 202" "code=${CODE} status=${RENDER_STATUS} job=${RENDER_JOB}"
fi

# ---- Test 10: render-decreases-available ----------------------------
echo "[test] render-decreases-available: avail dropped by 50"
CODE=$(curl -s -o "${TMP}/quota4.json" -w "%{http_code}" -X GET "${BASE}/api/quota" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
Q4_AVAIL=$(jget "${TMP}/quota4.json" '.available_credits')
Q4_RESV=$(jget "${TMP}/quota4.json" '.reserved_credits')
# After /render: avail dropped by exactly 50 from PRE_RENDER_AVAIL.
if [ "${CODE}" = "200" ] \
    && [ -n "${Q4_AVAIL}" ] \
    && awk "BEGIN { exit !((${PRE_RENDER_AVAIL} - ${Q4_AVAIL}) >= 50) }"; then
    ok "render-decreases-available"
else
    bad "render-decreases-available" "avail dropped by >=50" \
        "code=${CODE} pre=${PRE_RENDER_AVAIL} post=${Q4_AVAIL} resv=${PRE_RENDER_RESV}->${Q4_RESV}"
fi

# ---- Test 11: render-insufficient-402 -------------------------------
# Drain all remaining credits, then /render on a fresh project → 402.
echo "[setup] drain-alice: reserve everything left in available"
CODE=$(curl -s -o "${TMP}/quota_pre_drain.json" -w "%{http_code}" -X GET "${BASE}/api/quota" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
DRAIN_AVAIL=$(jget "${TMP}/quota_pre_drain.json" '.available_credits')
if [ "${CODE}" = "200" ] && [ -n "${DRAIN_AVAIL}" ] && awk "BEGIN { exit !(${DRAIN_AVAIL} > 0) }"; then
    # Reserve the full available balance (round up via integer math).
    DRAIN_AMOUNT=$(awk "BEGIN { printf \"%d\", ${DRAIN_AVAIL} }")
    if [ "${DRAIN_AMOUNT}" -lt 1 ]; then DRAIN_AMOUNT="${DRAIN_AVAIL}"; fi
    curl -s -X POST "${BASE}/api/quota/reserve" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${ALICE_TOKEN}" \
        -H "X-Tenant-Id: ${T1_ID}" \
        -d "{\"amount\":${DRAIN_AMOUNT},\"job_id\":\"jb_drain\"}" \
        -o "${TMP}/drain.json" >/dev/null
fi

# Fresh project for the insufficient-render test.
echo "[setup] create-third-project-vp_render2 (still CREATED, drained tenant)"
curl -s -X POST "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${PRODUCT_ID}\"}" \
    -o "${TMP}/vp_render2.json"
VP_RENDER2_ID=$(jget "${TMP}/vp_render2.json" '.id')

echo "[test] render-insufficient-402: drained tenant, /render → 402"
CODE=$(curl -s -o "${TMP}/render_insuf.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${VP_RENDER2_ID}/render" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
if [ "${CODE}" = "402" ]; then
    ok "render-insufficient-402"
else
    bad "render-insufficient-402" "402" "code=${CODE} body=$(cat "${TMP}/render_insuf.json" 2>/dev/null)"
fi

# ---- Summary ---------------------------------------------------------
echo "=== phase_4 gate done PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} $(date -Iseconds) ==="
if [ "${FAIL_COUNT}" -gt 0 ]; then exit 1; fi
exit 0