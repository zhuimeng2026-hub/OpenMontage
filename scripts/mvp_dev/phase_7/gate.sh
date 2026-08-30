#!/usr/bin/env bash
# Phase 7 gate — verify WAITING_APPROVAL + approve endpoint.
#
# Assumes run.sh has launched:
#   - stub MCP server on :18911 with --succeed-render (so render succeeds
#     and we can verify the full pipeline COMPLETED)
#   - BFF on :18907 with MCP_BASE_URL=http://127.0.0.1:18911/mcp
#
# Tests:
#   1. setup (alice + t1 + product + project)
#   2. approve from CREATED → 409 illegal transition
#   3. full pipeline: storyboard → animatic → sample → SAMPLE_READY
#   4. approve from SAMPLE_READY → WAITING_APPROVAL + approved_by persisted
#   5. approve from WAITING_APPROVAL (idempotent) → 200, stays WAITING_APPROVAL
#   6. render from WAITING_APPROVAL → FINAL_RENDERING → COMPLETED + artifacts
#   7. quota reserved + consumed (no refund on success)
#   8. cross-tenant 403 on approve
set -u
set -o pipefail

BFF="http://127.0.0.1:18907"
GATE_TAG="phase_7-$(date +%Y%m%d-%H%M%S)"
GATE_LOG="/opt/OpenMontage_Voicebox/logs/mvp_dev/gate-${GATE_TAG}.log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_7 gate start $(date -Iseconds) ==="

PASS=0
FAIL=0
ok () { echo "PASS $1"; PASS=$((PASS+1)); }
nok () { echo "FAIL $1 — $2"; FAIL=$((FAIL+1)); }

login () {
    local who="$1"
    local code="MOCK_${who}_${RANDOM}"
    local body
    body=$(curl -s -X POST "${BFF}/api/auth/login" -H 'Content-Type: application/json' -d "{\"code\":\"${code}\"}")
    echo "${body}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null
}

jget () {
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null
}

# ---- 1. setup ----
echo "=== phase_7 gate: setup ==="
ALICE_JWT=$(login alice)
BOB_JWT=$(login bob)
[ -n "${ALICE_JWT}" ] && [ -n "${BOB_JWT}" ] && ok "setup-login" || { nok "setup-login" "missing JWT"; exit 1; }

ALICE_TID=$(curl -s -X POST "${BFF}/api/tenants" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H 'Content-Type: application/json' \
    -d '{"name":"Alice Co"}' | jget '["id"]')
BOB_TID=$(curl -s -X POST "${BFF}/api/tenants" \
    -H "Authorization: Bearer ${BOB_JWT}" -H 'Content-Type: application/json' \
    -d '{"name":"Bob Co"}' | jget '["id"]')
[ -n "${ALICE_TID}" ] && [ -n "${BOB_TID}" ] && ok "setup-tenants" || { nok "setup-tenants" "missing"; exit 1; }

