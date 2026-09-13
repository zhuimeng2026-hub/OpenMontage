# OpenMontage 接收契约（OM 侧）

供 OM 团队（运行在 `lanes.ymxt.top:8900` 的 MCP 服务）参考：vclaw 在
OpenClaw 完成二次处理后，会把"产物"通过 MCP `tools/call` 提交给 OM 做最终
渲染。本文档描述 OM **必须实现/支持的接口形状**，使 vclaw 能成功提交并取回
渲染结果。

> 范围说明：本契约描述 vclaw → OM 的"最终渲染提交"通道。它复用了 vclaw
> 既有的 `internal/openmontage/client.go` 中 `RenderVideo` 所用的
> `video_compose` 工具，因此 OM 侧几乎不需要为新 remix 流程新增接口，
> 只需保证 `video_compose` / `get_render_status` 的输入输出字段对齐即可。
>
> 截至本文撰写，vclaw 端的 **OpenClaw 二次处理调用尚未接通**（见
> `internal/handler/remix_package.go` 的 `RemixRenderDispatchHandler` 仍是
> 501 桩，以及 `cmd/worker/main.go` 的 `dispatchToOpenClaw` 仍是 sim 桩）。
> OM 团队可先按本文档把接收端准备好，等 vclaw 把 OpenClaw 产出映射成下面的
> "产物"字段后即可联调。

---

## 1. 传输层

- **协议**：MCP streamable-http（JSON-RPC 2.0）。
- **端点**：`http://<om-host>:8900/mcp`（vclaw 配置项
  `openmontage.mcp_url`，生产值为 `http://192.168.20.173:8900/mcp`；
  对外域名 `lanes.ymxt.top`，由网络层映射到同一服务）。
- **鉴权**：`Authorization: Bearer <MCP_API_TOKEN>`
  （vclaw 配置项 `openmontage.mcp_token`）。
- **会话握手**（vclaw 客户端会自动完成，OM 必须支持）：
  1. `initialize`（protocolVersion `2024-11-05`，capabilities `{}`，
     clientInfo `{name:"vclaw-control-plane", version:"0.1.0"}`）
     → OM 在响应头返回 `Mcp-Session-Id`。
  2. `notifications/initialized`（无 id、无响应体）。
  3. 之后每次 `tools/call` 必须带 `Mcp-Session-Id` 头，否则返回 400
     "Missing session ID"。

---

## 2. 最终渲染：工具 `video_compose`

vclaw 通过 `tools/call` 调用，包成 MCP content 包裹返回。

### 2.1 请求（vclaw → OM）

```json
{
  "tool_name": "video_compose",
  "inputs": {
    "operation": "render",
    "edit_decisions": { "...": "由 OpenClaw 二次处理生成的剪辑决策；clawx-studio 的 effects/subtitles 会落在 edit_decisions.metadata 下" },
    "asset_manifest": { "version": "1", "assets": [ "..." ] },
    "scene_plan":     { "version": "1", "scenes": [ "..." ] },
    "output_path": "/tmp/<projectID>-render.mp4",
    "profile": "high_res",
    "_job_id": "<vclaw 生成的作业 ID>",
    "creative_brief": "<可选透传>",
    "scene_ids": [1, 2, 3]
  }
}
```

字段含义与约束：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `operation` | string | 是 | 固定 `"render"`（最终渲染）。 |
| `edit_decisions` | object | 条件 | OpenClaw 二次处理产出的剪辑决策。clawx-studio 的 `effects`/`subtitles` 会由 vclaw 注入到 `edit_decisions.metadata`（`metadata.effects` / `metadata.subtitles`）。 |
| `asset_manifest` | object | **是** | **渲染必需**。vclaw 在提交前会做 preflight：缺 `asset_manifest` 直接返回 400，不会打到 OM。OM 内部同样应校验。 |
| `scene_plan` | object | 条件 | 场景计划（含 scenes 列表）。 |
| `output_path` | string | 是 | OM **本地文件系统**上应写出 mp4 的路径（目前 vclaw 用 `/tmp/<projectID>-<type>.mp4`，后续应改为 OM 可写目录并回传）。 |
| `profile` | string | 是 | 质量档：`"low_res"`（预览/animatic）/ `"normal"`（sample）/ `"high_res"`（最终渲染）。remix 最终渲染用 `"high_res"`。 |
| `_job_id` | string | 是 | vclaw 生成的**所有权 ID**。OM **不得**把它当作自身 render-job 注册表中的 job id 来用（见 §3 的 `render_job_id` 区分）。它仅用于队列归属。 |
| `creative_brief` / `scene_ids` | 任意 | 否 | 透传，OM 可按需忽略或用于日志。 |

