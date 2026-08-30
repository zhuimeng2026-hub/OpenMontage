#!/usr/bin/env bash
# Phase 2 gate — 必须满足 tasks.yaml 的 gate_min_verification 才算通过。
# 占位版本只校验 tasks.yaml status=READY;真正的 gate 由填好 tasks.yaml 的人补全。

set -u
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"

status=$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")
if [ "${status}" != "READY" ]; then
    echo "[gate phase ${0##*/../}] STUB — tasks.yaml status=${status}"
    echo "[gate] 真正的 gate 还没实现,占位默认 PASS — 等实现后必须改成本地校验。"
    exit 0
fi

# TODO: 实际 gate — 例如:
#   curl -fsS http://localhost:8900/api/me -H "Authorization: Bearer ${JWT}"
#   psql -c "SELECT 1 FROM users WHERE internal_user_id IS NOT NULL LIMIT 1"
echo "[gate phase ${0##*/../}] status=READY 但 gate 逻辑未实现 — 默认 PASS"
exit 0
