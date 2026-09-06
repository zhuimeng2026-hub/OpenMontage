#!/usr/bin/env bash
# Phase 2 gate — §17.C (Product / Asset) smoke test.
#
# Invoked by phase_2/run.sh (server already up on :18903) but is also
# runnable standalone: if no server is listening on :18903 it launches
# the MVP binary itself, runs the 12 scenarios, then tears it down on EXIT.
#
# Each scenario prints `PASS <name>` or `FAIL <name> expected=X got=Y`.
# Exits 0 if all PASS, 1 if any FAIL.

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
LOG_DIR="/opt/OpenMontage_Voicebox/logs/mvp_dev"
mkdir -p "${LOG_DIR}"
GATE_LOG="${LOG_DIR}/gate-phase_2-$(date +%Y%m%d-%H%M%S).log"
exec >> "${GATE_LOG}" 2>&1
echo "=== phase_2 gate start $(date -Iseconds) ==="

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

BIN="/tmp/frameflow-bff-mvp-p2"
if [ ! -x "${BIN}" ]; then
    echo "[gate] FAIL: ${BIN} not built — run.sh must run first"
    exit 1
fi

PORT="${MVP_PORT:-18903}"
BASE="http://127.0.0.1:${PORT}"
export WEIXIN_MOCK_AUTH=1
export MVP_PORT="${PORT}"
DB_PATH="${MVP_DB_PATH:-/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db}"
export MVP_DB_PATH="${DB_PATH}"

OWN_PID=""  # only set if we launched the binary in this gate run

# kill any stale binary from a previous failed run (matches run.sh behaviour)
pkill -f "frameflow-bff-mvp-p2" 2>/dev/null || true
sleep 0.3

# Reuse an already-running server (e.g. launched by run.sh) or start one.
if curl -fsS --max-time 1 "${BASE}/healthz" >/dev/null 2>&1; then
    echo "[gate] server already up on :${PORT} — reusing"
else
    echo "[gate] launching ${BIN} on :${PORT}"
    "${BIN}" > "${LOG_DIR}/phase_2-gate-server.log" 2>&1 &
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
        tail -n 30 "${LOG_DIR}/phase_2-gate-server.log" 2>&1
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

TMP=/tmp/gate_p2.$$
mkdir -p "${TMP}"
trap 'rm -rf "${TMP}"; cleanup' EXIT

# Prepare fake jpeg-like bytes for asset uploads.
echo "fake jpeg bytes for hero_01"     > "${TMP}/hero_01.jpg"
echo "fake jpeg bytes for detail_01"   > "${TMP}/detail_01.jpg"
echo "fake jpeg bytes for random"      > "${TMP}/random.jpg"
echo "fake jpeg bytes for manual"      > "${TMP}/manual.jpg"

# ---- Setup: login Alice + Bob and create T1, T2 (idempotent) --------
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
    -d '{"name":"Alice Studio P2"}' -o "${TMP}/t1.json"
T1_ID=$(jget "${TMP}/t1.json" '.id')

echo "[setup] create-t2 (Bob owns)"
curl -s -X POST "${BASE}/api/tenants" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -d '{"name":"Bob Studio P2"}' -o "${TMP}/t2.json"
T2_ID=$(jget "${TMP}/t2.json" '.id')

# Add Bob as a member of T1 so we can later exercise cross-tenant denial.
echo "[setup] alice-adds-bob-to-t1"
curl -s -X POST "${BASE}/api/tenants/${T1_ID}/members" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d "{\"user_id\":\"${BOB_IU}\"}" >/dev/null

if [ -z "${ALICE_TOKEN}" ] || [ -z "${BOB_TOKEN}" ] || [ -z "${T1_ID}" ] || [ -z "${T2_ID}" ]; then
    echo "[gate] FAIL: setup did not produce tokens/tenants — aborting"
    exit 1
fi

# ---- Test 1: create-product-200 --------------------------------------
echo "[test] create-product-200: Alice POST /api/products"
curl -s -X POST "${BASE}/api/products" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"name":"Travel Mug","category":"kitchenware","sku":"TM-001"}' \
    -o "${TMP}/product.json"
