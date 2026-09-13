#!/usr/bin/env bash
# Phase 3 gate — §17.D (Project / Job 管理 + 13 档状态机) smoke test.
#
# Invoked by phase_3/run.sh (server already up on :18904) but is also
# runnable standalone: if no server is listening on :18904 it launches
# the MVP binary itself, runs the scenarios, then tears it down on EXIT.
#
# Phase 3 sits on top of Phase 1 (tenants) + Phase 2 (products/assets), so
# setup replays those two phases first: login Alice + Bob, create T1 + T2,
# add Bob to T1, create a product in T1 and upload one asset to it.
#
# Each scenario prints `PASS <name>` or `FAIL <name> expected=X got=Y`.
# Exits 0 if all PASS, 1 if any FAIL.

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
LOG_DIR="/opt/OpenMontage_Voicebox/logs/mvp_dev"
mkdir -p "${LOG_DIR}"
GATE_LOG="${LOG_DIR}/gate-phase_3-$(date +%Y%m%d-%H%M%S).log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_3 gate start $(date -Iseconds) ==="

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

BIN="/tmp/frameflow-bff-mvp-p3"
if [ ! -x "${BIN}" ]; then
    echo "[gate] FAIL: ${BIN} not built — run.sh must run first"
    exit 1
fi

PORT="${MVP_PORT:-18904}"
BASE="http://127.0.0.1:${PORT}"
export WEIXIN_MOCK_AUTH=1
export MVP_PORT="${PORT}"
DB_PATH="${MVP_DB_PATH:-/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db}"
export MVP_DB_PATH="${DB_PATH}"

OWN_PID=""  # only set if we launched the binary in this gate run

# kill any stale binary from a previous failed run (matches run.sh behaviour)
pkill -f "frameflow-bff-mvp-p3" 2>/dev/null || true
sleep 0.3

# Reuse an already-running server (e.g. launched by run.sh) or start one.
if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
    echo "[gate] server already up on :${PORT} — reusing"
else
    echo "[gate] launching ${BIN} on :${PORT}"
    "${BIN}" > "${LOG_DIR}/phase_3-gate-server.log" 2>&1 &
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
        tail -n 30 "${LOG_DIR}/phase_3-gate-server.log" 2>&1
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

# ---- 13-state lifecycle helpers (§17.G) ------------------------------
# status_rank <STATUS> → integer position in the lifecycle, or -1 for
# terminal/unknown states (FAILED / CANCELLED have no lifecycle position).
status_rank () {
    case "$1" in
        CREATED)             echo 0  ;;
        ASSET_ANALYZING)     echo 1  ;;
        REFERENCE_ANALYZING) echo 2  ;;
        PLANNING)            echo 3  ;;
        STORYBOARD_READY)    echo 4  ;;
        ANIMATIC_RENDERING)  echo 5  ;;
        ANIMATIC_READY)      echo 6  ;;
        SAMPLE_RENDERING)    echo 7  ;;
        SAMPLE_READY)        echo 8  ;;
        WAITING_APPROVAL)    echo 9  ;;
        FINAL_RENDERING)     echo 10 ;;
        COMPLETED)           echo 11 ;;
        FAILED)              echo -1 ;;
        CANCELLED)           echo -1 ;;
        *)                   echo -1 ;;
    esac
}

# is_monotonic <S1> <S2> ... → 0 if the sequence strictly ascends through
# the lifecycle (and every element is a known non-terminal state).
is_monotonic () {
    local prev=-1 cur
    for s in "$@"; do
        cur="$(status_rank "${s}")"
        if [ "${cur}" -lt 0 ]; then
            echo "[monotonic] unknown/terminal status in sequence: ${s}"
            return 1
        fi
        if [ "${cur}" -le "${prev}" ]; then
            echo "[monotonic] not strictly ascending at ${s} (rank=${cur} prev=${prev})"
            return 1
        fi
        prev="${cur}"
    done
    return 0
}

