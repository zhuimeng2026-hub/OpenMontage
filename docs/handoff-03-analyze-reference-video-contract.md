# 任务交接 03：vclaw `analyze_reference_video` + `synthesize_reference_brief` OM 端实现

> 面向对象：OM 团队 / 实现工程师
> 日期：2026-09-06
> 来源仓库：`/opt/vclaw`（commit `77687da` — `m2b: openmontage-receiving-contract.md adds analyze_reference 端点契约`）
> 状态：**契约已交付，等待 OM 实现**

## 1. 任务目标

OM 端 `/mcp` 工具列表需要新增（或确保实现）3 个工具，使 vclaw 后端
能完成参考视频拆解 + 可选 LLM 填充 + 最终渲染全链路：

| 工具 | 调用方 | vclaw 侧入口 | 必需性 |
|---|---|---|---|
| `video_analyzer` | vclaw `AnalyzeReferenceVideoHandler` (M1-B) | `POST /api/gateway/analyze-reference-video` | **必** |
| `video_brief_synthesizer` | vclaw `SynthesizeReferenceBriefHandler` (M1-B) | `POST /api/gateway/synthesize-reference-brief` | 条件（`extra.synthesize=true` 时） |
| `video_compose` | vclaw `RemixRenderDispatchHandler` (M2-A) | `POST /api/studio/video-projects/:id/render` | **必** |

完整契约见 `/opt/OpenMontage_Voicebox/docs/openmontage-receiving-contract.md` §4.1 / §4.2 / §2.1（已附在本目录）。

## 2. 当前 OM 实例实测缺口（2026-09-06 端到端验证）

vclaw 后端 + GUI 在 `192.168.20.173:8900` 这个 OM 实例上跑 curl 冒烟：

```
POST /api/gateway/analyze-reference-video
  → vclaw.om.CallTool("video_analyzer", ...)
  → OM: {"isError":true, "Unknown tool: video_analyzer"}  ← 缺
  → vclaw 502 Bad Gateway (正确透传)

POST /api/studio/video-projects/:id/render  (M2-A dispatch)
  → vclaw.om.CallTool("video_compose", ...)
  → OM: {"isError":true, "Unknown tool: video_compose"}   ← 缺
  → vclaw 502 OM_DISPATCH_FAILED (正确透传)
```

`tools/list` 当前返回 36 个工具（含 `scene_detect`, `create_remotion_video_share`,
`weiyun_upload` 等），但**不含** `video_analyzer` / `video_brief_synthesizer` / `video_compose`。

### 2.1 关键发现：实现已在 git 里，仅 mcp_server.py 未重启

**OM 仓库 git log 显示三个相关提交（最新 2026-09-05）：**
```
c80dcee feat(scripts): add analyze_reference_video end-to-end pipeline runner
7f4d4b5 docs(bugs): mark video_analyzer keyframe bug as FIXED
1e163cf fix(analyzer): surface FrameSampler success=False at STEP 4
```

**OM 仓库代码已存在 `BaseTool` 子类**（路径 + 类名 + name 属性都对得上 vclaw 端期望）：
- `tools/analysis/video_analyzer.py:44` `class VideoAnalyzer(BaseTool): name = "video_analyzer"`
- `tools/analysis/video_brief_synthesizer.py:72` `class VideoBriefSynthesizer(BaseTool): name = "video_brief_synthesizer"`
- `tools/video/video_compose.py:203` `class VideoCompose(BaseTool): name = "video_compose"`

注册机制（`tools/tool_registry.py::ToolRegistry.discover()`）会 walk `tools/` 包并自动 `register_module` 所有 `BaseTool` 抽象类的具体子类——所以这 3 个类应该已经被发现并注册。

**实际原因**：live `mcp_server.py`（PID 251569）已跑 **13 小时 55 分钟**——新工具 commit 落地后没重启，所以 `_tools` dict 里没有这 3 个。

**最小修复**（不用写新代码）：
```bash
cd /opt/OpenMontage_Voicebox
# 找到 mcp_server 进程（PID 251569）并替换
kill 251569
nohup ./.venv/bin/python3 mcp_server.py > logs/mcp_server.log 2>&1 &
# 等待 2-3 秒后 tools/list 应该含 36 + 3 = 39 个工具
```

重启后用 vclaw 团队冒烟命令验证（见 §5）。
vclaw → OM 的 MCP streamable-http 链路本身工作（initialize + tools/call 都成功），
只是工具名 OM 没实现。

## 3. OM 端实现清单（已存在 git 中，需重启加载）

### 3.0 重启 mcp_server.py（最小可行修复）

按 §2.1 重启即可。下面是更深入的字段对齐 + 契约确认（即使重启后工作，仍建议核对一次契约）。

### 3.1 `video_analyzer`（必）

**输入**（per `openmontage-receiving-contract.md` §4.1.1）：
- `source` (URL or local path)
- `project_id`, `userid`
- `analysis_depth` (`transcript_only` / `standard` / `deep`，默认 `standard`)
- `transcript_path` (optional)
- `max_keyframes` (default 20), `max_duration_seconds` (default 600)
- `language` (optional)

