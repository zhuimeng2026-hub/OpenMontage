#!/usr/bin/env bash
# Phase 6 gate — verify real MCP preview rendering.
#
# Assumes run.sh has launched:
#   - stub MCP server on :18910
#   - BFF on :18907 with MCP_BASE_URL=http://127.0.0.1:18910/mcp
#     WEIXIN_MOCK_AUTH=1 (so POST /api/auth/login with code=MOCK_* returns a JWT)
#     MVP_DB_PATH pointing at the same sqlite file as previous phases
#
# Tests:
#   1. login + setup (alice + bob + t1 + t2 + product + project)
#   2. storyboard: trigger → poll status → verify artifacts_json.storyboard.scenes
#   3. animatic:   trigger → poll status → verify artifacts_json.animatic.preview_url
#   4. sample:     trigger → poll status → verify artifacts_json.sample.files
#   5. render:     trigger → poll status → verify FAILED + quota refunded (stub render fails intentionally)
#   6. cross-tenant 403 on the production-status verb
#   7. fail-loud 503: launch a second BFF without MCP_BASE_URL, expect 503
set -u
set -o pipefail

BFF="http://127.0.0.1:18907"
GATE_TAG="phase_6-$(date +%Y%m%d-%H%M%S)"
GATE_LOG="/opt/OpenMontage_Voicebox/logs/mvp_dev/gate-${GATE_TAG}.log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_6 gate start $(date -Iseconds) ==="

PASS=0
FAIL=0
ok () { echo "PASS $1"; PASS=$((PASS+1)); }
nok () { echo "FAIL $1 — $2"; FAIL=$((FAIL+1)); }

# ---- helper: login ----
login () {
    local who="$1"
    local code="MOCK_${who}_${RANDOM}"
    local body
    body=$(curl -s -X POST "${BFF}/api/auth/login" -H 'Content-Type: application/json' -d "{\"code\":\"${code}\"}")
    echo "${body}" | grep -q '"token"' && echo "${body}" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" \
        || { echo ""; return 1; }
}

# ---- helper: extract JSON field via python (avoids jq dependency) ----
jget () {
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null
}

# ---- 1. setup ----
echo "=== phase_6 gate: setup ==="
ALICE_JWT=$(login alice)
BOB_JWT=$(login bob)
if [ -z "${ALICE_JWT}" ] || [ -z "${BOB_JWT}" ]; then
    nok "setup-login" "alice='${ALICE_JWT:0:8}' bob='${BOB_JWT:0:8}'"
    echo "=== phase_6 gate done PASS=${PASS} FAIL=${FAIL} ==="
    exit 1
fi
ok "setup-login"

ALICE_TID=$(curl -s -X POST "${BFF}/api/tenants" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H 'Content-Type: application/json' \
    -d '{"name":"Alice Co"}' | jget '["id"]')
BOB_TID=$(curl -s -X POST "${BFF}/api/tenants" \
    -H "Authorization: Bearer ${BOB_JWT}" -H 'Content-Type: application/json' \
    -d '{"name":"Bob Co"}' | jget '["id"]')
if [ -z "${ALICE_TID}" ] || [ -z "${BOB_TID}" ]; then
    nok "setup-tenants" "alice='${ALICE_TID}' bob='${BOB_TID}'"
    exit 1
fi
ok "setup-tenants"

# Add bob to alice's tenant so cross-tenant probe has a contrast
curl -s -X POST "${BFF}/api/tenants/${ALICE_TID}/members" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$(curl -s "${BFF}/api/me/jwt" -H "Authorization: Bearer ${BOB_JWT}" | jget '["internal_user_id"]')\"}" \
    > /dev/null