# project_status <project_id> → prints current status via GET /status
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

TMP=/tmp/gate_p3.$$
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
    -d "{\"name\":\"Alice Studio P3 ${STAMP}\"}" -o "${TMP}/t1.json"
T1_ID=$(jget "${TMP}/t1.json" '.id')

echo "[setup] create-t2 (Bob owns)"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -d "{\"name\":\"Bob Studio P3 ${STAMP}\"}" -o "${TMP}/t2.json"
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
    -d "{\"name\":\"Travel Mug P3 ${STAMP}\",\"category\":\"kitchenware\",\"sku\":\"TM-P3-${STAMP}\"}" \
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

# ---- Test 1: create-project-200 --------------------------------------
echo "[test] create-project-200: Alice POST /api/video-projects"
CODE=$(curl -s -o "${TMP}/project.json" -w "%{http_code}" -X POST "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${PRODUCT_ID}\"}")
PROJECT_ID=$(jget "${TMP}/project.json" '.id')
PROJ_STATUS=$(jget "${TMP}/project.json" '.status')
PROJ_TENANT=$(jget "${TMP}/project.json" '.tenant_id')
PROJ_PRODUCT=$(jget "${TMP}/project.json" '.product_id')
if [ "${CODE}" = "200" ] \
    && [ -n "${PROJECT_ID}" ] \
    && [ "${PROJ_STATUS}" = "CREATED" ] \
    && [ "${PROJ_TENANT}" = "${T1_ID}" ]; then
    ok "create-project-200"
else
    bad "create-project-200" "200 status=CREATED tenant=${T1_ID}" \
        "${CODE} (id=${PROJECT_ID} status=${PROJ_STATUS} tenant=${PROJ_TENANT} product=${PROJ_PRODUCT})"
fi

if [ -z "${PROJECT_ID}" ]; then
    echo "[gate] FAIL: no PROJECT_ID — remaining scenarios cannot run"
    echo "=== phase_3 gate done PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} $(date -Iseconds) ==="
    exit 1
fi

