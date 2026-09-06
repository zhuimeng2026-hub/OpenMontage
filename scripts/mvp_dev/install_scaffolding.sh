#!/usr/bin/env bash
# scripts/mvp_dev/install_scaffolding.sh
# 一键创建 phase_0..5 占位文件。Idempotent — 已存在的文件不动。
#
# 用法:
#   bash scripts/mvp_dev/install_scaffolding.sh           # 创建缺失的占位
#   bash scripts/mvp_dev/install_scaffolding.sh --force   # 覆盖已有占位(谨慎)

set -u

REPO_ROOT="/opt/OpenMontage_Voicebox"
DEV_DIR="${REPO_ROOT}/scripts/mvp_dev"
LOG_DIR="${REPO_ROOT}/logs/mvp_dev"
STATE_DIR="${LOG_DIR}/state"

FORCE="no"
for arg in "$@"; do
    case "${arg}" in
        --force) FORCE="yes" ;;
        -h|--help)
            echo "usage: $0 [--force]"
            echo "  --force  overwrite placeholder files (only safe before tasks.yaml is filled)"
            exit 0 ;;
        *) echo "unknown arg: ${arg}" >&2; exit 2 ;;
    esac
done

mkdir -p "${DEV_DIR}/phase_0" "${DEV_DIR}/phase_1" "${DEV_DIR}/phase_2" \
         "${DEV_DIR}/phase_3" "${DEV_DIR}/phase_4" "${DEV_DIR}/phase_5" \
         "${STATE_DIR}"

write_if_missing () {
    local path="$1"
    local content="$2"
    if [ -e "${path}" ] && [ "${FORCE}" != "yes" ]; then
        echo "skip: ${path} (exists; use --force to overwrite)"
        return
    fi
    cat > "${path}" <<EOF
${content}
EOF
    chmod +x "${path}" 2>/dev/null || true
    echo "wrote: ${path}"
}

# ---- phase metadata (parallel arrays so the loop stays simple) ----
NAMES=(
    "微信身份"
    "多租户 + 文件权限"
    "Product / Asset"
    "Project / Job"
    "Quota / Billing"
    "Agent Gateway + 状态聚合"
)
SECTIONS=(
    "§17.A"
    "§17.B + §17.H"
    "§17.C"
    "§17.D"
    "§17.E"
    "§17.F + §17.G"
)
GATES=(
    "POST /api/auth/login 用合法 code 拿 JWT;GET /api/me 带 JWT 返回 user_id + internal_user_id"
    "跨 tenant 调用任何资源接口返回 403;无 tenant header 返回 401;signed URL 过期拒绝"
    "POST /api/products 创建;POST /api/products/:id/assets 上传;GET /api/products/:id/manifest 拿到 role + quality_score"
    "POST /api/video-projects 创建;POST /api/video-projects/:id/storyboard 启动后 GET .../status 状态机单调推进"
    "reserve 后 available 减少;consume 后 reserved 减少;失败返还 reserved"
    "Agent Gateway 8 个动词都有路由无 404;状态聚合映射覆盖 13 档无 unknown"
)

for i in 0 1 2 3 4 5; do
    phase_dir="${DEV_DIR}/phase_${i}"
    tasks_yaml="${phase_dir}/tasks.yaml"
    run_sh="${phase_dir}/run.sh"
    gate_sh="${phase_dir}/gate.sh"
    readme="${phase_dir}/README.md"

    name="${NAMES[$i]}"
    section="${SECTIONS[$i]}"
    gate_desc="${GATES[$i]}"

    # tasks.yaml
    cat > "${tasks_yaml}.tmp" <<YAML
phase: ${i}
scope: ${section} — ${name}
section_ref: docs/openmontage_product_video_mvp_golang_scope.md#17.1-必须的

status: STUB   # 改成 READY 才允许 orchestrator 实际跑 run.sh
last_edited_by: human

files_to_create: []
files_to_modify: []

sql_migrations: []

go_tests: []

gate_endpoints:
  - "(fill from gate description below)"

gate_min_verification: |
  ${gate_desc}

estimated_changes: "TBD — fill before unstubbing"

notes: |
  (operator: 填完后把 status 改成 READY,run.sh / gate.sh 才会真正执行。)
YAML
    write_if_missing "${tasks_yaml}" "$(cat "${tasks_yaml}.tmp")"
    rm -f "${tasks_yaml}.tmp"

    # run.sh
    cat > "${run_sh}.tmp" <<BASH
#!/usr/bin/env bash
# Phase ${i} — ${section} — ${name}
# 由 orchestrator.sh 调用,带两个参数:mode (--resume|--fresh) + diff_file 路径。
#
# 行为:
#   1. 如果 tasks.yaml status != READY,直接退出 0 — 表示该 phase 还没开工。
#   2. 否则执行本 phase 的实际开发任务(schema 迁移、handler、test 等)。
#   3. 写入 diff_file(\${2})— orchestrator 会把它归档到 logs/mvp_dev/。

