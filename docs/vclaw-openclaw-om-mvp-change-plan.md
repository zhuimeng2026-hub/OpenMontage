# VClaw / OpenClaw / OpenMontage 单机 MVP 改造计划

> 状态：实现前约束与验收清单（2026-09-04）  
> 范围：VClaw 端接入、OpenClaw 运行时复用判断、OpenMontage 侧后续必须修改/验证的事项。  
> 本文只记录方案，不代表本次已经修改 OpenMontage 代码。

## 1. 已确认的单机调用关系

MVP 可以在同一台 Windows 10 机器上运行，但仍保持独立进程和清晰职责：

```text
VClaw GUI -> VClaw Go 控制面/Worker -> OpenClaw Runtime -> OpenMontage MCP/Worker
                                                   ^                 |
                                                   +--- 状态/结果 ---+
```

- GUI 只调用 VClaw；不直接调用 OM MCP，也不把大文件转发给 OpenClaw。
- VClaw 是用户、项目、资产登记、素材包版本、任务状态和额度的事实源。
- OpenClaw 负责 LLM 决策、阶段编排和把业务素材包编译成 OM 可执行工件。
- OM 负责参考视频分析、媒体处理、TTS/字幕消费、混合时间轴合成和最终渲染。
- 大文件只在本机共享素材根目录（或将来对象存储）保存一次；跨层只传 `asset_id`、`file_key`、JSON 工件引用和状态。
- MVP 的分析和渲染并发均先固定为 1，避免 Windows 单机内存竞争。

### 1.1 两种不同的消息

VClaw -> OpenClaw 是业务任务，例如：

```json
{
  "job_id": "job_001",
  "project_id": "project_001",
  "package_id": "package_001",
  "package_version": 3,
  "task": "analyze_reference",
  "reference_asset_id": "asset_ref_01",
  "user_asset_ids": ["asset_product_01"]
}
```

OpenClaw -> OM 是技术操作，例如：

```json
{
  "operation": "analyze_reference",
  "project_id": "project_001",
  "reference_asset": {"asset_id": "asset_ref_01", "file_key": "assets/reference/ref.mp4"},
  "requested_outputs": ["media_info", "transcript", "scene_map", "keyframes"]
}
```

OM 返回结构化结果和文件引用，不返回大文件二进制。渲染阶段 OpenClaw 传 `brief`、`script`、`scene_plan`、`asset_manifest`、`edit_decisions`，OM 按版本快照执行。

## 2. `C:\u-king\m-claw` 复用结论（只读审计）

### 2.1 可以复用的部件

| 部件 | 位置/证据 | 建议 |
|---|---|---|
| 已打包的 Windows Node + OpenClaw 运行时 | `offline/dist/payload-package/{runtime-node.tar,openclaw.tar}`；`manifest.json` 记录 SHA-256 和依赖顺序 | 可作为 VClaw 缺失运行时时的本地安装/供给来源；使用前锁定版本、校验哈希并确认与 VClaw 方案兼容 |
| Windows wrapper 的环境初始化模式 | `offline/payload/shell/bin/_env.cmd`、`bin/openclaw.cmd` | 可复用“相对根目录 + 独立 state/home/temp + 清晰 exit code”的模式；VClaw 应改成自己的 `VCLAW_*`/数据根变量，不能直接依赖 M-Claw 路径名 |
| 离线安装/校验流程 | `offline/scripts/install-m-claw-to-usb.ps1`、`manifest.json` | 可借鉴解包、依赖检查、幂等安装和 SHA-256 校验；不要把 USB 产品布局原样硬编码进 VClaw |
| Windows launcher/downloader 设计 | `launcher/`、`downloader/`、根目录 `README.md` | 若 MVP 需要一键启动，可复用启动前检查、缓存和失败提示思路；当前优先使用 VClaw 已有 `run-clawx.cmd` |
| OpenClaw 多 Agent 安装脚本和 MCP allowlist 思路 | `solutions/obsidian-openmontage/Install-Workflow.ps1`、配置示例 | 可复用 agent workspace、MCP 工具白名单、stage-gated/幂等安装模式；商品视频方案应以 VClaw 自己的 `product-video-production` 为准 |

