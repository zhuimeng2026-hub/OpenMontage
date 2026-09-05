# 任务交接 02：OpenMontage 混合时间线、静态图片正式渲染、逐场景时长与恢复

> 面向对象：接手实现的 LLM / 工程师  
> 日期：2026-09-04  
> 目标仓库：应由接手者确认实际 OM checkout；用户指定文档目录为 `C:\OpenMontage\_voicebox\docs`。  
> 状态：待实现；`video-template-remix` 当前为 beta，不能仅凭已有 Remotion 代码宣称满足本任务。

## 1. 任务目标

让 OpenMontage 接收一个显式、冻结、可重放的素材包，把原视频保留片段、用户静态图片、用户视频、TTS、字幕和音乐按逐场景时间轴合成为完整 MP4，并支持异步状态、幂等提交、进程重启恢复和可诊断失败。

目标最小案例：

```text
原视频 0.0-3.2 秒
-> 用户图片展示 3.2-6.7 秒（带正式运动效果）
-> 原视频 10.5-14.0 秒
-> TTS/字幕/音乐混合
-> 输出可播放 MP4，逐镜头漂移 <= 1 帧
```

## 2. 当前代码事实与差距

实施前必须阅读：

- `pipeline_defs/video-template-remix.yaml`
  - 已定义 remix 流程，但稳定性为 beta。
- `schemas/artifacts/scene_plan.schema.json`
  - 有 `start_seconds/end_seconds`，但没有正式表达原视频 source range、替换资产与 slot policy 的完整契约。
- `schemas/artifacts/asset_manifest.schema.json`
  - 当前主要使用项目相对 `path`；需要补足来自 VClaw 的显式资产引用和完整性字段。
- `schemas/artifacts/edit_decisions.schema.json`
  - cut 只有字符串 `source` 与 `in_seconds/out_seconds`，源类型语义不够明确。
- `tools/video/video_compose.py`
  - 已有 asset ID 解析、本地资产 staging、FFmpeg 与 Remotion 路径，但需正式支持显式混合时间轴、强校验和逐场景漂移验证。
- `remotion-composer/src/Explainer.tsx` 及其他 composition
  - 必须验证真实视频和静态图在同一个时间轴中按每个 cut 时长渲染，不能只验证图片幻灯片。
- `lib/workbuddy_session.py`、`lib/render_queue.py`
  - 已有排队/恢复相关结构；需要把 OM job registry、幂等与重启恢复契约补完整，并验证 Windows 行为。

已知风险：`lib/workbuddy_session.py` 的文件锁必须在 Windows 10 上可用，不能依赖 Unix-only `fcntl`。

## 3. 正式输入契约

建议新增一个面向外部调度层的 `remix_render_request` schema，不能让 VClaw/OpenClaw 直接拼凑内部 Remotion props。

```json
{
  "schema_version": 1,
  "request_id": "req_01J...",
  "idempotency_key": "render:tenant_1:project_1:package_3",
  "tenant_id": "tenant_1",
  "project_id": "project_1",
  "package_version": 3,
  "package_content_hash": "sha256-hex",
  "output": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "codec": "h264"
  },
  "assets": [
    {
      "asset_id": "asset_reference",
      "file_key": "assets/reference/source.mp4",
      "type": "video",
      "sha256": "..."
    },
    {
      "asset_id": "asset_product_01",
      "file_key": "assets/user/product-01.png",
      "type": "image",
      "sha256": "..."
    }
  ],
  "timeline": {
    "scenes": []
  },
  "audio": {},
  "subtitles": {}
}
```

所有文件必须通过项目资产根目录和 `file_key` 解析。外部请求不得直接传任意 Windows 绝对路径；OM 内部解析后也必须验证路径仍位于项目根目录内。

## 4. 正式场景契约

每个场景同时声明“输出时间”和“输入源范围”。禁止把二者混为 `in_seconds/out_seconds` 后靠文件扩展名猜语义。

### 4.1 保留原视频片段

```json
{
  "scene_id": "scene_001",
  "timeline_start_seconds": 0.0,
  "duration_seconds": 3.2,
  "source": {
    "type": "original_video",
    "asset_id": "asset_reference",
    "source_range": {
      "start_seconds": 0.0,
      "end_seconds": 3.2
    }
  },
  "fit": "cover",
  "audio_policy": "preserve_source"
}
```

### 4.2 静态图片替换片段

```json
{
  "scene_id": "scene_002",
  "timeline_start_seconds": 3.2,
  "duration_seconds": 3.5,
  "source": {
    "type": "image",
    "asset_id": "asset_product_01"
  },
  "fit": "contain",
  "background": "blur_fill",
  "motion": {
    "type": "ken_burns",
    "from_scale": 1.0,
    "to_scale": 1.08,
    "easing": "ease_in_out"
  },
  "audio_policy": "timeline_mix"
}
```

### 4.3 用户视频替换片段