PRODUCT_ID=$(jget "${TMP}/product.json" '.id')
PROD_NAME=$(jget "${TMP}/product.json" '.name')
PROD_CAT=$(jget "${TMP}/product.json" '.category')
PROD_SKU=$(jget "${TMP}/product.json" '.sku')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/products" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"name":"Travel Mug","category":"kitchenware","sku":"TM-001"}')
if [ "${CODE}" = "200" ] && [ -n "${PRODUCT_ID}" ] && [ "${PROD_NAME}" = "Travel Mug" ]; then
    ok "create-product-200"
else
    bad "create-product-200" "200" "${CODE} (id=${PRODUCT_ID} name=${PROD_NAME} cat=${PROD_CAT} sku=${PROD_SKU})"
fi

# ---- Test 2: get-product-200 -----------------------------------------
echo "[test] get-product-200: Alice GET /api/products/:product_id"
curl -s -X GET "${BASE}/api/products/${PRODUCT_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/product_get.json"
GET_NAME=$(jget "${TMP}/product_get.json" '.name')
GET_CAT=$(jget "${TMP}/product_get.json" '.category')
GET_SKU=$(jget "${TMP}/product_get.json" '.sku')
GET_TENANT=$(jget "${TMP}/product_get.json" '.tenant_id')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "${BASE}/api/products/${PRODUCT_ID}" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
if [ "${CODE}" = "200" ] && [ "${GET_NAME}" = "Travel Mug" ] && [ "${GET_TENANT}" = "${T1_ID}" ]; then
    ok "get-product-200"
else
    bad "get-product-200" "200" "${CODE} (name=${GET_NAME} tenant=${GET_TENANT})"
fi

# ---- Test 3: upload-asset-hero-200 ----------------------------------
echo "[test] upload-asset-hero-200"
curl -s -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/hero_01.jpg;filename=hero_01.jpg" \
    -o "${TMP}/asset_hero.json"
HERO_ASSET_ID=$(jget "${TMP}/asset_hero.json" '.asset_id')
HERO_FILE_KEY=$(jget "${TMP}/asset_hero.json" '.file_key')
HERO_ROLE=$(jget "${TMP}/asset_hero.json" '.role')
HERO_QS=$(jget "${TMP}/asset_hero.json" '.quality_score')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/hero_01.jpg;filename=hero_01.jpg")
if [ "${CODE}" = "200" ] \
    && [ -n "${HERO_ASSET_ID}" ] \
    && [ -n "${HERO_FILE_KEY}" ] \
    && [[ "${HERO_ROLE}" == *"hero"* ]] \
    && [ -n "${HERO_QS}" ]; then
    ok "upload-asset-hero-200"
else
    bad "upload-asset-hero-200" "200" "${CODE} (role=${HERO_ROLE} qs=${HERO_QS} fk=${HERO_FILE_KEY})"
fi

# ---- Test 4: upload-asset-detail-200 --------------------------------
echo "[test] upload-asset-detail-200"
curl -s -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/detail_01.jpg;filename=detail_01.jpg" \
    -o "${TMP}/asset_detail.json"
DETAIL_ASSET_ID=$(jget "${TMP}/asset_detail.json" '.asset_id')
DETAIL_ROLE=$(jget "${TMP}/asset_detail.json" '.role')
DETAIL_QS=$(jget "${TMP}/asset_detail.json" '.quality_score')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/detail_01.jpg;filename=detail_01.jpg")
if [ "${CODE}" = "200" ] \
    && [ -n "${DETAIL_ASSET_ID}" ] \
    && [[ "${DETAIL_ROLE}" == *"detail"* ]] \
    && [ -n "${DETAIL_QS}" ]; then
    ok "upload-asset-detail-200"
else
    bad "upload-asset-detail-200" "200" "${CODE} (role=${DETAIL_ROLE} qs=${DETAIL_QS})"
fi

# ---- Test 5: upload-asset-uncategorized-200 --------------------------
echo "[test] upload-asset-uncategorized-200"
curl -s -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/random.jpg;filename=random.jpg" \
    -o "${TMP}/asset_random.json"
RANDOM_ROLE=$(jget "${TMP}/asset_random.json" '.role')
RANDOM_QS=$(jget "${TMP}/asset_random.json" '.quality_score')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/random.jpg;filename=random.jpg")
if [ "${CODE}" = "200" ] && [ "${RANDOM_ROLE}" = "unclassified" ] && [ -n "${RANDOM_QS}" ]; then
    ok "upload-asset-uncategorized-200"
