# 固定接口与数据契约

计划调度版本为1.1，业务RemixPackage仍为v2。契约与共享fixture由I统一冻结，变更通过05中的CR流程；三条开发线不得自行改字段或兼容语义。见[并行交接](05-parallel-delivery.md)。

本文为**拟实现契约**，不是当前服务能力说明。T01 将其落为 schema / 类型 / fixtures。字段需变更时，先更新本文版本、共享样例和兼容说明，再做下游任务；单卡执行者不能独自改名。

## C1. 身份、资源和路径

1. principal 只能来自 OM 已验证的 MCP context / 持久绑定；不信任 body 里的 user_id、tenant_id、queue_owner_id。
2. Go 从已认证请求确定 actor_user_id，创建 job 时持久化。worker 使用该身份签署每次到 OM 的请求，包括 initialize 和 tools/call。签名复用现有断言规则，覆盖 method/path/session/body；不能改用服务 principal 去读用户素材。
3. OM 路径仅通过 `ProjectWorkspace.for_current_principal(project_id)` 解析。v1/v2/canary read_roots 由现有工厂处理；不拼 `projects/users`，不从客户端路径推 namespace。
4. asset_id 在“principal + project_id”内解析；素材 record 包含规范 file_key、完整 SHA256、实际媒体类型、bytes、duration_ms（适用时）、width/height、创建来源。
5. record 写入须原子、重复登记幂等。优先复用现有 asset id；旧 upload 返回 id 保留，新响应补齐 `asset_id`、`file_key` 与 `project_id`。碰到同 id 不同 SHA 必须冲突，不覆盖。
6. file_key 是 repo-relative POSIX 标识，不是浏览器 URL，更不是客户端 Windows 路径。请求中的 file_key 必须和已登记 record 一致；服务器只使用 record 解析出来的路径。
7. 每次读取、渲染、导出前再次校验 resolved path 位于允许 read_roots，拒绝路径穿越、另一用户目录和逃逸 symlink。上传、关键帧、TTS、渲染输出均需要登记。
8. 浏览器预览回读按 asset_id/project_id 定位；不把任意 B 盘或 `/opt` 绝对路径透传给客户端取文件。

## C2. 时间和镜头

- 持久 RemixPackage 使用整数毫秒；渲染编译产物使用整数帧。禁止到处混用 start/end 秒字段。
- output_frame(t_ms) = floor(t_ms * fps / 1000 + 0.5)。各段边界独立从全局时间换算，duration_frames=end_frame-start_frame；禁止先逐段四舍五入再累加造成漂移。
- fps 首版固定 30。源视频裁剪用源时间秒传入 renderer 的准确 seek/trim 机制；如果组件只接受帧值，必须按其实际 API 的帧率语义换算，并用 24/30/60fps 源片测试，不能假定源 fps = 输出 fps。
- 场景源区间 `[source_start_ms,source_end_ms)`；输出区间 `[start_ms,end_ms)`；v2 首版禁止变速，因此两者时长相等。
- timeline 按 start_ms 排序，scene_id 唯一，首段从 0 开始，无缺口/重叠，最后一段 end_ms=duration_ms；零长、负长、NaN、Infinity 直接拒绝。
- 重复 asset_id 允许；重复 scene_id 拒绝。镜头顺序来自 timeline，绝不来自 session assets。
- replace 图片持续整个场景，默认 contain + 固定背景，不裁掉产品。fit 可选 cover，须来自用户选择。
- cut 不改变时长。fade 在新场景头部的 duration_ms 范围完成，底层显示上一场景最后一帧；不减短总时长、不隐式制造重叠镜头。超过相邻镜头可用时长拒绝。不是对原片任意转场的自动复刻。

## C3. RemixPackage v2 样例

以下为共享正例 `three-scenes.json` 的语义骨架。`asset_id/file_key` 示例值是占位，集成测试由素材登记步骤返回真值。