### 2.2 不应复制的部件

- 不要整目录复制 `m-claw` 的 ClawX、installer、USB payload 或 `portable-data`；它们带有独立产品布局、用户数据和发布假设。
- 不要复制 `solutions/obsidian-openmontage` 的五 Agent 拓扑来替代 VClaw 商品视频拓扑；它是 Obsidian 工作流，职责和工具白名单不同。
- 不要复制 `HOME`、`OPENCLAW_HOME`、`OPENCLAW_STATE_DIR` 的实际值或机器绝对路径；必须由 VClaw 启动器按本机数据根生成。
- 不要复制旧的 ClawX 构建产物、缓存、日志、密钥或 `.env` 值。
- 不要把离线 tar 包当成“已完成集成”的证明；仍需执行 `openclaw --version`、`config validate`、MCP doctor 和 VClaw 端到端探针。

### 2.3 与当前 VClaw 的差距

`C:\vclaw\openclaw` 已有业务方案、ClawX 源/打包框架和 `run-clawx.cmd`，但当前检查显示：

- `runtime/node24` 不存在；
- `runtime/node_modules/openclaw` 不存在；
- `clawx/packages/openclaw/openclaw.mjs` 只是 36 字节占位文件；
- `clawx/packages/clawx` 也未发现。

因此结论是：**m-claw 的运行时包和启动环境模式可以利用，但不能宣称 VClaw 当前已有可运行 OpenClaw。** VClaw 实现应优先选择“使用已校验的 m-claw runtime 包安装到 VClaw 私有目录”或“明确安装同版本 npm runtime”之一，并写入版本探针；不要静默混用两个 runtime。

## 3. OM 侧必须修改或验证的事项

### 3.1 Windows 兼容性：`fcntl`

检查并修复 `C:\OpenMontage_voicebox\lib\workbuddy_session.py` 的 Windows 导入路径。不得只捕获导入错误后继续使用不存在的 `fcntl` API。建议：

- Unix 使用 `fcntl` 锁；Windows 使用等价的 `msvcrt`/原子文件方案，或明确采用跨平台锁库；
- 锁的语义、超时和异常必须一致；
- 增加 Windows Python import smoke test 和并发锁测试。

### 3.2 参考视频分析正式接口

提供稳定的业务/OM 操作（命名可按现有 MCP 规范落地）：

```text
analyze_reference(video_asset_ref, requested_outputs, idempotency_key)
get_analysis_status(analysis_id)
get_analysis_artifact(analysis_id, artifact_name)
```

至少输出媒体信息、镜头边界、带时间戳转写、关键帧引用、音频节奏/静音段。输出必须是可持久化 JSON 和资产引用，不能依赖当前 MCP 会话里“最近上传的图片”或隐式上下文。

### 3.3 静态图与原视频的混合时间轴

OM 渲染器必须消费显式 `scene_plan`/`edit_decisions`，每个镜头声明：

```json
{
  "scene_id": "scene_003",
  "start": 8.2,
  "duration": 3.4,
  "source": {"type": "image", "asset_id": "asset_product_01"},
  "motion": {"type": "ken_burns", "from": "cover", "to": "detail"}
}
```

`source.type` 至少支持 `original_video`、`image`、`video`。保留原视频镜头时必须按明确的原视频时间范围取片段；替换镜头必须使用用户指定资产；禁止按数组顺序或会话最近资产猜测。

### 3.4 逐镜头时长与时间轴校验