set -u
REPO_ROOT="/opt/OpenMontage_Voicebox"
PHASE_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
TASKS="\${PHASE_DIR}/tasks.yaml"
DIFF_FILE="\${2:-/dev/null}"

MODE="\${1:-}"
if [[ "\${MODE}" != "--resume" && "\${MODE}" != "--fresh" ]]; then
    echo "[FATAL] run.sh expects --resume or --fresh as \$1" >&2
    exit 2
fi

# 检查 tasks.yaml 是否已填写
status=\$(grep -E '^status:' "\${TASKS}" | awk '{print \$2}' | tr -d '"' | tr -d "'")
if [ "\${status}" != "READY" ]; then
    echo "[phase \${0##*/phase_}] STUB — tasks.yaml status=\${status}, skipping"
    {
        echo "phase \${0##*/phase_} skipped: status=\${status} (not READY)"
        echo "fill scripts/mvp_dev/\${0##*/}/tasks.yaml then re-run orchestrator"
    } > "\${DIFF_FILE}"
    exit 0
fi

echo "[phase \${0##*/phase_}] running in mode=\${MODE}"
{
    echo "=== phase \${0##*/phase_} diff (mode=\${MODE}) ==="
    echo "started: \$(date -Iseconds)"
    echo "scope: \$(grep '^scope:' \${TASKS})"
    echo ""
    echo "[TODO] 替换本行为实际的代码改动逻辑:"
    echo "  - 执行 sql_migrations"
    echo "  - 创建/修改 files_to_create / files_to_modify 列出的 Go 文件"
    echo "  - 跑 go_tests 列出的测试"
} > "\${DIFF_FILE}"

# TODO: 实际执行 — 由填好 tasks.yaml 的人实现。
# 这里是占位,exit 0 让 orchestrator 走 gate 检查。
exit 0
BASH
    write_if_missing "${run_sh}" "$(cat "${run_sh}.tmp")"
    rm -f "${run_sh}.tmp"

    # gate.sh
    cat > "${gate_sh}.tmp" <<BASH
#!/usr/bin/env bash
# Phase ${i} gate — 必须满足 tasks.yaml 的 gate_min_verification 才算通过。
# 占位版本只校验 tasks.yaml status=READY;真正的 gate 由填好 tasks.yaml 的人补全。

set -u
PHASE_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
TASKS="\${PHASE_DIR}/tasks.yaml"

status=\$(grep -E '^status:' "\${TASKS}" | awk '{print \$2}' | tr -d '"' | tr -d "'")
if [ "\${status}" != "READY" ]; then
    echo "[gate phase \${0##*/../}] STUB — tasks.yaml status=\${status}"
    echo "[gate] 真正的 gate 还没实现,占位默认 PASS — 等实现后必须改成本地校验。"
    exit 0
fi

# TODO: 实际 gate — 例如:
#   curl -fsS http://localhost:8900/api/me -H "Authorization: Bearer \${JWT}"
#   psql -c "SELECT 1 FROM users WHERE internal_user_id IS NOT NULL LIMIT 1"
echo "[gate phase \${0##*/../}] status=READY 但 gate 逻辑未实现 — 默认 PASS"
exit 0
BASH
    write_if_missing "${gate_sh}" "$(cat "${gate_sh}.tmp")"
    rm -f "${gate_sh}.tmp"

    # README.md(简化版 — 详细范围说明靠链接,避免 heredoc 内部 grep/§展开踩坑)
    cat > "${readme}.tmp" <<README
# Phase ${i} — ${section} — ${name}

配套计划:\`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md\` §2
范围文档:\`docs/openmontage_product_video_mvp_golang_scope.md\` §17.1(${section})

## Gate 最小验证

${gate_desc}

## 开工步骤

1. 编辑 \`tasks.yaml\`:
   - 把 \`status: STUB\` 改成 \`status: READY\`
   - 填 \`files_to_create\` / \`files_to_modify\` / \`sql_migrations\` / \`go_tests\` / \`gate_endpoints\`
2. 编辑 \`run.sh\` — 把 TODO 段替换成实际 schema / handler 改动
3. 编辑 \`gate.sh\` — 把 TODO 段替换成实际端到端验证
4. 干跑:\`bash scripts/mvp_dev/phase_${i}/run.sh --fresh && bash scripts/mvp_dev/phase_${i}/gate.sh\`
5. 通过后:\`bash scripts/mvp_dev/orchestrator.sh --only ${i}\`
README
    write_if_missing "${readme}" "$(cat "${readme}.tmp")"
    rm -f "${readme}.tmp"
done

echo ""
echo "scaffolding ready at ${DEV_DIR}"
echo "next: edit scripts/mvp_dev/phase_0/tasks.yaml (set status: READY + fill scope)"