```json
{
  "schema_version": 2,
  "project_id": "p-demo",
  "processing_mode": "direct",
  "source_asset_id": "video-1",
  "assets": [
    {"asset_id":"video-1","file_key":"SERVER_RETURNED_VIDEO_KEY","media_type":"video"},
    {"asset_id":"image-1","file_key":"SERVER_RETURNED_IMAGE_KEY","media_type":"image"}
  ],
  "timeline": {
    "fps": 30,
    "duration_ms": 15000,
    "width": 1080,
    "height": 1920,
    "scenes": [
      {"scene_id":"s1","mode":"keep","asset_id":"video-1","start_ms":0,"end_ms":2000,"source_start_ms":0,"source_end_ms":2000,"fit":"contain","transition_in":{"type":"cut","duration_ms":0}},
      {"scene_id":"s2","mode":"replace","asset_id":"image-1","start_ms":2000,"end_ms":7000,"source_start_ms":2000,"source_end_ms":7000,"fit":"contain","transition_in":{"type":"cut","duration_ms":0}},
      {"scene_id":"s3","mode":"replace","asset_id":"image-1","start_ms":7000,"end_ms":15000,"source_start_ms":7000,"source_end_ms":15000,"fit":"contain","transition_in":{"type":"cut","duration_ms":0}}
    ]
  },
  "audio": {"mode":"source","narration_asset_id":null,"volume":1},
  "subtitles": [{"id":"c1","start_ms":100,"end_ms":1800,"text":"示例字幕"}],
  "render": {"runtime":"remotion","renderer_family":"reference-remix","publish":true},
  "review": {"status":"confirmed","confirmed_scene_ids":["s1","s2","s3"],"audio_confirmed":true}
}
```

`review` 是用户编辑确认的记录，服务端检查完整性；**不等同于自动绕过 OM checkpoint 的 human_approved**。接入正式生产时，由已认证用户操作对应到适用 pipeline 的审核记录；不能信任任意 body 标志直接越过所需 gate。

draft 允许未完成资产：scene 可带 `pending_reason`，asset_id 可为空，review.status=draft。保存可成功；submit 必须所有素材就绪、media 类型匹配、review 确认、音轨和时间校验通过。不要把 draft 校验和 submit 校验混为一个函数。

首版尺寸枚举：1080×1920、1920×1080、1080×1080；最大 200 镜头、总长 600000ms、每镜头至少 1 输出帧。超限返回 CAPACITY_EXCEEDED，不切片、不截断。以后支持更长任务需要单独版本；此上限覆盖原 24 镜头遗漏问题。

旧 schema_version=1 仍能读写草稿；不在服务器静默转成可渲染 v2。GUI 显示“需要补齐源视频/确认时间线”，显式迁移保存新版本。旧版本内容不可覆盖。

## C4. 音频与字幕：无声音克隆

audio.mode 三选一：

| mode | 输入 | 输出 |
|---|---|---|
| source | source_asset_id | 按场景源区间裁剪原片音轨，replace 场景也保留对应原声；无源音轨时返回 SOURCE_AUDIO_MISSING，用户可选 mute |
| mute | 不需要音频素材 | 成片无音轨或明确静音轨，验收时说明采用哪种 |
| tts | narration_asset_id | 只放已有音色 TTS 生成的配音，所有源视频 muted；不混入原声 |

首版不做自动 ducking 或原声+TTS 混音。用户确定的 volume 范围 0–1，默认 1。

TTS 流程：选择已可用音色 → 调现有 voicebox_tts(text,profile_id,language,engine,output_path) → 获取返回音频 → 通过 OM 登记 → 保存 narration_asset_id。按实际线上 schema 传参，不额外发 operation；不调用 clone_voice 系列。

TTS 处理范围只包括已有音色。profile_id 是已有配置引用，不创建新 profile。前端文案统一“已有音色配音”，不显示“录制/克隆我的声音”。

音频短于时间线：尾部补静音。长于时间线超过一输出帧：返回 NARRATION_TOO_LONG，并显示实际时长，用户修改文案或时间线；不截音、不自动加速。测试默认用本地正弦波 WAV 验证混音机制，真实 TTS 只做授权样本。

字幕保留逐条时间戳；编辑只改 text，不按 paragraph 数重新分配时间。新增字幕必须填写有效时间。强校验 0≤start<end≤duration，同一轨禁止重叠。burned_in 源字幕无法靠本计划自动擦除；若新增字幕可能叠字，界面明确提示。字幕语言/字体使用项目已有 CJK 字体资源，不下载新字体。

## C5. 拟新增 OM 工具

下面名称均需在任务中实现并注册；工具薄包装位于 mcp_server.py，核心逻辑分文件。API 不接受自报 principal。