- 每镜头必须有显式 `start`/`duration`（或可无损推导的 `end`），单位统一为秒，渲染前转换为帧。
- 校验 `duration > 0`、镜头不重叠、总时长与音频/字幕范围一致，允许的浮点误差需固定。
- Remotion/FFmpeg 适配层必须做字段归一化，避免 `undefined * fps` 产生 `NaN`。
- 对缺失、负值、越界和混用 `in_seconds`/`duration` 等字段返回可诊断错误。

### 3.5 TTS、字幕和音频

- TTS 输出必须是冻结资产引用，带采样率、声道、时长和文本/语言元数据。
- 字幕必须包含明确的 `start`、`end`、文本和语言；渲染器不能从“当前会话文本”推断字幕。
- 验证 TTS 时长、字幕时间范围、背景音乐 ducking/混音和最终音轨同步。
- 失败时返回哪个资产/时间段出错，不能只返回笼统的“render failed”。

### 3.6 Remotion 运行时

必须在目标 Win10 机器上验证 Node、Remotion 项目依赖、浏览器/编码依赖和实际 render 命令。验证内容包括：

1. 最小静态图镜头渲染；
2. 原视频 + 静态图混合渲染；
3. TTS + 字幕渲染；
4. 真实素材路径（含中文路径）和不存在资产的失败诊断；
5. 进程退出码、stderr 尾部和输出 MP4 的 ffprobe 检查。

若 Remotion 未准备好，VClaw 不能把“已提交”标记为可交付完成；应在状态中明确 `degraded`/`blocked`。

### 3.7 禁止会话隐式资产选择

所有 OM 分析和渲染调用都必须接收显式资产 manifest：`asset_id`、`file_key`、媒体类型、校验哈希（可选）和项目/版本。OM 不得使用：

- 最近一次 MCP 上传文件；
- 最近一次生成的图片；
- 目录枚举顺序；
- 全局单例“当前项目”；
- 未传入请求的会话变量。

这也是跨进程重启、并发隔离和审计可重放的前提。

### 3.8 幂等、状态和恢复

- 分析和渲染都接受 `request_id` 与阶段级 `idempotency_key`。
- 相同幂等键重复调用应返回原任务/原工件，不重复生成或扣费。
- 状态至少覆盖 `created`、`queued`、`running`、`awaiting_input`、`completed`、`failed`、`cancelled`。
- VClaw 保存 OpenClaw job ID 与 OM job ID 的映射；轮询必须使用 OM 返回的真实 ID。
- 未知结果不得盲目重试；进程重启后可由项目版本和持久化 job registry 恢复。

## 4. 最小验收矩阵

| 验收 | 通过条件 |
|---|---|
| Windows import | OM Python 模块在 Win10 无 `fcntl` 导入错误，锁测试通过 |
| Runtime | VClaw 能打印 Node/OpenClaw 版本，`openclaw config validate` 通过 |
| 分析 | 给定 `asset_id` 可重复取得相同 scene map/transcript/keyframe 引用 |
| 混合渲染 | 一条原视频镜头 + 一条静态图镜头按各自时长生成可播放 MP4 |
| 音频字幕 | TTS、字幕和视频时间轴对齐，中文路径可用 |
| 资产显式性 | 改变会话最近上传资产不会改变已提交 job 的输出 |
| 幂等恢复 | 重复提交不重复生成；杀掉并重启 worker 后可继续轮询完成 |
| 错误诊断 | 缺失资产、非法时长、Remotion 失败均返回具体 job/scene/asset 信息 |
| 单机资源 | 分析/渲染并发=1，输出目录、临时目录和 state 互不污染 |

## 5. 实施边界

本文件要求 VClaw/OpenClaw 端完成业务编排、runtime 供给和 MCP 接线；OM 端后续按第 3 节完成必要修复与验证。达到目标 MVP 不能简化为“只改 VClaw”：VClaw 是主改造面，但 OM 的 Windows 兼容、正式分析接口、显式混合时间轴和可用 Remotion runtime 是交付前置条件。
