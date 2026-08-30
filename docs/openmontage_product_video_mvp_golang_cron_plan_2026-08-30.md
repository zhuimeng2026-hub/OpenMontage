# OpenMontage 商品视频 MVP — §17 Golang/Gin Cron-Driven 落地计划

> 配套文档:`docs/openmontage_product_video_mvp_golang_scope.md`
> 制定日期:2026-08-30
> 执行环境:`/opt/OpenMontage_Voicebox/frameflow/bff/`
> 目标:把 §17 必做项(A–H)按阶段拆解,由系统 cron 定时推进,终端离线运行。

---

## 1. 设计原则

1. **每个 Phase = 一个独立 cron 任务**,状态文件驱动 `--fresh / --resume`。
2. **Phase 之间串行,Phase 内可并行**;每个 Phase 都有 gate test,gate 不绿下个 Phase 不启动。
3. **绝不静默改代码** — 每个 Phase 修改的文件清单必须落到 `logs/mvp_dev/diff-<phase>.txt`,事后可审计。
4. **绝不越权扩 scope** — Phase 4 写着实现 E Quota,就只动 quota 相关文件;不要顺手把 §19 接口全部加上。
5. **可中断、可恢复** — OOM / 系统重启后 `--resume` 从上次断点继续,不从头跑。
6. **失败保守** — 任何 gate 失败,cron 行保留(下次再跑),不自我删除,不自动重试 N 次。
7. **不污染生产数据** — 所有 schema 迁移带 `IF NOT EXISTS`,所有写操作带 `tenant_id` 过滤。

---

## 2. 阶段拆分(Phase 0 → Phase 5)

| Phase | §17 项 | 范围 | 上游 gate | 估算改动量 | 计划 cron 时间 |
|---|---|---|---|---|---|
| 0 | A 微信身份 | openid/unionid/JWT/internal_user_id;OAuth callback 重构为 POST 静默登录 | (无) | 4–6 个文件,1 张表 | 02:00 每日 |
| 1 | B + H 多租户 + 文件权限 | tenant/tenant_user 表;TenantScope 中间件;signed URL + 文件 ACL | Phase 0 全绿 | 6–9 个文件,2 张表 | 03:00 每日 |
| 2 | C Product/Asset | product/product_asset/product_manifest 表;上传 + AI 分类 + 手工修正 | Phase 1 全绿 | 5–7 个文件,3 张表 | 02:00 每日 |
| 3 | D Project/Job | video_project/production_job/preview_job/render_job;状态机 + 重试 | Phase 2 全绿 | 8–10 个文件,4 张表 | 03:00 每日 |
| 4 | E Quota | available/reserved/consumed credits;Final Render 前 reserve | Phase 3 全绿 | 3–4 个文件,1 张表 + 视图 | 02:00 每日 |
| 5 | F + G Agent Gateway + 状态聚合 | 8 个业务动词封装;OM 状态 → 13 档统一状态映射 | Phase 4 全绿 | 5–7 个文件,无新表 | 03:00 每日 |

> 时间错峰(02:00 / 03:00 交替)是为了避免和现有 01:00 regression cron(`scripts/regression/`)抢资源。文档修改量估算以 Go 后端为准;不包含 OM/MCP 侧的工作。

---

## 3. 每个 Phase 的统一结构

每个 Phase 目录结构:

```
scripts/mvp_dev/
├── orchestrator.sh            # 入口:遍历所有 phase,按顺序跑
├── phase_0/
│   ├── tasks.yaml             # 本阶段任务清单(机械化格式)
│   ├── run.sh                 # 实际执行脚本(可被 --resume)
│   ├── gate.sh                # gate test,跑不过下个 Phase 不启动
│   └── README.md              # 人类可读的范围说明
├── phase_1/ ...
└── ...

logs/mvp_dev/
├── state/
│   ├── phase_0.json           # last_run_started_at / finished_at / exit_code / diff / interrupted
│   └── ...
├── diff-phase_0-20260830-020015.txt
├── gate-phase_0-20260830-020015.log
└── summary-<timestamp>.log
```

`state/phase_N.json` 格式:

```json
{
  "phase": 0,
  "last_run_started_at": "2026-08-30T02:00:01+08:00",
  "last_run_finished_at": "2026-08-30T02:03:42+08:00",
  "last_run_exit_code": 0,
  "last_gate_exit_code": 0,
  "diff_file": "logs/mvp_dev/diff-phase_0-20260830-020015.txt",
  "interrupted": false,
  "files_changed": ["internal/auth/jwt.go", "internal/auth/wechat.go", ...],
  "mode": "fresh"
}
```