```json
{
  "scene_id": "scene_003",
  "timeline_start_seconds": 6.7,
  "duration_seconds": 3.5,
  "source": {
    "type": "video",
    "asset_id": "asset_user_clip",
    "source_range": {
      "start_seconds": 1.0,
      "end_seconds": 4.5
    }
  },
  "fit": "cover",
  "audio_policy": "mute_source"
}
```

`source.type` MVP 至少支持：

- `original_video`
- `image`
- `video`

未上传完成或待生成素材不能进入 render。`pending_replacement`、`pending_generation` 必须在请求校验阶段返回 `RENDER_INPUT_INCOMPLETE`。

## 5. 时间轴归一化与强校验

在进入 FFmpeg/Remotion 前建立唯一的 normalized timeline，不允许多个 renderer 各自解释原始输入。

建议新增纯函数模块，例如 `lib/remix_timeline.py`，输出：

```text
scene_id
source_kind
resolved_path
source_in_seconds
source_out_seconds
timeline_start_seconds
duration_seconds
start_frame
duration_frames
end_frame_exclusive
```

校验规则：

- `fps > 0`，宽高为正数。
- `duration_seconds > 0`。
- `timeline_start_seconds >= 0`。
- 场景按时间排序后不得重叠；MVP 建议也不允许空洞。
- `source_range.end > source_range.start`。
- 视频 source range 不得超过 ffprobe 时长，允许误差最多半帧。
- 图片不能携带 source range。
- 每个 `asset_id` 必须存在、类型匹配、文件存在且位于项目资产根。
- `package_content_hash` 与冻结请求一致。
- 所有秒数统一用 decimal/float 输入，但进入渲染器前统一转换为整数帧。
- 使用明确舍入策略，例如累计边界 `round(seconds * fps)`，每个 scene 的 end frame 由下一个边界或总边界推导，避免逐段独立四舍五入造成累计漂移。
- 错误必须包含 `error_code`、`scene_id`、`asset_id` 和具体字段。

不得产生 `undefined * fps`、`NaN`、负帧或依赖数组顺序猜资产。

## 6. 静态图片正式渲染要求

静态图必须是一等 timeline source，而不是错误兜底或临时截图。

### 6.1 Remotion 路径

- 每个 scene 用 normalized `start_frame/duration_frames` 创建 `Sequence`。
- 图片用 `Img/staticFile` 或现有安全 staging 机制加载。
- `cover/contain/blur_fill` 行为必须固定并测试横图、竖图、透明 PNG。
- motion 参数由正式 schema 驱动；缺省 motion 必须有稳定默认值，不读取会话状态。
- 原视频使用 Remotion 的视频组件，按 `source_in_seconds` 偏移并静音/保留音轨。
- composition 总帧数等于 normalized timeline 的最后 `end_frame_exclusive`。

### 6.2 FFmpeg 路径

FFmpeg 只能作为显式选择的 runtime 或技术适配层，不能在 Remotion 失败时静默替换。

- 图片输入必须使用循环/持续时长语义并精确截断。
- 所有片段归一到相同分辨率、像素格式、fps 和 timebase。
- 视频剪切必须使用精确 duration，避免只靠 keyframe 快速 seek 造成超长片段。
- concat 前必须保证音视频流布局一致；无音轨场景需要显式静音轨。

### 6.3 Runtime 治理

请求必须携带已经在上游批准的 `render_runtime`。如果选择的 runtime 不可用，返回 `RENDER_RUNTIME_UNAVAILABLE`；不得静默从 Remotion 换成 FFmpeg 或反向替换。

## 7. 音频和字幕

- TTS、音乐、SFX、字幕都必须引用 asset manifest 中的冻结资产。
- narration segment 必须有明确 `start_seconds/end_seconds`。
- 字幕 cue 必须有 `start/end/text/language`，不得从当前聊天文本推断。
- 保留原视频音轨与外部 TTS 冲突时，必须按 scene 的 `audio_policy` 执行。
- 所有场景即使没有声音，也应让最终 timeline 的音频布局稳定。
- 混音后执行响度和峰值检查；失败信息指出具体轨道/时间范围。
- 最终字幕最后一个 cue 不得超过视频总时长超过 1 帧。

## 8. 异步任务、幂等和恢复

建立持久化 OM job registry。不得只依赖 MCP 会话内存或“最近一次任务”。

### 8.1 API/工具操作

至少提供稳定操作：

```text
submit_remix_render(request) -> om_job_id
get_render_status(om_job_id) -> status/progress/error/artifacts
cancel_render(om_job_id)
```

相同 `tenant_id + idempotency_key + package_content_hash` 必须返回原 `om_job_id`。若幂等键相同但 hash 不同，返回 `IDEMPOTENCY_CONFLICT`。

### 8.2 状态机

```text
created -> validating -> queued -> running -> verifying -> completed
        -> awaiting_input
        -> failed
        -> cancelled
```

持久化：

- 请求快照或不可变请求路径；
- package hash；
- normalized timeline 工件；
- runtime、pid/worker lease、attempt；
- progress 和当前 scene；
- stdout/stderr 日志引用；
- 临时输出、最终输出、ffprobe 报告；
- 错误码与可诊断上下文。