| 工具 | 参数 | 成功输出 |
|---|---|---|
| prepare_remix_analysis | project_id, source_asset_id | analysis_id, project_id, source_path, frames_dir, transcript_dir, audio_dir；路径只供后端工具参数使用 |
| register_remix_asset | project_id, analysis_id 可选, relative_path, expected_media_type | asset_id,file_key,project_id,media_type,sha256,duration_ms 等 |
| get_remix_asset | project_id,asset_id,include_preview=false | 元数据；include_preview=true 仅返回限尺寸图片预览的 mime/base64 |
| submit_remix_render | request_id, project_id, snapshot_json, snapshot_sha256 | job_id,status,project_id,snapshot_sha256 |
| get_remix_render_status | project_id,job_id | C7 状态对象 |
| retry_remix_publish | project_id,job_id,request_id | 同一个 render job 的发布状态 |

prepare 创建唯一 analysis 子目录；asset resolver 验证源视频属于本用户本项目。register 仅允许当前 project 工作区内、由 upload/analysis/TTS 路径产生的资源；不接受任意服务端路径作为用户素材。对输入和输出路径都验证，不能只校验输出。

analysis_path 用于 GUI 调现有 execute_tool，不放入持久 RemixPackage。GUI 将返回关键帧逐个 register，再用 asset_id 预览；source_path 仅暂态调用数据。

工具限制：预览生成最大边 640、单张响应建议≤512KiB，原始文件仍保留；prepare 元数据轻量返回；长 render 非阻塞受理；TTS/分解为有界长请求。

OM submit 内部以 registry 获取并调用确定性 video_compose 能力；不自动做新素材创作。新核心生产工具需继承 BaseTool，声明 schema/能力/运行环境并通过 registry 发现。

## C6. Go 接口与不可变快照

保留已有 PUT `/api/video-projects/:id/remix-package` 的 `{base_version,manifest}` 形式；支持 v2。服务端计算 ContentHash，返回 version/content_hash/manifest。

扩展现有 POST `/api/studio/video-projects/:id/render`：

```json
{"package_version":3,"idempotency_key":"client-generated-uuid"}
```

processing_mode 从指定版本快照读取，不再让请求覆盖。202 响应：

```json
{"job_id":"cp-job-1","project_id":"p-demo","package_version":3,"status":"queued","processing_mode":"direct"}
```

新增 GET `/api/studio/video-projects/:id/jobs/:jobId`、POST `.../:jobId/retry-publish`。均沿用 PrincipalAuth 与 renders:read/write scope，校验项目 tenant + job.project + actor 权限；禁止仅凭 job_id 访问。

受理事务同时写 production_job 与 outbox/job_queue；unique(tenant_id,project_id,idempotency_key)。重复键且同快照返回原 job；同键不同快照 409 IDEMPOTENCY_CONFLICT。保存 actor_user_id、package_version、快照字符串/hash、处理模式、upstream_job_id；不要存 JWT 或 refresh token。

快照跨语言哈希：Go 使用实际已持久化的 UTF-8 manifest 字符串，计算 SHA256；传给 OM `snapshot_json` 为该**原始字符串**，不让 Python 重新 JSON 序列化后算 hash。OM 先按 UTF-8 校验 hash，再 parse。客户端提交前仅指定 version，不自报权威快照。

worker 使用 CP job_id 作为 OM request_id。OM 幂等键包含 principal + project_id + request_id；同键同 hash 重试返回同 job，异 hash 冲突。网络“发出成功但响应丢失”不能变成第二次渲染。

新 remix job_type 独立命名，禁止落入当前 legacy start_production 的 sim-id 兜底。缺 OM/OpenClaw 配置返回真实 blocked/failed，不造 run_id。

## C7. 状态与恢复

统一状态：queued → validating → rendering → rendered → publishing → published；另有 failed。rendered 是媒体可用但发布未完成；不是 published。

```json
{
  "job_id":"cp-job-1",
  "project_id":"p-demo",
  "package_version":3,
  "status":"publishing",
  "phase":"publish",
  "progress":90,
  "render_ready":true,
  "artifact":{"asset_id":"final-1","file_key":"SERVER_RETURNED_KEY"},
  "share_url":null,
  "error":null,
  "updated_at":"2026-09-05T00:00:00Z"
}
```

failed 带 `error={code,message,retryable,scene_id?}` 和 phase；发布失败保留 render_ready/artifact。重试发布只执行上传/分享，不重渲染、不再预扣渲染费用。已有账单调用若参与，复用其幂等键和结算规则，不能在本计划中另定费率。