`--resume` 语义:仅当 `last_run_exit_code == 0 && last_gate_exit_code == 0 && interrupted == false && finished_at > 24h ago` 时跳过本 Phase;否则从头重跑该 Phase(因为 Phase 内部分步状态没持久化,这一步只能重跑)。

---

## 4. Cron 编排

### 4.1 推荐 crontab 片段(贴在 `crontab -e` 末尾)

```cron
# OpenMontage 商品视频 MVP §17 阶段化开发 — 终端离线运行
# Orchestrator at 01:30 daily;依次推进 phase 0 → 5。
# 每个 phase 内部会跳过最近已绿的 phase(--resume 语义)。
# 任何 gate 失败,当天后续 phase 不再启动;crontab 行不会自删。
30 1 * * * /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh >> /opt/OpenMontage_Voicebox/logs/mvp_dev/cron-stdout.log 2>&1
```

> 选 01:30 是为了不与现有 01:00 / 01:05 regression cron 抢 CPU/IO。
> 单次 orchestrator 启动后,phase 之间间隔 5 分钟(用 `sleep 300`),给前一个 phase 的 DB 迁移留 flush 时间。

### 4.2 orchestrator.sh 行为

```text
for phase in 0 1 2 3 4 5:
    read state/phase_<n>.json
    if state.last_run_exit_code == 0 and not interrupted and finished < 24h ago:
        log "phase <n> green, skip"
        continue
    run scripts/mvp_dev/phase_<n>/run.sh --fresh  (or --resume)
    if run exit != 0:
        log "phase <n> failed, halting orchestration"
        exit 1
    run scripts/mvp_dev/phase_<n>/gate.sh
    if gate exit != 0:
        log "phase <n> gate failed, halting"
        exit 1
    sleep 300   # 下个 phase 前等 5 分钟
```

### 4.3 单个 phase 内部(以 Phase 0 为例)

```bash
# phase_0/run.sh
# A. 微信身份实现 — 范围严格限定在 §17.A
set -u
REPO=/opt/OpenMontage_Voicebox
BFF=${REPO}/frameflow/bff
LOG=${REPO}/logs/mvp_dev/diff-phase_0-$(date +%Y%m%d-%H%M%S).txt
: > "${LOG}"

# 1. repo 必须干净(防御:有人手动改了一半)
if [[ -n "$(git -C "${REPO}" status --porcelain)" ]]; then
    echo "[FATAL] repo not clean — refusing to run" >&2
    git -C "${REPO}" status --porcelain >&2
    exit 2
fi

# 2. 写入本 phase 范围
{
    echo "phase 0 — §17.A 微信身份"
    echo "scope: openid/unionid/JWT/internal_user_id + OAuth callback 重构"
    echo "files to touch (预期):"
    echo "  internal/auth/jwt.go        (新建/重写)"
    echo "  internal/auth/wechat.go     (新建)"
    echo "  internal/middleware/auth.go (新建)"
    echo "  handlers/auth.go            (扩字段)"
    echo "  store/migrations/0001_auth.sql (新建)"
} >> "${LOG}"

# 3. 写代码(实际就是把这部分 task 转成具体 go 文件)
#    —— 关键:不能简单 echo,要么由 orchestrator 调 sub-agent 执行,要么人工 commit
#    这里给出 placeholder,实际填充见 §6 安装步骤。
echo "[STUB] phase_0/run.sh — 实际代码改动逻辑见 scripts/mvp_dev/phase_0/tasks.yaml" | tee -a "${LOG}"

# 4. 更新 state
cat > ${REPO}/logs/mvp_dev/state/phase_0.json <<EOF
{
  "phase": 0,
  "last_run_started_at": "$(date -Iseconds)",
  "last_run_finished_at": "$(date -Iseconds)",
  "last_run_exit_code": 0,
  "last_gate_exit_code": 0,
  "diff_file": "${LOG}",
  "interrupted": false,
  "files_changed": ["(待填)"],
  "mode": "fresh"
}
EOF

# 5. 跑 gate
bash ${REPO}/scripts/mvp_dev/phase_0/gate.sh || exit 1
```

---

## 5. Gate Test 设计

每个 Phase 配一个 gate.sh,过不了下个 Phase 不启动。Gate 只测本 Phase 必做的最小可验证项,不测全局。