> **"产物"映射**：在 remix 流程里，OpenClaw 二次处理的输出应被 vclaw 转换成
> 上面的 `edit_decisions` / `asset_manifest` / `scene_plan`。即 OpenClaw 负责
> 把 remix 包的 timeline/scenes（keep/replace/generate + prompt）解析成这三个
> OM 可消费的 JSON。OM 侧无需理解 remix 包原始结构，只看这三个字段。

### 2.2 响应（OM → vclaw）

OM 必须返回 MCP 标准 content 包裹，文本里是 JSON：

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"ok\":true,\"data\":{\"output\":\"/tmp/<projectID>-render.mp4\",\"share_url\":\"http://<om-host>:4750/media/<projectID>/render/<jobID>.mp4\",\"render_job_id\":\"om-xxxx\",\"status\":\"rendered\"}}"
    }
  ],
  "isError": false
}
```

vclaw 客户端（`populateRenderHandle`）实际会读取的键（皆可出现在顶层或
`data` 内层，**二者都返回最稳妥**）：

| OM 返回键 | 映射为 | 说明 |
|---|---|---|
| `ok` / `success` | 成功标志 | 必须为 `true` 表示成功；失败置 `false` 并带 `error`。 |
| `status` | 作业状态 | `"rendered"` / `"completed"` → vclaw 记为 `published`；其它原样。 |
| `output` / `output_path` / `video_path` | 本地输出路径 | OM 写出的 mp4 本地路径。 |
| `share_url` / `file_url` | 可访问 URL | HTTP 可访问的渲染结果地址（vclaw 会记入 `RenderHandle.FileURL`）。**强烈建议返回**，否则 vclaw 只能靠 `backlot_base_url` 自行拼 URL。 |
| `render_job_id` | OM 作业注册 ID | **仅当异步时返回**。用于 §3 的 `get_render_status` 轮询。**不要与 `_job_id` 混淆**。 |

**同步模式（video_compose 当前为同步）**：直接带 `output`/`share_url` 即视为
已完成（vclaw 置为 `published`）。若既无 `output` 也无 `render_job_id`，
vclaw 会报错 `"video_compose returned no output or render_job_id"`。

**异步模式（可选）**：若 OM 选择入队后先返回 `render_job_id` 与
`status:"queued"`，vclaw 会进入 §3 轮询。

失败：返回 `{"ok":false,"error":"<原因>"}`，或 `isError:true`，vclaw 会置作业
为 `FAILED` 并透传错误。

---

## 3. 异步状态轮询：工具 `get_render_status`

当 `video_compose` 返回了 `render_job_id` 时，vclaw 的 worker 轮询此工具。

### 3.1 请求

```json
{ "tool_name": "get_render_status", "inputs": { "render_job_id": "om-xxxx" } }
```

### 3.2 响应（OM → vclaw）

文本 JSON 中 vclaw 会读取的键：

| OM 返回键 | 说明 |
|---|---|
| `status` | `"queued"` / `"running"` / `"published"` / `"failed"`（缺省按 `queued`）。 |
| `render_phase` / `phase` | OM 内部渲染阶段，透传展示。 |
| `video_path` / `output_path` | 完成时本地路径。 |
| `share_url` / `file_url` | 完成时 HTTP URL。 |
| `progress` | 0–100 进度（best-effort）。 |
| `queue_position` | 队列位置（不在队列为 -1）。 |
| `error` | `failed` 时的错误描述。 |

---

## 4. 参考视频拆解：video_analyzer + video_brief_synthesizer

vclaw 的两个 M1-B 动词（/api/gateway/analyze-reference-video 和
/api/gateway/synthesize-reference-brief）都通过 tools/call 落到
OM 的 MCP 上。本节是 OM 必须实现两个工具的契约。

调用方是 vclaw Agent Gateway（UserAuth 中间件鉴权后的
desktop JWT，由 vclaw 在 MCP 代理里替换为 MCP_API_TOKEN），
不是 OpenClaw agent。本节定义的字段集合与 vclaw 端
internal/handler/gateway_verbs.go 的 AnalyzeReferenceVideoHandler /
SynthesizeReferenceBriefHandler / extractReferenceSummary 双向对齐。

### 4.1 video_analyzer（必跑，analyzer 阶段）

#### 4.1.1 请求（vclaw → OM）

```json
{
  "tool_name": "video_analyzer",
  "inputs": {
    "source":               "https://youtube.com/watch?v=abc",
    "project_id":           "vclaw-<uuid>",
    "userid":               "<wechat_openid>",
    "analysis_depth":       "transcript_only | standard | deep",
    "transcript_path":      "projects/users/<uid>/<pid>/_audio_transcript.json",
    "max_keyframes":        20,
    "max_duration_seconds": 600,
    "language":             ""
  }
}
```

字段表：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| source | string | 是 | URL（http/https）或 OM 可达的本地路径 |
| project_id | string | 是 | vclaw video_projects.id，safe basename |
| userid | string | 是 | WeChat openid，用于产物归属 |
| analysis_depth | enum | 否（默认 standard） | transcript_only 跳过视觉分析；deep 多花 token 跑细粒度 |
| transcript_path | string | 否 | 已存在的转录文件路径，OM 应跳过 Whisper 直接复用 |
| max_keyframes | int | 否（默认 20） | 关键帧采样上限，OM 可截断到此值 |
| max_duration_seconds | int | 否（默认 600） | 视频时长上限，超过应 fail-fast 或截断 |
| language | string | 否 | 强制 Whisper 语言；空字符串 = 自动检测 |

#### 4.1.2 响应（OM → vclaw）

返回标准 MCP content 包裹，文本 JSON：

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"source\":{...},\"structure_analysis\":{...},\"content_analysis\":{...},\"replication_guidance\":{...},\"artifacts\":[\"projects/users/<uid>/<pid>/analysis_<ts>/video_analysis_brief.json\"],\"_analysis_meta\":{...}}"
    }
  ],
  "isError": false
}
```