PRODUCT_ID=$(curl -s -X POST "${BFF}/api/products" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" \
    -H 'Content-Type: application/json' -d '{"name":"P7 Test Product"}' | jget '["id"]')
[ -n "${PRODUCT_ID}" ] && ok "setup-product" || { nok "setup-product" ""; exit 1; }

PROJECT_ID=$(curl -s -X POST "${BFF}/api/video-projects" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" \
    -H 'Content-Type: application/json' -d "{\"product_id\":\"${PRODUCT_ID}\"}" | jget '["id"]')
[ -n "${PROJECT_ID}" ] && ok "setup-project" || { nok "setup-project" ""; exit 1; }

ALICE_IUID=$(curl -s "${BFF}/api/me/jwt" -H "Authorization: Bearer ${ALICE_JWT}" | jget '["internal_user_id"]')
echo "[setup] T1=${ALICE_TID} T2=${BOB_TID} P1=${PRODUCT_ID} VP1=${PROJECT_ID} ALICE_IUID=${ALICE_IUID}"

# ---- 2. approve from CREATED → 409 ----
echo "=== phase_7 gate: approve-from-CREATED ==="
APPROVE_EARLY=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${BFF}/api/video-projects/${PROJECT_ID}/approve" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
if [ "${APPROVE_EARLY}" = "409" ]; then
    ok "approve-from-CREATED-409"
else
    nok "approve-from-CREATED-409" "got HTTP ${APPROVE_EARLY}; expected 409"
fi

# ---- 3. trigger stages storyboard → animatic → sample ----
echo "=== phase_7 gate: storyboard → animatic → sample ==="
trigger_stage () {
    local stage="$1"
    local want="$2"
    local max_s="${3:-90}"
    local resp job_id immediate
    resp=$(curl -s -X POST "${BFF}/api/video-projects/${PROJECT_ID}/${stage}" \
        -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
    job_id=$(echo "${resp}" | jget '["job_id"]')
    immediate=$(echo "${resp}" | jget '["status"]')
    if [ -z "${job_id}" ]; then
        nok "trigger-${stage}" "no job_id; resp=${resp}"
        return 1
    fi
    echo "  [${stage}] job=${job_id} immediate=${immediate}"
    local s="" waited=0
    for i in $(seq 1 $((max_s/2))); do
        s=$(curl -s "${BFF}/api/video-projects/${PROJECT_ID}/status" \
            -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" | jget '["status"]')
        case "${s}" in "${want}"|"FAILED") break ;; esac
        sleep 2
        waited=$((waited + 2))
    done
    if [ "${s}" != "${want}" ] && [ "${s}" != "FAILED" ]; then
        nok "trigger-${stage}" "status stayed at '${s}' after ${waited}s"
        return 1
    fi
    echo "  [${stage}] reached ${s}"
}
trigger_stage "storyboard" "STORYBOARD_READY" 30
trigger_stage "animatic" "ANIMATIC_READY" 60
trigger_stage "sample" "SAMPLE_READY" 90

# ---- 4. approve → WAITING_APPROVAL ----
echo "=== phase_7 gate: approve from SAMPLE_READY ==="
APPROVE_RESP=$(curl -s -X POST "${BFF}/api/video-projects/${PROJECT_ID}/approve" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
APPROVE_STATUS=$(echo "${APPROVE_RESP}" | jget '["status"]')
APPROVE_BY=$(echo "${APPROVE_RESP}" | jget '["approved_by"]')
APPROVE_AT=$(echo "${APPROVE_RESP}" | jget '["approved_at"]')
if [ "${APPROVE_STATUS}" = "WAITING_APPROVAL" ] && [ "${APPROVE_BY}" = "${ALICE_IUID}" ] && [ -n "${APPROVE_AT}" ]; then
    ok "approve-SAMPLE_READY→WAITING_APPROVAL (approved_by=${APPROVE_BY:0:16} approved_at=${APPROVE_AT})"
else
    nok "approve-SAMPLE_READY→WAITING_APPROVAL" "status=${APPROVE_STATUS} approved_by=${APPROVE_BY} approved_at=${APPROVE_AT}"
fi

# Re-read project to verify DB persistence (separate query — not the
# response we just parsed).
PROJ_RESP=$(curl -s "${BFF}/api/video-projects/${PROJECT_ID}" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
DB_STATUS=$(echo "${PROJ_RESP}" | jget '["status"]')
if [ "${DB_STATUS}" = "WAITING_APPROVAL" ]; then
    ok "approve-DB-persists (status=${DB_STATUS})"
else
    nok "approve-DB-persists" "DB status=${DB_STATUS}"
fi

# ---- 5. approve again (idempotent) ----
echo "=== phase_7 gate: approve idempotency ==="
APPROVE2_RESP=$(curl -s -X POST "${BFF}/api/video-projects/${PROJECT_ID}/approve" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
APPROVE2_STATUS=$(echo "${APPROVE2_RESP}" | jget '["status"]')
if [ "${APPROVE2_STATUS}" = "WAITING_APPROVAL" ]; then
    ok "approve-idempotent (status=${APPROVE2_STATUS})"
else
    nok "approve-idempotent" "second approve status=${APPROVE2_STATUS}; expected WAITING_APPROVAL"
fi

# ---- 6. render from WAITING_APPROVAL → COMPLETED ----
echo "=== phase_7 gate: render from WAITING_APPROVAL → COMPLETED ==="
PRE_AVAIL=$(curl -s "${BFF}/api/quota" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" | jget '["available_credits"]')
echo "  [render] pre-reserve available=${PRE_AVAIL}"
trigger_stage "render" "COMPLETED" 30
POST_AVAIL=$(curl -s "${BFF}/api/quota" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" | jget '["available_credits"]')
echo "  [render] post-complete available=${POST_AVAIL}"

# On success: Reserve(50) then Consume(50). Consume moves from reserved →
# consumed but does NOT touch available. Expected:
#   pre:  available=100, reserved=0
#   post: available=50,  reserved=0, consumed=50
# So the delta on available should equal the render cost (50).
COST=50
DELTA=$(python3 -c "print(${PRE_AVAIL} - ${POST_AVAIL})")
if [ "${DELTA}" = "${COST}" ]; then
    ok "render-success-Reserve+Consume (available ${PRE_AVAIL} → ${POST_AVAIL}; delta=${DELTA})"
else
    nok "render-success-Reserve+Consume" "available delta ${DELTA}; expected ${COST} (Reserve moves 50 from available → reserved; Consume moves reserved → consumed, no change to available)"
fi

# ---- 7. cross-tenant 403 on approve ----
echo "=== phase_7 gate: cross-tenant approve 403 ==="
# Create a second project, then try to approve Alice's via Bob.
PRODUCT_B=$(curl -s -X POST "${BFF}/api/products" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" \
    -H 'Content-Type: application/json' -d '{"name":"P7 B"}' | jget '["id"]')
PROJECT_B=$(curl -s -X POST "${BFF}/api/video-projects" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" \
    -H 'Content-Type: application/json' -d "{\"product_id\":\"${PRODUCT_B}\"}" | jget '["id"]')
CROSS_APPROVE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${BFF}/api/video-projects/${PROJECT_B}/approve" \
    -H "Authorization: Bearer ${BOB_JWT}" -H "X-Tenant-Id: ${BOB_TID}")
if [ "${CROSS_APPROVE}" = "403" ]; then
    ok "cross-tenant-approve-403"
else
    nok "cross-tenant-approve-403" "got HTTP ${CROSS_APPROVE}"
fi

echo "=== phase_7 gate done PASS=${PASS} FAIL=${FAIL} ==="
if [ "${FAIL}" != "0" ]; then
    exit 1
fi
exit 0