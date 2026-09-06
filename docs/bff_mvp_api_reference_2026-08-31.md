# BFF MVP API Reference — 客户端集成手册

> **状态**:Phase 0-7 全绿(commit `db67123`),生产可对接。
> **基础 URL**:`http://<bff-host>:18907`(默认端口,`MVP_PORT` 可改)
> **鉴权**:`Authorization: Bearer <jwt>` + `X-Tenant-Id: <tid>`(除登录/创建租户外,所有业务端点都需要)
> **生产模式**:`MCP_BASE_URL` 必须指向可达的 OpenMontage MCP(开发期 `http://127.0.0.1:18910/mcp`,生产 `http://lanes.ymxt.top:8900/mcp`);未配置 → stage 端点统一 503(plan §8.2 fail-loud)

---

## 1. 完整状态机(13 档,§17.G)

```
CREATED ─storyboard─▶ STORYBOARD_READY
                        ├─animatic─▶ ANIMATIC_RENDERING ─async_done─▶ ANIMATIC_READY
                        │                                ├─sample─▶ SAMPLE_RENDERING ─async_done─▶ SAMPLE_READY
                        │                                                                 ├─approve─▶ WAITING_APPROVAL
                        │                                                                 │            ├─render─▶ FINAL_RENDERING ─async_done─▶ COMPLETED
                        │                                                                 │            └─approve─▶ WAITING_APPROVAL (幂等)
                        │                                                                 └─render─▶ FINAL_RENDERING (跳过 approve)
                        └─cancel─▶ CANCELLED
```

13 档枚举(常量):`CREATED / ASSET_ANALYZING / REFERENCE_ANALYZING / PLANNING / STORYBOARD_READY / ANIMATIC_RENDERING / ANIMATIC_READY / SAMPLE_RENDERING / SAMPLE_READY / WAITING_APPROVAL / FINAL_RENDERING / COMPLETED / FAILED / CANCELLED`

---

## 2. 鉴权 / 用户 / 租户

| 方法 | 路径 | 鉴权 | 备注 |
|---|---|---|---|
| POST | `/api/auth/login` | 无 | body: `{"code": "<wechat code>"}`(开发期 `WEIXIN_MOCK_AUTH=1`,任何 `MOCK_*` 都行)→ `{"token":"...","internal_user_id":"iu_...","expires_in":86400}` |
| GET  | `/api/me/jwt` | Bearer | → `{"user_id":"wechat:<openid>","internal_user_id":"iu_..."}` |
| POST | `/api/tenants` | Bearer | body: `{"name":"<co name>"}` → `{"id":"tn_..."}` |
| GET  | `/api/tenants` | Bearer | → `{"tenants":[{...}]}` 列出我的所有租户 |
| POST | `/api/tenants/:id/members` | Bearer + `X-Tenant-Id: :id` | body: `{"user_id":"iu_..."}` |

---

## 3. 商品 / 素材 / Manifest(§17.C,Phase 2)

| 方法 | 路径 | 鉴权 | 备注 |
|---|---|---|---|
| POST | `/api/products` | Bearer + Tenant | body: `{"name":"...", "category":"...", "sku":"..."}` → `{"id":"pr_..."}` |
| GET  | `/api/products/:id` | Bearer + Tenant | |
| POST | `/api/products/:id/assets` | Bearer + Tenant | multipart 上传,`role` 字段(主图 / 详情 / 视频 / 字幕) |
| GET  | `/api/products/:id/assets` | Bearer + Tenant | |
| GET  | `/api/products/:id/manifest` | Bearer + Tenant | → AI 自动分类的 manifest,每个 asset 含 `role` + `quality_score` |
| PUT  | `/api/products/:id/manifest/:asset_id` | Bearer + Tenant | 人工修正 `role`/`caption` |

---

## 4. 项目 / 渲染流水线(§17.D,Phase 3+6+7)— **核心**

### 4.1 创建 / 更新项目

```http
POST /api/video-projects
Authorization: Bearer <jwt>
X-Tenant-Id: <tid>
Content-Type: application/json

{"product_id": "pr_xxx"}

→ 201/200
{"id": "vp_xxx", "tenant_id": "...", "product_id": "...", "status": "CREATED", ...}
```

```http
PUT /api/video-projects/:id/brief
{"creative_brief": {...}, "reference_mode": "balanced|description_first|reference_first"}

POST /api/video-projects/:id/reference
{"file_key": "<signed file key>"}     // 引用视频
```

### 4.2 触发阶段(storyboard / animatic / sample / render)— **异步,立即返回 `*_RENDERING`**

```http
POST /api/video-projects/:id/storyboard    # 或 /animatic | /sample | /render
Authorization: Bearer <jwt>
X-Tenant-Id: <tid>

→ 200 (立即)
{
  "job_id":     "jb_xxx",
  "project_id": "vp_xxx",
  "job_type":   "storyboard|animatic|sample|render",
  "status":     "STORYBOARD_READY|ANIMATIC_RENDERING|SAMPLE_RENDERING|FINAL_RENDERING",
  "cost_reserved": 1|5|10|50,
  "async":      true
}
```