字段含义（vclaw 端 extractReferenceSummary 会按需读取）：

| 字段 | 路径 | 说明 |
|---|---|---|
| source.type | source.type | url / local，vclaw 据此写 reference_mode |
| source.url | source.url | URL 模式的源地址 |
| source.local_path | source.local_path | 本地模式的源路径 |
| source.duration_seconds | source.duration_seconds | 用于 14 键 summary |
| source.aspect_ratio | source.aspect_ratio | 同上 |
| structure_analysis.total_scenes | structure_analysis.total_scenes | 整数 |
| structure_analysis.pacing_profile.pacing_style | structure_analysis.pacing_profile.pacing_style | slow/medium/fast |
| structure_analysis.pacing_profile.avg_scene_duration_seconds | 同 | 浮点 |
| structure_analysis.pacing_profile.cuts_per_minute | 同 | 浮点 |
| structure_analysis.motion_breakdown[] | structure_analysis.motion_breakdown | 每项含 motion_type + duration；vclaw 读 len + [0].motion_type |
| content_analysis.tone | content_analysis.tone | 字符串 |
| content_analysis.summary | content_analysis.summary | 自由文本，OpenClaw 渲染时直接展示 |
| content_analysis.language | content_analysis.language | ISO 639-1（如 en / zh） |
| replication_guidance.suggested_pipeline | replication_guidance.suggested_pipeline | OM 推荐的 vclaw 渲染管线 |
| replication_guidance.estimated_complexity | replication_guidance.estimated_complexity | low/medium/high |
| artifacts[] | artifacts | OM 上写出的所有产物路径，**首项必须是 video_analysis_brief.json 的相对路径**——vclaw 把它存到 reference_brief_path 列 |
| _analysis_meta.has_transcript | _analysis_meta.has_transcript | bool。vclaw 用它做终态判断（见 §4.4） |
| _analysis_meta.keyframe_count | _analysis_meta.keyframe_count | 整数 |
| _analysis_meta.steps_failed[] | _analysis_meta.steps_failed | 失败步骤名数组。vclaw 持久化到 reference_steps_failed_json 列 |