else
    bad "upload-asset-uncategorized-200" "role=unclassified" "role=${RANDOM_ROLE} qs=${RANDOM_QS} code=${CODE}"
fi

# ---- Test 6: upload-asset-with-override-200 --------------------------
echo "[test] upload-asset-with-override-200"
curl -s -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/manual.jpg;filename=manual.jpg" \
    -F "role=lifestyle" \
    -F "quality_score=0.95" \
    -o "${TMP}/asset_manual.json"
MANUAL_ASSET_ID=$(jget "${TMP}/asset_manual.json" '.asset_id')
MANUAL_ROLE=$(jget "${TMP}/asset_manual.json" '.role')
MANUAL_QS=$(jget "${TMP}/asset_manual.json" '.quality_score')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -F "file=@${TMP}/manual.jpg;filename=manual.jpg" \
    -F "role=lifestyle" \
    -F "quality_score=0.95")
if [ "${CODE}" = "200" ] && [ "${MANUAL_ROLE}" = "lifestyle" ] && [ "${MANUAL_QS}" = "0.95" ]; then
    ok "upload-asset-with-override-200"
else
    bad "upload-asset-with-override-200" "role=lifestyle qs=0.95" "role=${MANUAL_ROLE} qs=${MANUAL_QS} code=${CODE}"
fi

# ---- Test 7: list-assets-200 -----------------------------------------
echo "[test] list-assets-200"
curl -s -X GET "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/assets_list.json"
ASSETS_COUNT=$(jcount "${TMP}/assets_list.json" '.assets')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
if [ "${CODE}" = "200" ] && [ "${ASSETS_COUNT}" -ge 4 ]; then
    ok "list-assets-200"
else
    bad "list-assets-200" ">=4" "count=${ASSETS_COUNT} code=${CODE}"
fi

# ---- Test 8: get-manifest-200 ----------------------------------------
echo "[test] get-manifest-200"
curl -s -X GET "${BASE}/api/products/${PRODUCT_ID}/manifest" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/manifest_v1.json"
MANIFEST_VERSION=$(jget "${TMP}/manifest_v1.json" '.version')
MANIFEST_ASSETS_COUNT=$(jcount "${TMP}/manifest_v1.json" '.assets')
MANIFEST_FIRST_ROLE=$(jget "${TMP}/manifest_v1.json" '.assets[0].role')
MANIFEST_FIRST_QS=$(jget "${TMP}/manifest_v1.json" '.assets[0].quality_score')
MANIFEST_AI=$(jget "${TMP}/manifest_v1.json" '.ai_model')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "${BASE}/api/products/${PRODUCT_ID}/manifest" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
if [ "${CODE}" = "200" ] \
    && [ "${MANIFEST_VERSION}" -ge 1 ] \
    && [ "${MANIFEST_ASSETS_COUNT}" -ge 1 ] \
    && [ -n "${MANIFEST_FIRST_ROLE}" ] \
    && [ -n "${MANIFEST_FIRST_QS}" ] \
    && [ -n "${MANIFEST_AI}" ]; then
    ok "get-manifest-200"
else
    bad "get-manifest-200" "version>=1, assets>=1, role+qs present" "version=${MANIFEST_VERSION} count=${MANIFEST_ASSETS_COUNT} role=${MANIFEST_FIRST_ROLE} qs=${MANIFEST_FIRST_QS} ai=${MANIFEST_AI} code=${CODE}"
fi

# ---- Test 9: correct-asset-200 ---------------------------------------
# Pick the hero asset (or whatever was uploaded first) — easiest: correct HERO_ASSET_ID.
echo "[test] correct-asset-200: PUT manifest/${HERO_ASSET_ID} role=detail qs=0.99"
PUT_CODE=$(curl -s -o "${TMP}/correct.json" -w "%{http_code}" -X PUT \
    "${BASE}/api/products/${PRODUCT_ID}/manifest/${HERO_ASSET_ID}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -d '{"role":"detail","quality_score":0.99}')
CORRECT_ASSET_ID=$(jget "${TMP}/correct.json" '.asset_id')
CORRECT_ROLE=$(jget "${TMP}/correct.json" '.role')
CORRECT_QS=$(jget "${TMP}/correct.json" '.quality_score')
if [ "${PUT_CODE}" = "200" ] && [ -n "${CORRECT_ASSET_ID}" ]; then
    ok "correct-asset-200"
else
    bad "correct-asset-200" "200" "${PUT_CODE} (asset_id=${CORRECT_ASSET_ID} role=${CORRECT_ROLE} qs=${CORRECT_QS})"
fi

# ---- Test 10: manifest-after-correct-200 -----------------------------
echo "[test] manifest-after-correct-200: version+1, hero asset now role=detail qs=0.99"
curl -s -X GET "${BASE}/api/products/${PRODUCT_ID}/manifest" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}" \
    -o "${TMP}/manifest_v2.json"