进度只显示已确认状态；不把本地定时器的增长当完成证据。published 必须有合法 share_url；publish=false 可在 rendered 作为交付终点，并提供经过用户权限校验的下载入口。轮询超时保留“仍处理中/连接中断”，不改为完成，也不自动重提 job。

OM status/retry 必须校验 job owner，即使重启换 MCP session，同 principal 仍可访问；另一 principal 即使知道 job_id 也拒绝。

Go / OM 持久状态写入与队列领取都需要原子操作。运行中崩溃，先检查完成产物及 render_report，再决定恢复或标记 INTERRUPTED；没有证据不能伪装 completed。outbox 必须有租约/过期领取恢复，避免永远停留 running。

## C8. 错误码

固定首批：ASSET_NOT_FOUND、ASSET_FORBIDDEN、ASSET_CHANGED、PROJECT_MISMATCH、INVALID_TIMELINE、SOURCE_RANGE_INVALID、SOURCE_AUDIO_MISSING、NARRATION_TOO_LONG、UNRESOLVED_SCENE、UNSUPPORTED_TRANSITION、CAPACITY_EXCEEDED、RUNTIME_UNAVAILABLE、VERSION_CONFLICT、IDEMPOTENCY_CONFLICT、AUTH_REQUIRED、UPSTREAM_TIMEOUT、INTERRUPTED、PUBLISH_FAILED。

Go HTTP 使用 400 语法、401 身份、403 权限、404 不可见资源、409 版本/幂等冲突、422 业务不可渲染、503 依赖不可用。MCP 工具失败仍用结构化 success=false/error.code，而非依赖错误文本正则猜测。

## C9. 上传与重试

保持当前 start/append/complete 向后兼容。新增 `operation=status`，只允许同 principal + 同 session 查询 offset/total/state；重复 append 的已写区间若 bytes/hash 一致返回当前 offset，不同返回冲突；重复 complete 在上传完成状态保留期内返回同 asset。

首版明确保证：同会话网络恢复可续传；应用重启导致 session 丢失时重新 start，但复用已完成 asset 并避免重复发布。不要把同 principal 跨新 session 任意接管 upload_id 作为省事修复；跨会话真正续传是独立授权设计，本版不承诺。

客户端本地路径变化、文件 SHA 变化必须重新上传；不可带旧 SHA 强行 complete。超时不盲目推进 offset，先 status 对账。

## C10. 审核与确定性生产边界

GUI 最终确认必须展示所提交版本、镜头保留/替换范围、音频策略、已选 runtime、是否发布，以及“按此快照完成确定性合成和发布”的授权范围。不能只把一个客户端 review.status 字符串视为已经获得所有审核。

Go 在已认证确认请求中记录 actor、package_version、snapshot_hash、确认时间和范围；OM 通过可信请求链获取该记录，追加 decision_log 的 approval_policy。只有该记录明确覆盖且已有合法产物的阶段，才能按当前 pipeline/checkpoint 规则填写 human_approved；必须检查适用 manifest 的 gate，禁止全局硬编码为 true。

T07 负责合法产物映射、T14 负责可信确认记录、T16 负责确认界面。T12 在缺少必要确认时返回待审核，不运行生成。用户确认后又改任何快照字段，需要新的版本确认。记录声音策略但不包含声音克隆。

如果现有 pipeline API 不能表达这个确定性任务的审批范围，应在 T07 报告准确冲突并补一张受控适配子任务；不得通过删除 governance 校验或伪造 completed checkpoint 来使渲染通过。

## C11. 功能开关与生产模式

以下均为拟新增配置，T21/T23实现；名称固定，避免各端发明不同的开关。

- Go `VCLAW_REMIX_V2_ENABLED`：缺省false；只控制新v2任务受理，关闭时明确503，旧草稿读取仍可用。
- GUI `VITE_REMIX_V2_ENABLED`：缺省false；启用前检查后端能力。false不表示允许把旧照片视频流程伪装成已修复重构。
- Go `VCLAW_DEPLOYMENT_MODE`：production/development/test，缺省production。若仓库已有等价配置，T00记录后使用明确映射，不能同时留下互相矛盾的两个真相源。
- production + 非空DevUserID应启动失败并提示变量名，不输出值；开发测试需要显式development/test。此行为先改代码/示例，真实环境切换留T23授权发布窗口。

OM新增工具向后兼容注册；实际渲染仍要求完整权限、契约和runtime检测。功能开关不是安全边界，不能用“只在UI隐藏”代替服务端校验。