OM 端可以输出 content_analysis.style_profile /
replication_guidance.key_elements_to_replicate 等额外字段——vclaw 不会
逐字段读，但会把整个 brief JSON 持久化到 creative_brief_json 列，
OpenClaw agent 在 M2-A 里读它做编排决策。

### 4.2 video_brief_synthesizer（可选，synthesizer 阶段）

仅当 vclaw 收到 extra.synthesize=true **且** 服务端
ANTHROPIC_BASE_URL 环境变量非空时调用。失败是允许的——vclaw 会把
synthesis 失败记到 synthesis_status="failed"，整体仍返回
REFERENCE_ANALYZED（不让 VLM 抖动影响 GUI）。

#### 4.2.1 请求

```json
{
  "tool_name": "video_brief_synthesizer",
  "inputs": {
    "brief_path": "projects/users/<uid>/<pid>/analysis_<ts>/video_analysis_brief.json",
    "userid":     "<wechat_openid>",
    "project_id": "vclaw-<uuid>",
    "max_frames": 16,
    "max_tokens": 4096,
    "model":      "sonnet"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| brief_path | string | 是 | video_analyzer 写出的 brief 路径（应等于 video_analyzer 响应里 artifacts[0]） |
| userid / project_id | string | 是 | 同 video_analyzer |
| max_frames | int | 否（默认 16） | VLM 看多少帧，太多会爆 token |
| max_tokens | int | 否（默认 4096） | VLM 输出上限 |
| model | string | 否 | 覆盖默认 sonnet；OM 校验可用模型列表 |

#### 4.2.2 响应

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"synthesis\":{\"status\":\"ok\"},\"output_path\":\"projects/users/<uid>/<pid>/analysis_<ts>/research_brief.json\"}"
    }
  ],
  "isError": false
}
```

status 三态：

| status | 含义 | vclaw 行为 |
|---|---|---|
| ok | 成功；output_path 是 research_brief.json 的 OM 路径 | 存 research_brief_path，记 synthesis_status="ok" |
| skipped | OM 决定不跑（brief 太薄 / ANTHROPIC_BASE_URL 缺失） | 不存路径，记 synthesis_status="skipped" |
| failed 或 missing | 错误 | 不存路径，记 synthesis_status="failed" |

output_path 在 status ≠ ok 时**不应**返回；vclaw 在 ok 但
output_path 为空时也兜底为 failed。

### 4.3 14 键 summary 投影（vclaw 端 contract）

vclaw 把 extractReferenceSummary(tr.Result) 的结果放进
GatewayResponse.Raw 字段，GUI / OpenClaw 都能消费。OM 不需要
直接产出这个形状，但**字段名必须与 §4.1.2 一致**——vclaw 端按
这套 key 读取，名字差异会直接落到缺字段、显示成空。

| 14 键 | 来源路径 | 类型 |
|---|---|---|
| platform | source.type | string |
| duration_seconds | source.duration_seconds | number |
| aspect_ratio | source.aspect_ratio | string |
| scene_count | structure_analysis.total_scenes | int |
| motion_count | len(structure_analysis.motion_breakdown) | int |
| primary_motion_type | motion_breakdown[0].motion_type | string |
| pacing_style | pacing_profile.pacing_style | string |
| avg_scene_duration_seconds | pacing_profile.avg_scene_duration_seconds | number |
| cuts_per_minute | pacing_profile.cuts_per_minute | number |
| tone | content_analysis.tone | string |
| summary | content_analysis.summary | string |
| language | content_analysis.language | string |
| suggested_pipeline | replication_guidance.suggested_pipeline | string |
| complexity | replication_guidance.estimated_complexity | string |
| has_transcript | _analysis_meta.has_transcript | bool |
| keyframe_count | _analysis_meta.keyframe_count | int |