MANIFEST_V2_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "${BASE}/api/products/${PRODUCT_ID}/manifest" \
    -H "Authorization: Bearer ${ALICE_TOKEN}" \
    -H "X-Tenant-Id: ${T1_ID}")
MANIFEST_VERSION_2=$(jget "${TMP}/manifest_v2.json" '.version')
# Find the corrected asset by asset_id in the assets array.
CORRECTED_ROLE=""
CORRECTED_QS=""
if command -v jq >/dev/null 2>&1; then
    CORRECTED_ROLE=$(jq -r --arg aid "${HERO_ASSET_ID}" '.assets[] | select(.asset_id == $aid) | .role' "${TMP}/manifest_v2.json")
    CORRECTED_QS=$(jq -r --arg aid "${HERO_ASSET_ID}" '.assets[] | select(.asset_id == $aid) | .quality_score' "${TMP}/manifest_v2.json")
else
    CORRECTED_ROLE=$(python3 - "${TMP}/manifest_v2.json" "${HERO_ASSET_ID}" <<'PYEOF'
import json, sys
fp, aid = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(fp))
except Exception:
    print(''); sys.exit(0)
for a in d.get('assets', []):
    if a.get('asset_id') == aid:
        print(a.get('role', '')); sys.exit(0)
print('')
PYEOF
)
    CORRECTED_QS=$(python3 - "${TMP}/manifest_v2.json" "${HERO_ASSET_ID}" <<'PYEOF'
import json, sys
fp, aid = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(fp))
except Exception:
    print(''); sys.exit(0)
for a in d.get('assets', []):
    if a.get('asset_id') == aid:
        print(a.get('quality_score', '')); sys.exit(0)
print('')
PYEOF
)
fi
VERSION_DELTA=$((MANIFEST_VERSION_2 - MANIFEST_VERSION))
if [ "${MANIFEST_V2_CODE}" = "200" ] && [ "${VERSION_DELTA}" = "1" ] \
    && [ "${CORRECTED_ROLE}" = "detail" ] \
    && [ "${CORRECTED_QS}" = "0.99" ]; then
    ok "manifest-after-correct-200"
else
    bad "manifest-after-correct-200" "version+1, role=detail, qs=0.99" "v1=${MANIFEST_VERSION} v2=${MANIFEST_VERSION_2} delta=${VERSION_DELTA} role=${CORRECTED_ROLE} qs=${CORRECTED_QS} code=${MANIFEST_V2_CODE}"
fi

# ---- Test 11: cross-tenant-product-403 -------------------------------
# Bob (X-Tenant-Id=T2) tries to GET Alice's product (in T1) — TenantScope
# says Bob is not in T1 → 403.
echo "[test] cross-tenant-product-403: Bob (T2) GET Alice's product in T1"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "${BASE}/api/products/${PRODUCT_ID}" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T2_ID}")
if [ "${CODE}" = "403" ]; then
    ok "cross-tenant-product-403"
else
    bad "cross-tenant-product-403" "403" "${CODE}"
fi

# ---- Test 12: cross-tenant-upload-403 --------------------------------
# Bob (T2) tries to upload to Alice's product in T1 — 403.
echo "[test] cross-tenant-upload-403: Bob (T2) POST /api/products/:id/assets for T1 product"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/products/${PRODUCT_ID}/assets" \
    -H "Authorization: Bearer ${BOB_TOKEN}" \
    -H "X-Tenant-Id: ${T2_ID}" \
    -F "file=@${TMP}/random.jpg;filename=random.jpg")
if [ "${CODE}" = "403" ]; then
    ok "cross-tenant-upload-403"
else
    bad "cross-tenant-upload-403" "403" "${CODE}"
fi

# ---- Summary ---------------------------------------------------------
echo "=== phase_2 gate done PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} $(date -Iseconds) ==="
if [ "${FAIL_COUNT}" -gt 0 ]; then exit 1; fi
exit 0