# Create a product as Alice
PRODUCT_ID=$(curl -s -X POST "${BFF}/api/products" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" \
    -H 'Content-Type: application/json' -d '{"name":"Phase6 Test Product"}' | jget '["id"]')
if [ -z "${PRODUCT_ID}" ]; then
    nok "setup-product" "got empty product id"
    exit 1
fi
ok "setup-product"

# Create a project linked to the product
PROJECT_ID=$(curl -s -X POST "${BFF}/api/video-projects" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" \
    -H 'Content-Type: application/json' -d "{\"product_id\":\"${PRODUCT_ID}\"}" | jget '["id"]')
if [ -z "${PROJECT_ID}" ]; then
    nok "setup-project" "got empty project id"
    exit 1
fi
ok "setup-project"
echo "[setup] T1=${ALICE_TID} T2=${BOB_TID} P1=${PRODUCT_ID} VP1=${PROJECT_ID}"

# Helper: trigger a stage + poll status
# Usage: trigger_stage stage expected_status [max_seconds]
trigger_stage () {
    local stage="$1"
    local want_status="$2"
    local max_s="${3:-90}"

    local resp job_id
    resp=$(curl -s -X POST "${BFF}/api/video-projects/${PROJECT_ID}/${stage}" \
        -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
    job_id=$(echo "${resp}" | jget '["job_id"]')
    local immediate
    immediate=$(echo "${resp}" | jget '["status"]')
    if [ -z "${job_id}" ]; then
        nok "trigger-${stage}" "no job_id; resp=${resp}"
        return 1
    fi
    echo "  [${stage}] job=${job_id} immediate=${immediate}"

    # Poll /status until the project lands on want_status (or terminal FAILED
    # for the render-as-failure case). storyboard reaches its target
    # IMMEDIATELY because there's no STORYBOARD_RENDERING state in §17.G —
    # but we still need to give the runner time to stamp artifacts_json.
    local s=""
    local waited=0
    for i in $(seq 1 $((max_s/2))); do
        s=$(curl -s "${BFF}/api/video-projects/${PROJECT_ID}/status" \
            -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" | jget '["status"]')
        case "${s}" in
            "${want_status}"|"FAILED")
                # Status matches — but for stages with a runner, give the
                # goroutine an extra beat to write artifacts_json.
                if [ "${stage}" = "storyboard" ]; then
                    sleep 1
                else
                    sleep 2
                fi
                break ;;
        esac
        sleep 2
        waited=$((waited + 2))
    done
    if [ "${s}" != "${want_status}" ] && [ "${s}" != "FAILED" ]; then
        nok "trigger-${stage}" "status stayed at '${s}' after ${waited}s; want '${want_status}'"
        return 1
    fi
    echo "  [${stage}] status reached ${s}"

    # Inspect job artifacts
    local job_resp ext_run artifacts
    job_resp=$(curl -s "${BFF}/api/jobs/${job_id}" \
        -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
    ext_run=$(echo "${job_resp}" | jget '["external_run_id"]')
    artifacts=$(echo "${job_resp}" | jget '["artifacts_json"]')
    if [ "${s}" = "FAILED" ]; then
        # render is expected to fail; just confirm error_message is set
        local em
        em=$(echo "${job_resp}" | jget '["error_message"]')
        if [ -z "${em}" ]; then
            nok "trigger-${stage}" "FAILED but no error_message"
            return 1
        fi
        ok "trigger-${stage}-failed-as-expected (external_run_id=${ext_run:0:24})"
        return 0
    fi
    if [ -z "${ext_run}" ]; then
        nok "trigger-${stage}" "external_run_id empty; artifacts=${artifacts}"
        return 1
    fi
    if [ -z "${artifacts}" ] || [ "${artifacts}" = "None" ]; then
        nok "trigger-${stage}" "artifacts_json empty"
        return 1
    fi
    # Per-stage artifact shape check
    case "${stage}" in
        storyboard)
            echo "${artifacts}" | python3 -c "
import json,sys
a=json.loads(sys.stdin.read())
scenes=a.get('scenes') or []
assert len(scenes)>=1, f'expected scenes>=1 got {len(scenes)}'
for s in scenes:
    assert s.get('preview_url'), 'missing preview_url'
print(f'  [storyboard] artifacts.scenes={len(scenes)} OK')
" || { nok "trigger-${stage}" "scenes shape invalid"; return 1; }
            ;;
        animatic)
            echo "${artifacts}" | python3 -c "
import json,sys
a=json.loads(sys.stdin.read())
assert a.get('preview_url'), 'missing preview_url'
assert a.get('duration_seconds'), 'missing duration'
print(f'  [animatic] artifacts.preview_url present OK')
" || { nok "trigger-${stage}" "animatic shape invalid"; return 1; }
            ;;
        sample)
            echo "${artifacts}" | python3 -c "
import json,sys
a=json.loads(sys.stdin.read())
files=a.get('files') or []
assert len(files)>=1, f'expected files>=1 got {len(files)}'
print(f'  [sample] artifacts.files={len(files)} OK')
" || { nok "trigger-${stage}" "sample shape invalid"; return 1; }
            ;;
    esac
    ok "trigger-${stage} (external_run_id=${ext_run:0:24}...)"
}

# ---- 2. storyboard ----
trigger_stage "storyboard" "STORYBOARD_READY" 30

# ---- 3. animatic ----
trigger_stage "animatic" "ANIMATIC_READY" 60

# ---- 4. sample ----
trigger_stage "sample" "SAMPLE_READY" 90

# ---- 5. render — stub intentionally fails; expect FAILED + quota Refund ----
# Get pre-render available_credits
PRE_AVAIL=$(curl -s "${BFF}/api/quota" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" | jget '["available_credits"]')
echo "  [render] pre-reserve available_credits=${PRE_AVAIL}"
trigger_stage "render" "FINAL_RENDERING" 30 || true
# After FAILED, available should be back to pre
POST_AVAIL=$(curl -s "${BFF}/api/quota" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}" | jget '["available_credits"]')
echo "  [render] post-fail available_credits=${POST_AVAIL}"
if [ "${PRE_AVAIL}" = "${POST_AVAIL}" ]; then
    ok "render-quota-refunded (available ${PRE_AVAIL} → ${POST_AVAIL})"
else
    nok "render-quota-refund" "available ${PRE_AVAIL} → ${POST_AVAIL} (should match — render failed, refund required)"
fi

# ---- 6. cross-tenant probe: Bob tries to read Alice's project ----
CROSS_RESP=$(curl -s -o /dev/null -w "%{http_code}" \
    "${BFF}/api/gateway/production-status?project_id=${PROJECT_ID}" \
    -H "Authorization: Bearer ${BOB_JWT}" -H "X-Tenant-Id: ${BOB_TID}")
if [ "${CROSS_RESP}" = "403" ]; then
    ok "cross-tenant-403"
else
    nok "cross-tenant-403" "got HTTP ${CROSS_RESP}"
fi

# ---- 7. fail-loud: spin a second BFF without MCP_BASE_URL, expect 503 on storyboard ----
echo "=== phase_6 gate: 503 fail-loud test ==="
NO_MCP_PORT=18908
# CRITICAL: cd to /tmp so the BFF's config.Load() doesn't pick up
# frameflow/bff/.env (which sets MCP_BASE_URL=http://127.0.0.1:8900/mcp).
# Even with `env -u MCP_BASE_URL`, godotenv.Load() re-reads it from the
# .env file when the BFF starts. Working dir change defeats that.
cd /tmp
env -u MCP_BASE_URL -u MCP_API_TOKEN -u MCP_PROGRESS_URL -u UPSTREAM_MCP_URL \
    PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}" \
    WEIXIN_MOCK_AUTH=1 MVP_PORT=${NO_MCP_PORT} \
    MVP_DB_PATH="/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db" \
    HOME="$HOME" \
    nohup /tmp/frameflow-bff-mvp-p6 > /opt/OpenMontage_Voicebox/logs/mvp_dev/phase_6-nomcp.log 2>&1 &
NOMCP_PID=$!
for i in 1 2 3 4 5 6 7 8; do
    curl -sf "http://127.0.0.1:${NO_MCP_PORT}/healthz" >/dev/null 2>&1 && break || sleep 0.5
done
# Sanity-check: if the no-MCP server logged mcp=ok, something in our env
# injection failed — surface it loudly so we don't silently pass the test.
if grep -q "mcp=ok" /opt/OpenMontage_Voicebox/logs/mvp_dev/phase_6-nomcp.log 2>/dev/null; then
    nok "fail-loud-503" "no-MCP server inherited MCP_BASE_URL (env injection failed); gate is bogus"
    kill ${NOMCP_PID} 2>/dev/null || true
    wait ${NOMCP_PID} 2>/dev/null || true
    echo "=== phase_6 gate done PASS=${PASS} FAIL=${FAIL} ==="
    exit 1
fi
# Reuse Alice's JWT (issued by :18907) — the no-MCP server shares the DB and
# JWT_SECRET so the same token validates, and Alice is already a member of
# ALICE_TID, so TenantScope passes through to the handler's MCP guard.
NOMCP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "http://127.0.0.1:${NO_MCP_PORT}/api/video-projects/${PROJECT_ID}/storyboard" \
    -H "Authorization: Bearer ${ALICE_JWT}" -H "X-Tenant-Id: ${ALICE_TID}")
if [ "${NOMCP_STATUS}" = "503" ]; then
    ok "fail-loud-503 (MCP_BASE_URL unset → 503)"
else
    nok "fail-loud-503" "got HTTP ${NOMCP_STATUS}; expected 503"
fi
kill ${NOMCP_PID} 2>/dev/null || true
wait ${NOMCP_PID} 2>/dev/null || true
cd - >/dev/null

echo "=== phase_6 gate done PASS=${PASS} FAIL=${FAIL} ==="
if [ "${FAIL}" != "0" ]; then
    exit 1
fi
exit 0