| Phase | Gate 最小验证 |
|---|---|
| 0 | `POST /api/auth/login` 用合法 code 拿 JWT,`/api/me` 带 JWT 返回 user_id + internal_user_id 字段 |
| 1 | 跨 tenant 调用任何资源接口返回 403;无 tenant header 返回 401;signed URL 过期拒绝 |
| 2 | `POST /api/products` 创建成功,`POST /api/products/:id/assets` 上传一张图,`GET /api/products/:id/manifest` 拿到 role + quality_score |
| 3 | `POST /api/video-projects` 创建,`POST /api/video-projects/:id/storyboard` 启动后,`GET /api/video-projects/:id/status` 状态机单调推进 |
| 4 | reserve 后 available 减少;consume 后 reserved 减少;失败返还 reserved |
| 5 | Agent Gateway 8 个动词都有路由,无 404;状态聚合映射覆盖 13 档,无 unknown |

---

## 6. 安装步骤(首次部署)

```bash
# 1. 建目录
mkdir -p /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_{0..5}
mkdir -p /opt/OpenMontage_Voicebox/logs/mvp_dev/state

# 2. 把本计划 § 7 的脚本占位文件生成出来(见 §7)
bash scripts/mvp_dev/install_scaffolding.sh

# 3. 填每个 phase_*/tasks.yaml(就是把 §17.A..H 的具体任务转成机械化步骤)
#    —— 这一步必须人工,不要让 cron 自己填。

# 4. 干跑一次 orchestrator --dry-run,确认 phase 全部 skip(还没填 tasks)
bash scripts/mvp_dev/orchestrator.sh --dry-run

# 5. 填完 phase_0/tasks.yaml 后,手动跑一次 phase 0
bash scripts/mvp_dev/phase_0/run.sh --fresh && bash scripts/mvp_dev/phase_0/gate.sh

# 6. gate 绿了再上 cron
crontab -e
# 追加:
# 30 1 * * * /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh >> /opt/OpenMontage_Voicebox/logs/mvp_dev/cron-stdout.log 2>&1
```

> **不要跳步骤 4-5**。memory 中 `regression cron pre-existing bugs 2026-08-29` 显示现有 cron 已经会 nightly 暴露 contract bug,本计划里如果直接上 cron,跑出来的 diff 会污染主分支。**首次部署必须先干跑 + 人工验证 phase_0 gate 绿,再开 cron。**

---

## 7. 文件清单(待落地)

| 文件 | 作用 | 状态 |
|---|---|---|
| `scripts/mvp_dev/orchestrator.sh` | 主入口,遍历 phase | 待写 |
| `scripts/mvp_dev/install_scaffolding.sh` | 一键创建目录 + 占位 | 待写 |
| `scripts/mvp_dev/phase_0/{tasks.yaml,run.sh,gate.sh,README.md}` | A 微信身份 | 待写 |
| `scripts/mvp_dev/phase_1/...` | B + H | 待写 |
| `scripts/mvp_dev/phase_2/...` | C | 待写 |
| `scripts/mvp_dev/phase_3/...` | D | 待写 |
| `scripts/mvp_dev/phase_4/...` | E | 待写 |
| `scripts/mvp_dev/phase_5/...` | F + G | 待写 |
| `logs/mvp_dev/` | 运行时日志 + state | 自动创建 |

---

## 8. 风险与边界

1. **§17.A 微信身份改造会影响现有 `/api/wechat/login` 等路由**,Phase 0 阶段会触发 behavior change — 但目前 `/api/wechat/*` 没有生产用户(纯内部 MVP),影响可控。
2. **§17.D 状态机的 13 档和现有 MCP 状态码不一一对应**,Phase 5 引入 mapping table 时会暴露 MCP 侧的隐藏状态(例如 mcp-raw 的 `error_unknown`)— 这些映射失败要 fail-loud,不能 silently fallback。
3. **§17.E Quota 的"reserve"语义和现有 `limits.Usage` 重叠**,Phase 4 要么替换 `limits.Usage` 要么桥接,设计时由 orchestrator 在 tasks.yaml 里注明。
4. **cron 跑出来的 diff 如果有 50+ 文件**,需要在 PR 时人工 review — orchestrator 不要自动 push,只把 diff 落到 `logs/mvp_dev/diff-phase_*.txt`,由人类决定是否 cherry-pick / squash。
5. **memory 中的 `regression cron pre-existing bugs 2026-08-29`**:现有 cron 已经 nightly 暴露 contract bug,本计划设计上引入 gate + diff 文件,问题不会更糟,但**首次部署务必先干跑 + 人工验证**,不要直接上 cron。