加上 M1-E 元字段（在 vclaw 的 Raw 里与 14 键并列，vclaw 自动从
内部 8 列复制进去，OM 不必单独返回）：

| 字段 | 来源 | 说明 |
|---|---|---|
| brief_path | video_analyzer 的 artifacts[0] | vclaw 自动复制 |
| research_brief_path | video_brief_synthesizer 的 output_path（仅 status=ok） | 同上 |
| synthesis_status | 综合 ok / skipped / failed / none | none 表示没跑 synthesizer |

### 4.4 终态语义（vclaw status_map 14 档）

status_map 在 internal/handler/status_map.go 维护，OM 端不需要
关心；这里列出 vclaw 如何根据 OM 的产物决定 status，OM 自测时
可以照表 §4.4.1 的输入断言对应 status。

#### 4.4.1 video_analyzer 响应 → vclaw 终态

| 输入条件 | vclaw 终态 |
|---|---|
| tr.OK == false 或 transport 错 | 502（**不**更新 DB status） |
| tr.OK == true 且 has_transcript=true | REFERENCE_ANALYZED |
| tr.OK == true 且 has_transcript=false 且 len(steps_failed) == 0 | REFERENCE_ANALYZED（缺转录但其它步骤全 OK） |
| tr.OK == true 且 has_transcript=false 且 len(steps_failed) > 0 | **FAILED** |
| tr.OK == true 且合成器失败 / 跳过 | 仍 REFERENCE_ANALYZED（合成器失败/跳过不影响终态，只写 synthesis_status 列） |

#### 4.4.2 OM 自检建议

- [ ] video_analyzer 在 5xx / ok=false 时**不要**填充 _analysis_meta——transport 错走 vclaw 502 路径
- [ ] video_analyzer 在 Whisper / ffmpeg / VLM 失败时，把失败步骤名填进 steps_failed（字符串数组），**不要** throw 让 transport 失败——vclaw 需要这些信息判终态
- [ ] video_brief_synthesizer 在 ANTHROPIC_BASE_URL 缺失时返回 status="skipped" 而不是 "failed"——vclaw 据此区分"OM 自愿跳过"和"真出错"
- [ ] research_brief.json 至少含 content_analysis / style_profile / replication_guidance 三段（与 video_analyzer 的 brief 同构）——OpenClaw 在 M2-A 的 preamble 注入会读它们

### 4.5 vclaw ↔ OM 拆解字段对照速查

```
vclaw 提交 (video_analyzer.inputs)    OM 应回 (text JSON inside content[])
─────────────────────────────        ──────────────────────────────────
source (URL or local path)       →   source.{type, url, local_path, duration_seconds, aspect_ratio}
project_id, userid              →   artifacts[].<om path>            [0] = video_analysis_brief.json
analysis_depth                  →   structure_analysis / content_analysis depth-tinted output
transcript_path (optional)      →   (skip Whisper; reuse file)
max_keyframes (default 20)      →   structure_analysis.keyframes ≤ N
max_duration_seconds (default 600) → fail-fast or trim if exceeded
language (optional)             →   content_analysis.language; Whisper forced if set

vclaw 提交 (synthesizer.inputs)
─────────────────────────────
brief_path (= artifacts[0])      →   reads video_analysis_brief.json + frames + transcript
userid, project_id              →   output_path = projects/users/<uid>/<pid>/analysis_<ts>/research_brief.json
max_frames (16), max_tokens (4096), model (sonnet)

OM 应回 (synthesizer)
─────────────────────
synthesis.status = ok|skipped|failed
output_path (only when status=ok)
```

## 5. 预览拼接（可选）：工具 `video_stitch`