注意:
- **storyboard** 没有 `STORYBOARD_RENDERING` 档,handler 立即写到 `STORYBOARD_READY`,job 后台跑 MCP 落 `artifacts_json`(只需轮询 `/api/jobs/:job_id`)
- **render** 先 `quotasvc.Reserve(50)`;credits 不足 → `402 insufficient credits`,**不写 job row**
- 状态机非法转移 → `409 illegal transition from <cur> via <stage>`

### 4.3 轮询项目状态

```http
GET /api/video-projects/:id/status
→ 200 {"project_id": "...", "status": "ANIMATIC_RENDERING", "updated_at": "..."}
```

### 4.4 轮询 job 进度 + 取 artifact

```http
GET /api/jobs/:job_id
→ 200 {
  "id": "jb_xxx",
  "status": "running|succeeded|failed",
  "progress": 0..1,
  "cost_reserved": 50,
  "cost_actual": 50,
  "external_run_id": "stub-run-render-1788...",   // 上游 MCP run id
  "om_project_id":   "om-vp_ccb...",
  "artifacts_json":  "{\"preview_url\":\"...\",\"duration_seconds\":20,\"resolution\":\"1080x1920\"}",
  // storyboard 形状:{"scenes":[{"scene_id":1,"preview_url":"...","duration":2.4},...]}
  // sample 形状:    {"files":["..."], "scene_ids":[3,5]}
  // render 形状:    {"preview_url":"...","duration_seconds":20,"resolution":"1080x1920"}
  "error_message": ""                              // 仅 failed 时非空
}
```

artifact 的 §23 形状(storyboard / animatic / sample / render)见 scope §23,直接 `json.loads(artifacts_json)` 用。

### 4.5 用户 approve(Phase 7)— sample → render 的强制关

```http
POST /api/video-projects/:id/approve
Authorization: Bearer <jwt>
X-Tenant-Id: <tid>

→ 200 {
  "project_id":  "vp_xxx",
  "status":      "WAITING_APPROVAL",
  "approved_by": "iu_xxx",
  "approved_at": "2026-08-31T07:06:36Z"
}

→ 409 {
  "error":          "illegal transition from CREATED via approve",
  "allowed_from":   ["SAMPLE_READY", "WAITING_APPROVAL"],
  "current_status": "CREATED"
}
```

幂等:从 `WAITING_APPROVAL` 再 approve 仍 200,`approved_at` 会被刷新。

### 4.6 Cancel

```http
POST /api/video-projects/:id/cancel
→ 200 {"project_id": "...", "status": "CANCELLED"}     // 任何非终态都能调
```

### 4.7 端到端调用流(典型客户端集成)

```
1. login → token
2. create product + upload assets + wait /manifest
3. create project
4. PUT /brief + POST /reference(可选)
5. POST /storyboard → poll /jobs/:id → 取 artifacts_json.storyboard.scenes  → 展示给用户
6. POST /animatic   → poll /status → ANIMATIC_READY → GET /jobs → 取 artifacts_json.animatic.preview_url → 展示
7. POST /sample     → poll /status → SAMPLE_READY   → GET /jobs → 取 artifacts_json.sample.files → 展示给用户审批
8. 用户点"同意 render"
9. POST /approve    → WAITING_APPROVAL(写 approved_by + approved_at)
10. POST /render    → poll /status → FINAL_RENDERING → COMPLETED → GET /jobs → artifacts_json.preview_url 即最终 mp4
```

失败路径:`render` 失败(上游 MCP 报错)→ BFF 写 `production_jobs.status=failed` + `error_message` + 自动 `Refund(50 credits)` + 项目状态 `FAILED`。

---

## 5. Agent Gateway(§17.F,Phase 5)— 给前端 / 编排层用

8 个动词,统一委托到 §17.D handler:

```http
POST /api/gateway/analyze-product-assets   {project_id, payload}   → 占位响应(Phase 6+ 接 OpenClaw)
POST /api/gateway/analyze-reference-video  {project_id, payload}   → 占位响应
POST /api/gateway/generate-storyboard      {project_id, payload}   → 委托到 StartStage("storyboard")
POST /api/gateway/generate-animatic        {project_id, payload}   → 委托到 StartStage("animatic")
POST /api/gateway/generate-sample          {project_id, payload}   → 委托到 StartStage("sample")
POST /api/gateway/render-final             {project_id, payload}   → 委托到 StartStage("render") + Reserve(50)
POST /api/gateway/cancel-production        {project_id, payload}   → 委托到 Cancel
GET  /api/gateway/production-status?project_id=vp_xxx              → {verb, project_id, status, job_id, detail}
```

每个 verb 响应统一是 `VerbResponse`:

```json
{
  "verb":      "GenerateAnimatic",
  "project_id":"vp_xxx",
  "status":    "ANIMATIC_RENDERING",   // 13 档统一 enum
  "job_id":    "jb_xxx",                // 仅 state-changing verbs
  "detail":    {"note": "..."}          // 占位或 stage-specific extras
}
```

### 5.1 OM 状态聚合(§17.G)— Phase 5 已实现

```http
GET /api/status/lookup?raw=<om_status_string>
→ 200 {
  "raw": "pending",
  "unified": "CREATED",           // 永远映射到 13 档之一;未知 raw → FAILED(plan §8.2 fail-loud,不静默)
  "supported_raw_states": ["pending", "queued", "running", "error_unknown", ...]
}
```

---

## 6. 配额(§17.E,Phase 4)

| 方法 | 路径 | 备注 |
|---|---|---|
| GET | `/api/quota` | 读 tenant 当前 `available_credits / reserved_credits / consumed_credits`;首次访问自动 upsert free tier (100) |
| POST | `/api/quota/reserve` | `{amount, job_id}` → `reservation_id`;`402 insufficient credits` 当 `available < amount` |
| POST | `/api/quota/consume` | `{reservation_id}` → reserved 移到 consumed |
| POST | `/api/quota/refund` | `{reservation_id}` → reserved 移到 available |

成本表(Phase 4 fixed):`storyboard=1, animatic=5, sample=10, render=50`

---

## 7. 文件(§17.H,Phase 1)— 签发 / 拉

| 方法 | 路径 | 备注 |
|---|---|---|
| GET | `/api/files/sign?key=<key>&op=read\|write` | 签发短时 signed URL,内含 tenant_id 校验 |
| GET | `/api/files/:key` | 通过签名 URL 直接拉(无 Bearer) |

---

## 8. 错误码速查

| HTTP | 含义 |
|---|---|
| 400 | body 缺字段或格式错 |
| 401 | JWT 缺失或失效 |
| 403 | 跨 tenant 访问(plan §17.H 隔离边界) |
| 404 | project / product / job 不存在 |
| 409 | 状态机非法转移 / 项目已 CANCELLED |
| 402 | quota 不足(`render` 触发) |
| 503 | `MCP_BASE_URL` 未配置 — stage 端点 fail-loud(plan §8.2) |

---

## 9. 客户端接入清单(开发者 TODO)

| 步骤 | 端点 | 注意 |
|---|---|---|
| 1 | `POST /api/auth/login` | 缓存 `token`(24h)和 `internal_user_id` |
| 2 | `POST /api/tenants` | 首次登录用户需要建租户 |
| 3 | `POST /api/products` + `POST /:id/assets` | 上传素材,等 `GET /:id/manifest` 拿到 AI 分类 |
| 4 | `POST /api/video-projects` | 拿到 `vp_xxx` |
| 5 | `POST /:id/{stage}` 4 次 | 每次拿到 `jb_xxx`,立即轮询 `/api/jobs/:job_id` 等 `status=succeeded` + `artifacts_json` |
| 6 | `POST /:id/approve` | 用户点同意后调一次 |
| 7 | `POST /:id/render` | 最终渲染,拿到 `preview_url` |

---

## 10. 与 vclaw(`/opt/vclaw/`)的差异

| 项 | vclaw | BFF(本仓库) |
|---|---|---|
| 鉴权 | `POST /api/auth/wechat/login` + Bearer | `POST /api/auth/login` + Bearer(Phase 0) |
| 渲染请求体 | 必传 `edit_decisions + asset_manifest` | 只传 stage 名,body 为空(Phase 6 简化版) |
| Polling 端点 | `GET /api/video-projects/:id/preview/:jobId` | `GET /api/jobs/:job_id` |
| Approve 端点 | `POST /:id/approve/:type`(per-stage) | `POST /:id/approve`(单端点,sample 之后) |
| MCP 接入 | vclaw 自己的 `internal/` 实现 | `frameflow-bff/internal/mvpclient/`(Phase 6) |

**新客户端建议直接用 BFF(本仓库)端点** — 已通过 21/21 gate,文档齐全。vclaw 的实现是平行版本,后续可统一。

---

## 11. 状态码 → 13 档映射(全表)— Phase 5 已实现

```
ASSET_ANALYZING     ← analyzing_assets, mcp-raw
REFERENCE_ANALYZING ← analyzing_reference
PLANNING            ← queued, storyboard_pending
STORYBOARD_READY    ← storyboard_ready
ANIMATIC_RENDERING  ← animatic_rendering
ANIMATIC_READY      ← animatic_ready
SAMPLE_RENDERING    ← sample_rendering
SAMPLE_READY        ← sample_ready
WAITING_APPROVAL    ← (客户端主动)
FINAL_RENDERING     ← final_rendering, running, in_progress, mcp-progress
COMPLETED           ← render_done, success, succeeded, done
FAILED              ← failed, error, error_unknown, aborted, timeout, 未知 raw
CANCELLED           ← cancelled, canceled
```

调用 `/api/status/lookup?raw=<任意>` 拿映射。