# ---- Test 2: get-project-200 -----------------------------------------
echo "[test] get-project-200: Alice GET /api/video-projects/${PROJECT_ID}"
CODE=$(curl -s -o "${TMP}/project_get.json" -w "%{http_code}" -X GET \
    "${BASE}/api/video-projects/${PROJECT_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
GET_PRODUCT=$(jget "${TMP}/project_get.json" '.product_id')
GET_TENANT=$(jget "${TMP}/project_get.json" '.tenant_id')
GET_STATUS=$(jget "${TMP}/project_get.json" '.status')
if [ "${CODE}" = "200" ] \
    && [ "${GET_PRODUCT}" = "${PRODUCT_ID}" ] \
    && [ "${GET_TENANT}" = "${T1_ID}" ]; then
    ok "get-project-200"
else
    bad "get-project-200" "200 product_id=${PRODUCT_ID}" \
        "${CODE} (product=${GET_PRODUCT} tenant=${GET_TENANT} status=${GET_STATUS})"
fi

# ---- Test 3: update-brief-200 ----------------------------------------
echo "[test] update-brief-200: PUT /api/video-projects/${PROJECT_ID}/brief"
CODE=$(curl -s -o "${TMP}/brief.json" -w "%{http_code}" -X PUT \
    "${BASE}/api/video-projects/${PROJECT_ID}/brief" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"creative_brief":{"goal":"lead_generation","duration":20},"reference_mode":"balanced"}')
BRIEF_MODE=$(jget "${TMP}/brief.json" '.reference_mode')
if [ "${CODE}" = "200" ]; then
    ok "update-brief-200"
else
    bad "update-brief-200" "200" "${CODE} (reference_mode=${BRIEF_MODE})"
fi

# ---- Test 4: set-reference-200 ---------------------------------------
echo "[test] set-reference-200: POST /api/video-projects/${PROJECT_ID}/reference"
CODE=$(curl -s -o "${TMP}/reference.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${PROJECT_ID}/reference" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"file_key\":\"${HERO_FILE_KEY}\"}")
REF_KEY=$(jget "${TMP}/reference.json" '.reference_file_key')
if [ "${CODE}" = "200" ]; then
    ok "set-reference-200"
else
    bad "set-reference-200" "200" "${CODE} (reference_file_key=${REF_KEY})"
fi

# ---- Test 5: start-storyboard-200 ------------------------------------
echo "[test] start-storyboard-200: POST /api/video-projects/${PROJECT_ID}/storyboard"
CODE=$(curl -s -o "${TMP}/storyboard.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${PROJECT_ID}/storyboard" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
SB_STATUS=$(jget "${TMP}/storyboard.json" '.status')
JOB_ID=$(jget "${TMP}/storyboard.json" '.job_id')
if [ -z "${JOB_ID}" ]; then JOB_ID=$(jget "${TMP}/storyboard.json" '.job.id'); fi
if [ -z "${JOB_ID}" ]; then JOB_ID=$(jget "${TMP}/storyboard.json" '.id'); fi
if [ "${CODE}" = "200" ] && [ "${SB_STATUS}" = "STORYBOARD_READY" ] && [ -n "${JOB_ID}" ]; then
    ok "start-storyboard-200"
else
    bad "start-storyboard-200" "200 status=STORYBOARD_READY job_id present" \
        "${CODE} (status=${SB_STATUS} job_id=${JOB_ID})"
fi

# ---- Test 6: get-status-after-storyboard -----------------------------
echo "[test] get-status-after-storyboard: GET /api/video-projects/${PROJECT_ID}/status"
CODE=$(curl -s -o "${TMP}/status_sb.json" -w "%{http_code}" -X GET \
    "${BASE}/api/video-projects/${PROJECT_ID}/status" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
S1=$(jget "${TMP}/status_sb.json" '.status')
S1_PROJECT=$(jget "${TMP}/status_sb.json" '.project_id')
S1_UPDATED=$(jget "${TMP}/status_sb.json" '.updated_at')
if [ "${CODE}" = "200" ] \
    && [ "${S1}" = "STORYBOARD_READY" ] \
    && [ "${S1_PROJECT}" = "${PROJECT_ID}" ] \
    && [ -n "${S1_UPDATED}" ]; then
    ok "get-status-after-storyboard"
else
    bad "get-status-after-storyboard" "200 status=STORYBOARD_READY" \
        "${CODE} (status=${S1} project_id=${S1_PROJECT} updated_at=${S1_UPDATED})"
fi

# ---- Test 7: get-job-200 ---------------------------------------------
echo "[test] get-job-200: GET /api/jobs/${JOB_ID}"
CODE=$(curl -s -o "${TMP}/job.json" -w "%{http_code}" -X GET "${BASE}/api/jobs/${JOB_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
JOB_TYPE=$(jget "${TMP}/job.json" '.job_type')
JOB_TENANT=$(jget "${TMP}/job.json" '.tenant_id')
JOB_PROJECT=$(jget "${TMP}/job.json" '.video_project_id')
JOB_STATUS=$(jget "${TMP}/job.json" '.status')
if [ "${CODE}" = "200" ] \
    && [ "${JOB_TYPE}" = "storyboard" ] \
    && [ "${JOB_TENANT}" = "${T1_ID}" ] \
    && [ "${JOB_PROJECT}" = "${PROJECT_ID}" ]; then
    ok "get-job-200"
else
    bad "get-job-200" "200 job_type=storyboard tenant=${T1_ID}" \
        "${CODE} (job_type=${JOB_TYPE} tenant=${JOB_TENANT} project=${JOB_PROJECT} status=${JOB_STATUS})"
fi

# ---- Test 8: start-animatic-200 --------------------------------------
echo "[test] start-animatic-200: POST /api/video-projects/${PROJECT_ID}/animatic"
CODE=$(curl -s -o "${TMP}/animatic.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${PROJECT_ID}/animatic" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
AN_STATUS=$(jget "${TMP}/animatic.json" '.status')
ANIMATIC_JOB_ID=$(jget "${TMP}/animatic.json" '.job_id')
if [ -z "${ANIMATIC_JOB_ID}" ]; then ANIMATIC_JOB_ID=$(jget "${TMP}/animatic.json" '.job.id'); fi
if [ -z "${ANIMATIC_JOB_ID}" ]; then ANIMATIC_JOB_ID=$(jget "${TMP}/animatic.json" '.id'); fi
if [ "${CODE}" = "200" ] && [ "${AN_STATUS}" = "ANIMATIC_RENDERING" ] && [ -n "${ANIMATIC_JOB_ID}" ]; then
    ok "start-animatic-200"
else
    bad "start-animatic-200" "200 status=ANIMATIC_RENDERING job_id present" \
        "${CODE} (status=${AN_STATUS} job_id=${ANIMATIC_JOB_ID})"
fi

# ---- Test 9: wait-animatic-done --------------------------------------
echo "[test] wait-animatic-done: runner should reach ANIMATIC_READY (~400ms)"
S2=$(wait_status "${PROJECT_ID}" "ANIMATIC_READY" 20)
if [ "${S2}" = "ANIMATIC_READY" ]; then
    ok "wait-animatic-done"
else
    bad "wait-animatic-done" "ANIMATIC_READY" "${S2}"
fi

# ---- Test 10: start-sample-200 ---------------------------------------
echo "[test] start-sample-200: POST /api/video-projects/${PROJECT_ID}/sample"
CODE=$(curl -s -o "${TMP}/sample.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${PROJECT_ID}/sample" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
SA_STATUS=$(jget "${TMP}/sample.json" '.status')
SAMPLE_JOB_ID=$(jget "${TMP}/sample.json" '.job_id')
if [ -z "${SAMPLE_JOB_ID}" ]; then SAMPLE_JOB_ID=$(jget "${TMP}/sample.json" '.job.id'); fi
if [ -z "${SAMPLE_JOB_ID}" ]; then SAMPLE_JOB_ID=$(jget "${TMP}/sample.json" '.id'); fi
if [ "${CODE}" = "200" ] && [ "${SA_STATUS}" = "SAMPLE_RENDERING" ]; then
    ok "start-sample-200"
else
    bad "start-sample-200" "200 status=SAMPLE_RENDERING" \
        "${CODE} (status=${SA_STATUS} job_id=${SAMPLE_JOB_ID})"
fi

# ---- Test 11: wait-sample-done ---------------------------------------
echo "[test] wait-sample-done: runner should reach SAMPLE_READY (~400ms)"
S3=$(wait_status "${PROJECT_ID}" "SAMPLE_READY" 20)
if [ "${S3}" = "SAMPLE_READY" ]; then
    ok "wait-sample-done"
else
    bad "wait-sample-done" "SAMPLE_READY" "${S3}"
fi

# ---- Test 12: start-render-200 ---------------------------------------
echo "[test] start-render-200: POST /api/video-projects/${PROJECT_ID}/render"
CODE=$(curl -s -o "${TMP}/render.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${PROJECT_ID}/render" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
RE_STATUS=$(jget "${TMP}/render.json" '.status')
RENDER_JOB_ID=$(jget "${TMP}/render.json" '.job_id')
if [ -z "${RENDER_JOB_ID}" ]; then RENDER_JOB_ID=$(jget "${TMP}/render.json" '.job.id'); fi
if [ -z "${RENDER_JOB_ID}" ]; then RENDER_JOB_ID=$(jget "${TMP}/render.json" '.id'); fi
if [ "${CODE}" = "200" ] && [ "${RE_STATUS}" = "FINAL_RENDERING" ]; then
    ok "start-render-200"
else
    bad "start-render-200" "200 status=FINAL_RENDERING" \
        "${CODE} (status=${RE_STATUS} job_id=${RENDER_JOB_ID})"
fi

# ---- Test 13: wait-render-done ---------------------------------------
echo "[test] wait-render-done: runner should reach COMPLETED (~400ms)"
S4=$(wait_status "${PROJECT_ID}" "COMPLETED" 20)
if [ "${S4}" = "COMPLETED" ]; then
    ok "wait-render-done"
else
    bad "wait-render-done" "COMPLETED" "${S4}"
fi

# ---- Test 14: monotonic-check ----------------------------------------
# The four observed statuses (tests 6, 9, 11, 13) must strictly ascend
# through the §17.G lifecycle — no back-stepping, no terminal states.
echo "[test] monotonic-check: [${S1}, ${S2}, ${S3}, ${S4}]"
if is_monotonic "${S1}" "${S2}" "${S3}" "${S4}"; then
    ok "monotonic-check"
else
    bad "monotonic-check" "strictly ascending lifecycle" "${S1} -> ${S2} -> ${S3} -> ${S4}"
fi

# ---- Test 15: cross-tenant-403 ---------------------------------------
# Bob scoped to T2 tries to read Alice's project (in T1) → 403.
echo "[test] cross-tenant-403: Bob (T2) GET Alice's project in T1"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET \
    "${BASE}/api/video-projects/${PROJECT_ID}" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T2_ID}")
if [ "${CODE}" = "403" ]; then
    ok "cross-tenant-403"
else
    bad "cross-tenant-403" "403" "${CODE}"
fi

# ---- Test 16: cross-tenant-job-403 -----------------------------------
echo "[test] cross-tenant-job-403: Bob (T2) GET /api/jobs/${JOB_ID}"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "${BASE}/api/jobs/${JOB_ID}" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T2_ID}")
if [ "${CODE}" = "403" ]; then
    ok "cross-tenant-job-403"
else
    bad "cross-tenant-job-403" "403" "${CODE}"
fi

# ---- Test 17: cancel-creates-new-project -----------------------------
# A brand new project P2 is cancelled straight out of CREATED — cancel must
# work from any state and land on the CANCELLED terminal state.
echo "[test] cancel-creates-new-project: create P2 then POST /cancel"
curl -s -X POST "${BASE}/api/video-projects" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"product_id\":\"${PRODUCT_ID}\"}" \
    -o "${TMP}/project2.json"
PROJECT2_ID=$(jget "${TMP}/project2.json" '.id')
CANCEL_CODE=$(curl -s -o "${TMP}/cancel.json" -w "%{http_code}" -X POST \
    "${BASE}/api/video-projects/${PROJECT2_ID}/cancel" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{}')
CANCEL_STATUS=$(jget "${TMP}/cancel.json" '.status')
curl -s -X GET "${BASE}/api/video-projects/${PROJECT2_ID}/status" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/project2_status.json"
CANCEL_STATUS_GET=$(jget "${TMP}/project2_status.json" '.status')
if [ "${CANCEL_CODE}" = "200" ] \
    && [ -n "${PROJECT2_ID}" ] \
    && [ "${CANCEL_STATUS}" = "CANCELLED" ] \
    && [ "${CANCEL_STATUS_GET}" = "CANCELLED" ]; then
    ok "cancel-creates-new-project"
else
    bad "cancel-creates-new-project" "200 status=CANCELLED" \
        "${CANCEL_CODE} (p2=${PROJECT2_ID} body_status=${CANCEL_STATUS} get_status=${CANCEL_STATUS_GET})"
fi

# ---- Summary ---------------------------------------------------------
echo "=== phase_3 gate done PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} $(date -Iseconds) ==="
if [ "${FAIL_COUNT}" -gt 0 ]; then exit 1; fi
exit 0