---

## 9. 验收标准(MVP 完工)

1. 6 个 phase 的 gate 全绿,且 state 文件显示 24h 内无重跑;
2. §19 列出的 24 个端点全部 200(空数据 404 除外);
3. §20 列出的 7 张表全部建好,外键关系符合预期;
4. 一次完整的端到端流程跑通:登录 → 建 product → 上传素材 → 建 video_project → 跑 storyboard → approve → 跑 final render → 拿到 mp4。
5. 所有 diff 文件归档到 `logs/mvp_dev/diff-phase_*.txt`,可回溯。

---

## 10. 不在本计划内(明确不做)

- §19 的 24 个具体端点的 handler 实现 — 这是 phase 0-5 之外的工作,Phase 5 收尾后单独排期。
- §18 列出的 11 类不该做的功能 — 反向边界,phase 内不允许越界。
- §22 提到的 OM MCP 薄封装(`prepare_product_remix` 等)— 在 OM 侧做,不在 Golang 控制面。
- §25 提到的"第一阶段不做"清单(专业 NLE、自定义 Prompt 工程等)— 全期不做。



## 开发环境认证模式（必须保留身份模型，允许绕过微信扫码）

为避免开发和调试阶段频繁使用微信扫码，系统需要区分 `dev / staging / prod` 三种认证模式。

### 1. 基本原则

开发环境可以绕过“微信认证过程”，但**不能绕过系统内部的身份、租户、权限模型**。

也就是说，即使在 `dev` 环境中，所有业务请求仍必须具有：

```text
user_id
tenant_id
device_id（桌面 OpenClaw 场景）
permissions
```

后续项目、素材、任务、预览、渲染结果等仍然必须按照 `tenant_id` 做隔离。

禁止为了方便开发而使用匿名业务逻辑，例如：

```text
默认 user_id = 1
默认 tenant_id = 1
所有用户共用同一 workspace
```

---

### 2. Dev 模式

开发环境允许通过配置直接注入测试身份，例如：

```text
AUTH_MODE=dev
DEV_USER_ID=dev_user_01
DEV_TENANT_ID=dev_tenant_01
DEV_DEVICE_ID=dev_device_01
```

Gin 可以提供仅在 `dev` 环境启用的测试登录接口，例如：

```http
POST /api/dev/login-as
```

返回与正式登录相同格式的 `access_token`。

后续业务代码必须继续走正常的：

```text
access_token
→ user_id
→ tenant_id
→ permission
→ resource ownership
```

不得为 dev 模式另外维护一套业务逻辑。

---

### 3. OpenClaw 桌面端开发模式

OpenClaw 开发阶段允许使用预配置测试设备身份自动登录，无需每次显示小程序码。

例如：

```text
AUTH_MODE=dev
DEV_USER_ID=dev_user_01
DEV_TENANT_ID=dev_tenant_01
DEV_DEVICE_ID=dev_device_01
```

启动后由 OpenClaw 自动向 Gin 获取开发环境 access token。

但 OpenClaw 后续执行任务、读取素材、调用 OM 时，仍必须携带：

```text
user_id
tenant_id
device_id
```

并继续接受 Gin 的权限和租户校验。

---

### 4. Staging 模式

`staging` 环境应尽量接近生产环境：

```text
AUTH_MODE=wechat
```

需要真实微信登录 / 小程序设备绑定。

为了提高测试效率，可以允许较长的测试 session 或 refresh token，但不得绕过：

```text
openid → user_id → tenant_id
```

的真实映射过程。

---

### 5. Production 模式

生产环境：

```text
AUTH_MODE=wechat
```

必须使用正式微信认证、设备绑定、token refresh、权限校验。

生产环境必须禁止：

```text
/dev/login-as
DEV_USER_ID
DEV_TENANT_ID
```

等任何开发态身份注入能力。

---

### 6. 实现要求

认证模式只负责决定：

> “身份是如何获得的”

不能改变：

> “系统内部如何表示和校验身份”。

因此三种环境最终都必须统一进入同一套业务身份模型：

```text
user_id
↓
tenant_id
↓
device_id（如适用）
↓
permissions
↓
project / asset / job / render
```

一句话原则：

> **开发环境可以免扫码，但不能免身份、免租户、免权限。**