### 8.3 重启恢复

- `queued`：可重新入队。
- 尚未启动 renderer 的 `running`：lease 过期后可安全重新入队。
- 已启动 renderer 但进程状态未知：先核对 pid、临时输出和 job lease，不得盲目双开。
- `verifying`：若成品存在，继续 ffprobe/QA，不应重新渲染。
- `completed`：重复查询只返回冻结结果。
- 临时文件按 `tenant/project/job` 隔离；清理只能针对已解析且位于指定根目录的路径。

Windows 锁必须使用跨平台方案或 `msvcrt`/原子数据库 lease；禁止 import `fcntl` 后在 Windows 继续调用。

## 9. 输出与验证

完成渲染不等于进程退出码为 0。进入 `completed` 前必须验证：

- 输出文件存在且非零；
- ffprobe 可解析容器；
- 宽高、fps、codec 符合请求；
- 总时长与 normalized timeline 相差不超过 1 帧；
- 每个场景边界抽帧可解码；
- 音轨存在性符合策略；
- 字幕或烧录字幕符合请求；
- 结果资产记录包含路径、大小、SHA-256、时长和 ffprobe 摘要。

逐场景 QA 至少检查边界前后帧，防止静态图片时长正确但切换点错位。

## 10. 推荐修改面

具体文件名可随现有架构调整，但职责应覆盖：

1. `schemas/`：新增外部 remix render request/job/result schema；扩展 artifact schema 时保持版本兼容。
2. `lib/`：素材解析、normalized timeline、帧舍入、job registry、lease/recovery。
3. `tools/video/video_compose.py`：只消费 normalized contract，支持 image/video/original_video 混合。
4. `remotion-composer/src/`：新增或扩展正式 mixed timeline composition。
5. MCP/API 层：submit/status/cancel，显式参数，不读取最近上传资产。
6. `tests/`：schema、时间轴、Windows 路径、Remotion/FFmpeg 集成和恢复测试。
7. `skills/pipelines/video-template-remix/`：代码契约变化后同步 stage director 文档，避免 LLM 继续生成旧格式。

## 11. 测试矩阵

### 单元测试

- 30fps 下小数秒到帧的确定性转换。
- 三场景累计时长无漂移。
- overlap、gap、0 时长、负值、越界 source range。
- asset 类型不匹配、缺文件、路径穿越、中文文件名。
- pending replacement/generation 拒绝渲染。
- 幂等键重复与 hash 冲突。

### 集成测试

- 原视频 + PNG + 原视频混合。
- JPG/PNG/WebP、横图/竖图/透明图。
- 用户视频替换且静音。
- 保留原视频音轨 + TTS + 字幕 + 音乐 ducking。
- 无音轨原视频。
- Remotion 第一次 bundle/Chrome 初始化。
- 输出目录和素材目录包含中文、空格。
- renderer 进程中断后重启恢复。
- `verifying` 阶段重启不重复渲染。

### 端到端验收样例

固定一套小素材，保存 golden request 和 ffprobe 期望：

```text
scene_001 original_video 0.0-3.2
scene_002 image          3.2-6.7
scene_003 original_video 6.7-10.2（源范围 10.5-14.0）
```

30fps 预期总帧数为 306。输出必须可播放，三个边界误差均不超过 1 帧。

## 12. 验收标准

- 正式 request schema 可验证且无隐式会话资产。
- 同一个请求重放得到同一个 `om_job_id` 和同一冻结结果。
- 混合 timeline 可在目标 Windows 10 机器上渲染成功。
- 图片展示时长严格来自各 scene，不使用统一 `duration_per_image` 覆盖。
- 原视频片段严格来自 `source_range`。
- renderer 不可用时明确 blocked，不静默换 runtime。
- worker 被杀死后任务可恢复，且不会产生两个并行 render。
- 所有失败能定位到 job、scene、asset 和字段。
- 输出通过 ffprobe，逐镜头和总时长误差不超过 1 帧。

## 13. 非目标与禁止项

- 不在本任务中实现 VClaw 到 OpenClaw 的网络/CLI 调度；见交接文档 01。
- 不通过目录枚举、最近上传文件、最近生成图片或全局 current project 选择素材。
- 不把 `pending_replacement` 当成可渲染图片。
- 不用单一 `duration_per_image` 代替逐 scene 时长。
- 不通过静默 runtime fallback 掩盖 Remotion 配置问题。
- 不修改已经冻结的素材包版本；所有派生工件另存并记录 hash。

## 14. 交付清单

接手 LLM 完成后必须给出：

- 输入/输出 schema 与兼容策略；
- normalized timeline 示例和帧舍入说明；
- 修改文件列表；
- Windows 10 上的真实测试命令与结果；
- 端到端样例的 OM job ID、输出 MP4、ffprobe 报告和各 scene 边界核验；
- 幂等与进程重启恢复证据；
- 仍存在的 beta 风险，不得把单一 happy path 描述成生产可用。