低分辨率预览用。vclaw 调用 `execute_tool("video_stitch",
{operation:"preview_stitch", clips:[...], output_path:"..."})`，期望返回
`output_path` 与 `file_url`/`share_url`。OM 实现后可被预览链路复用。

---

## 6. vclaw ↔ OM 字段对照速查（渲染路径）

```
vclaw 提交 (video_compose.inputs)        OM 应回 (text JSON)
─────────────────────────────────        ─────────────────────────────────
operation: "render"                →     固定接收 render
edit_decisions                     →     用于剪辑决策（含 metadata.effects/subtitles）
asset_manifest (必填)              →     渲染素材清单（缺则 vclaw 先 400）
scene_plan                         →     场景计划
output_path (OM 本地路径)          →     output / output_path / video_path
profile: high_res                  →     质量档
_job_id (vclaw 所有权, 非 OM id)   →     （忽略用途，仅归属）
                                     ←     ok / success: true
                                     ←     render_job_id (仅异步)
                                     ←     share_url / file_url (强烈建议)
                                     ←     status: rendered/completed/published
```

## 7. 联调前 OM 侧自检清单

- [ ] `:8900/mcp` 支持 `initialize` → `notifications/initialized` 会话握手并返回 `Mcp-Session-Id`。
- [ ] `video_compose` 接收 `operation:"render"` + `asset_manifest`（必填）+ `edit_decisions`/`scene_plan`/`output_path`/`profile`/`_job_id`。
- [ ] 渲染结果回传 `output`/`output_path` 本地路径，且**尽量回传 `share_url`/`file_url`**。
- [ ] 成功 JSON 含 `ok:true`（建议同时给 `data` 内层与顶层，键名见 §2.2）。
- [ ] 若异步：回传 `render_job_id` 并实现 `get_render_status`（§3）。
- [ ] 失败返回 `ok:false` + `error`，或 `isError:true`，便于 vclaw 标记 FAILED。
- [ ] `_job_id` 仅用于归属，不冲突到 OM 自身 job 注册表。
- [ ] `video_analyzer` 输出 `artifacts[0]` = `video_analysis_brief.json` 的 OM 相对路径（§4.1）。
- [ ] `video_analyzer` 在部分步骤失败时填 `_analysis_meta.steps_failed`，**不要 throw**（§4.4）。
- [ ] `video_brief_synthesizer` 在 `ANTHROPIC_BASE_URL` 缺失时返回 `synthesis.status="skipped"`（§4.2）。

---

## 8. 参考

- `internal/openmontage/client.go` — vclaw 侧 MCP 客户端与字段解析
  （`RenderVideo` / `GetRenderStatus` / `populateRenderHandle` /
  `populateRenderStatus`）。
- `internal/handler/preview.go` — `previewHandler` 组装 `RenderRequest` 并调用
  `RenderVideo`（既有 `/api/video-projects/:id/render` 路径，已验证可用）。
- `internal/handler/remix_package.go` — remix 渲染入口
  `RemixRenderDispatchHandler`（M2-A 已接入 OpenClaw；reference 块
  缺时 WARN 而非 400，pre-M1 包仍可 dispatch）。
- `internal/handler/gateway_verbs.go` — `AnalyzeReferenceVideoHandler` /
  `SynthesizeReferenceBriefHandler` / `extractReferenceSummary` 端到端定义
  （M1-A/B/C 拆解链路在 vclaw 侧的入口；与 §4 一一对应）。
- `internal/store/store_reference.go` — `UpdateProjectReferenceBrief` /
  `GetProjectReferenceMeta` / `RecordReferenceAnalysisFailure`（M1-A 8 列写入）。
- `internal/handler/status_map.go` — 14 档 `UnifiedStatus` 常量 + 4 个
  reference_analyzed OM alias（§4.4）。
- `config.yaml` — `openmontage.mcp_url` / `mcp_token` / `backlot_base_url`。
- `docs/openclaw-integration.md` — vclaw 与 OpenClaw/OM 的整体分层说明。