**输出**（per §4.1.2）—— 必须含这些字段供 vclaw 14 键 summary 投影：
- `source.{type, url, local_path, duration_seconds, aspect_ratio}`
- `structure_analysis.{total_scenes, pacing_profile.{pacing_style, avg_scene_duration_seconds, cuts_per_minute}, motion_breakdown[]}`
- `content_analysis.{tone, summary, language}`
- `replication_guidance.{suggested_pipeline, estimated_complexity}`
- `artifacts[0]` **必须是** `video_analysis_brief.json` 的 OM 相对路径
- `_analysis_meta.{has_transcript, keyframe_count, steps_failed[]}` —— vclaw 据此判终态

**异常处理**：transient 失败时填 `steps_failed`（如 `["transcribe", "scene_detection"]`），
**不要 throw / 5xx** —— vclaw 通过 `has_transcript=false + steps_failed 非空` 判 FAILED。

### 3.2 `video_brief_synthesizer`（条件）

仅当 `extra.synthesize=true` 且 `ANTHROPIC_BASE_URL` 非空时调用。

**输入**：`brief_path` (= `video_analyzer` 的 `artifacts[0]`)、`project_id`、`userid`、
`max_frames` (16)、`max_tokens` (4096)、`model` (sonnet)。

**输出**：`synthesis.status ∈ {ok, skipped, failed}`：
- `ok` → 同时返回 `output_path` = `research_brief.json` 路径
- `skipped` → OM 主动选择不跑（brief 太薄 / ANTHROPIC_BASE_URL 缺失），不算错
- `failed` → 错误，**不要 throw**

### 3.3 `video_compose`（必，M2-A dispatch 路径）

**输入**（per §2.1）：
- `operation`: 固定 `"render"`
- `edit_decisions.metadata`: 含 vclaw 注入的 `reference.{brief_path, research_brief_path, synthesis_status, has_transcript, keyframe_count, summary_json}`，加上 GUI 的 `aspect_ratio` / `subtitles` / `effects`
- `asset_manifest`, `scene_plan`, `output_path`, `profile: "high_res"`, `_job_id`, `creative_brief`

**输出**：`output` (本地 mp4 路径) + `share_url` (公网 URL)。**video_compose 当前对 vclaw 是同步调用**（成功 = 已完成，无轮询）。

## 4. 自检清单（重启 mcp_server.py 后跑一遍）

对应 `openmontage-receiving-contract.md` §7 末尾追加的 3 项：

- [ ] `mcp_server.py` 重启后，`tools/list` 包含 `video_analyzer`, `video_brief_synthesizer`, `video_compose`（工具数应从 36 → 39）
- [ ] `video_analyzer` 的 `artifacts[0]` = `video_analysis_brief.json` 的 OM 路径
- [ ] `video_analyzer` 在部分步骤失败时填 `_analysis_meta.steps_failed`，不 throw
- [ ] `video_brief_synthesizer` 在 `ANTHROPIC_BASE_URL` 缺失时返回 `status="skipped"`
- [ ] `video_compose` 同步返回 `output` + `share_url`

## 5. 端到端验收命令（vclaw 团队提供，复用本仓库冒烟脚本）

OM 团队实现完上面 3 个工具后，vclaw 这边跑：

```bash
# 1. 登录拿 desktop JWT（带 renders:write scope）
ACCESS=$(JWT_SECRET=dev-change-me-please SMOKE_TID=$TENANT SMOKE_UID=$USER \
  go run /opt/vclaw/cmd/smoke_mint)  # 一次性 helper，跑完可删

# 2. 建项目
PID=$(curl -sS -X POST http://localhost:8080/api/video-projects \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"name":"om-smoke"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

# 3. 拆解（这步要 video_analyzer）
curl -sS -X POST http://localhost:8080/api/gateway/analyze-reference-video \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PID\",\"extra\":{\"source\":\"https://www.youtube.com/watch?v=dQw4w9WgXcQ\",\"synthesize\":true}}"
# 期望: HTTP 200, status="REFERENCE_ANALYZED", raw.brief_path / research_brief_path / synthesis_status 齐全

# 4. 渲染 dispatch（这步要 video_compose）
curl -sS -X PUT http://localhost:8080/api/video-projects/$PID/remix-package \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d "{\"base_version\":0,\"manifest\":{\"schema_version\":1,\"assets\":[],\"timeline\":{\"scenes\":[]},\"reference\":{\"brief_path\":\"projects/...\"}}}"

curl -sS -X POST http://localhost:8080/api/studio/video-projects/$PID/render \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"package_version":1}'
# 期望: HTTP 202, output_path + share_url 齐全
```

## 6. 联系方式

- vclaw 后端 owner：`/opt/vclaw/internal/handler/gateway_verbs.go::AnalyzeReferenceVideoHandler`
- 契约源文件：`/opt/vclaw/docs/openmontage-receiving-contract.md`（本目录下副本）
- vclaw 测试 stub：`/opt/vclaw/openclaw/solutions/product-video-production/mcp/openmontage-adapter.mjs`（dev mock 用，OM 实现后可废弃）

—— vclaw 团队 @ 2026-09-06
