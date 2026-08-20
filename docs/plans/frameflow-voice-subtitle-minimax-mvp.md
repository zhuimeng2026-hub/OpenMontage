# FrameFlow 字幕、声音克隆与 MiniMax MVP 落地方案

状态：MVP 已实现，生产增强项待办
记录日期：2026-08-20

## 0. 本次落地结果

本次代码已按“OpenMontage 统一调度、Voicebox 作为内部执行服务”的边界完成 MVP：

- 通用上传会话可登记并隔离 `image`、`video`、`audio` 素材，业务接口只接收会话内 `asset_id`；
- 新增 `create_captioned_video_share` 与 `create_cloned_voice_video_share` 两个异步 MCP 工具；
- 新增 Voicebox 内部鉴权客户端及声音克隆、TTS 工具适配器；
- 新增替换视频音轨、字幕烧录、微云上传与分享链路；
- 新增媒体任务持久化、状态查询、SSE 进度/断线快照与重启后失败态恢复；
- FrameFlow BFF 增加生产工具白名单、任务入队识别和字幕/克隆配音页面入口。

本轮未把 Voicebox 锁死为某个供应商 SDK。Voicebox 可在内部接 MiniMax Official，OpenMontage 通过稳定的 `/api/v1/voices/clone` 与 `/api/v1/tts` 合约调用，未来替换供应商不改变公网 MCP 和 FrameFlow 接口。

以下属于上线前生产增强，不应视为本轮已完成：供应商异步任务轮询/重试、收费操作幂等键、`voice_profiles` 独立持久化与删除周期、对象存储预签名数据面、真实 Voicebox 与微云环境的端到端冒烟。

## 1. 当前基线

FrameFlow 第一阶段业务闭环已经实现：

- 图片及素材分块上传；
- MCP 会话亲和；
- 图片批次和异步渲染；
- 微信登录、用户隔离、限流和分级配额；
- SQLite 任务持久化、并发租约及重启恢复；
- Remotion 合成、渲染进度查询、微云上传及分享链接返回。

本方案不重做第一阶段。MVP 的目标是补齐两个用户明确需要的业务能力：

1. 视频自动加字幕；
2. 用户声音克隆、配音，并可同时生成字幕。

## 2. MVP 对外工具

### 2.1 `create_captioned_video_share`

建议输入：

```json
{
  "project_id": "project-id",
  "video_asset_id": "asset-id",
  "language": "zh",
  "subtitle_style": "short_video",
  "title": "作品标题"
}
```

处理链路：

```text
上传视频
-> transcriber 提取词级时间戳
-> subtitle_gen 生成字幕数据
-> remotion_caption_burn/FFmpeg 烧录字幕
-> 微云上传
-> 创建分享链接
```

### 2.2 `create_cloned_voice_video_share`

建议输入：

```json
{
  "project_id": "project-id",
  "video_asset_id": "asset-id",
  "voice_sample_asset_id": "voice-asset-id",
  "script": "需要朗读的文案",
  "audio_mode": "replace",
  "subtitle": true,
  "title": "作品标题",
  "voice_consent": true
}
```

处理链路：

```text
上传视频、声音样本和文案
-> MiniMax 上传复刻音频
-> 创建克隆 voice_id
-> 使用克隆音色合成语音并请求词级字幕时间戳
-> 替换或混合视频音轨
-> 烧录字幕
-> 微云上传
-> 创建分享链接
```

两个工具都必须异步提交，立即返回 `job_id`，通过现有状态查询/SSE 机制报告进度，不能让 MCP HTTP 请求等待完整渲染。

## 3. 需要新增或调整的代码

### 3.1 通用素材模型

扩展当前偏图片批次的上传登记，支持：

- `image`
- `video`
- `audio`
- `voice_sample`
- `subtitle`

上传完成后返回稳定的 `asset_id`，业务工具只接收 `asset_id`，不接收客户端任意本地路径。

### 3.2 MiniMax 官方适配器

保留当前经 fal.ai 调用的 `minimax_video.py`，将其供应商身份明确为 `minimax_fal`。新增：

- `tools/video/minimax_official_video.py`
  - 使用 `MINIMAX_API_KEY`；
  - 支持 Hailuo 2.3 T2V/I2V；
  - 创建任务、查询状态、取得 `file_id`、下载结果；
- `tools/audio/minimax_voice_clone.py`
  - 上传复刻音频；
  - 创建、查询和删除克隆音色；
- `tools/audio/minimax_tts.py`
  - 使用克隆 `voice_id` 合成；
  - 支持 `speech-2.8-hd/turbo`；
  - 请求句级或词级字幕时间戳。

新增工具按 `BaseTool` 合约注册。selector 根据 capability 自动发现，不在 selector 中硬编码供应商。

### 3.3 持久化模型

建议新增：

```text
media_assets
media_jobs
voice_profiles
```

`media_jobs` 至少保存：

```text
user_id, project_id, job_id, job_type, status, current_stage,
progress, result_url, error_code, error_message, created_at, updated_at
```

`voice_profiles` 至少保存：

```text
user_id, provider, provider_voice_id, sample_sha256,
consent_at, expires_at, created_at
```

声音样本必须记录授权确认、样本哈希、所属用户和删除时间；不同用户不得引用同一个私有音色。

### 3.4 FrameFlow 接入

- 新增字幕和克隆配音提交接口；
- 新增视频/声音样本上传控件；
- 复用现有任务队列和 SSE 展示，补充新阶段状态；
- BFF 对生产 MCP 工具设置 allowlist；
- 为视频时长、音频时长、文件大小、MIME 和每日任务数设置配额；
- 增加幂等键，避免客户端重试导致重复计费；
- 对供应商限流、超时和失败实现有限次数重试；
- 任务失败时保存供应商 trace/task id，但不把 API Key 或原始敏感响应写入日志。

## 4. 供应商补足顺序

### TTS/声音

1. MiniMax Official：MVP 主路径，覆盖中文克隆和 TTS；
2. Edge TTS：免费、非克隆兜底；
3. ElevenLabs：多语言和高质量克隆备选；
4. 豆包或 DashScope：国内普通 TTS 备选；
5. Piper：只有明确需要完全离线时再部署。

不以配置满全部 TTS 工具为目标。

### AI 视频

1. MiniMax Official Hailuo 2.3：MVP 主路径；
2. Kling Official：第二条 I2V/T2V 路径；
3. Seedance、Veo、Runway：高价值或复杂需求再补；
4. HeyGen/Higgsfield：数字人、人物广告场景再补；
5. ComfyUI/Wan/Hunyuan/CogVideo/LTX：有本地 GPU 和运维需求后再补。

## 5. 生产网络拓扑决策

推荐采用统一公网入口，并由 OpenMontage 统一调度；新增的 Voicebox 只作为内部能力执行服务，不单独公开给客户端：

```text
浏览器
  -> HTTPS FrameFlow 域名
  -> FrameFlow BFF
  -> VPS 内网 MCP Proxy

Claw/OpenClaw/其他 MCP Client
  -> HTTPS MCP 域名 /mcp
  -> VPS MCP Proxy

VPS MCP Proxy
  -> WireGuard/受限 IPv6 链路
  -> 防火墙内 OpenMontage :8900/mcp

OpenMontage 调度器
  -> 本机/LAN 内部 API 或任务队列
  -> Voicebox 执行声音克隆、TTS 等音频任务
  -> OpenMontage 继续完成字幕、合成、发布和统一任务状态
```

端口建议：

```text
公网 Nginx/Caddy          :443
OpenMontage MCP Proxy    127.0.0.1:8080
FrameFlow BFF            127.0.0.1:8081
防火墙内 OpenMontage      :8900
Voicebox                  仅本机或内网监听，不设公网端口
```

如果 BFF 和 MCP Proxy 不在同一 VPS，可继续分别使用 8080；如果在同一台 VPS，必须使用不同监听端口。

生产侧原则：

- 外部客户端只看到 HTTPS 443，不直接暴露 8080、8900 或 Voicebox；
- FrameFlow BFF 的 `MCP_BASE_URL` 指向 VPS 本机 MCP Proxy，而不是绕公网域名回环；
- Proxy 保存上游 8900 的凭证并替换客户端凭证；
- 8900 最好通过 WireGuard 访问；暂时继续使用公网 IPv6 时，只允许 VPS 源地址、启用 TLS 和高强度独立令牌；
- OpenMontage 持有用户、配额、任务、素材、工作流和发布状态；Voicebox 只执行被调度的音频能力并返回制品或 `asset_id`；
- 代理不改变模型、提示词或渲染质量；
- MVP 小文件继续走分块上传。文件明显增大后，将媒体数据面改为对象存储预签名上传，MCP 只传 `asset_id` 和任务控制消息。

### 5.1 IPv6 资源受限时的演进

IPv6 地址、入口或带宽受限时，不改变“OpenMontage 是唯一调度者”的逻辑边界，只调整任务分发方式：

1. 少量节点时，OpenMontage 按 Voicebox 的能力、健康状态和 `max_concurrency` 选择执行节点；
2. 多节点时增加 worker 注册表，记录 `worker_id`、能力标签、当前负载、IPv6/出口配置和最后心跳；
3. 同一任务在中间文件仍位于本地时保持 worker affinity；改用对象存储后可弱化节点亲和；
4. 如果没有足够的可入站 IPv6，改为 Voicebox 主动连接 VPS 的 pull-worker 模式：Voicebox 通过出站 HTTPS/WebSocket 拉取任务、续租并回传结果，无需为每个 worker 暴露公网端口；
5. IPv6 带宽或并发限制进入调度配额，作为资源令牌参与排队，不能靠客户端选择或直连 Voicebox 绕过。

### 5.2 IPv6 资源充足时的演进

即使 OpenMontage 和每个 Voicebox 节点都有独立、稳定、可公网访问的 IPv6，也保留 OpenMontage 作为唯一控制面和任务所有者。IPv6 可达性用于优化数据面和执行资源分配，不让普通客户端自行拆分工作流。

推荐采用混合模式：

```text
控制面：客户端 -> OpenMontage -> 创建统一 job、鉴权、配额、调度、状态、发布
数据面：客户端 -> 一次性签名地址 -> 被选中的 Voicebox/对象存储上传大素材
执行面：OpenMontage -> Voicebox worker -> 返回 asset_id/时间戳/执行结果
```

资源池按能力拆分：

- `video_render`：Remotion/FFmpeg 最终渲染；
- `voice_clone`：声音样本处理和音色复刻；
- `tts`：语音合成；
- `subtitle`：转写、时间戳和字幕生成；
- `publish`：成品上传与分享；
- `network_egress`：供应商 API 和大文件传输带宽。

调度按 CPU、GPU、内存、磁盘、带宽、供应商并发额度和缓存亲和性选择节点，而不是只按 IPv6 地址轮询。普通业务调用仍只访问 OpenMontage；只有经 OpenMontage 授权的一次性素材上传或下载可以直达 Voicebox。这样既减少大文件经 VPS 中转，又不拆散用户、任务、计费和状态管理。

## 6. 验收标准

- 视频上传后能异步返回带字幕的视频微云链接；
- 声音样本上传后能创建用户隔离的克隆音色；
- 输入文案后能生成克隆配音、同步字幕并返回最终分享链接；
- BFF/Proxy 重启后仍可查询任务；
- 重试不会重复创建收费任务；
- 不同用户不能查看或使用彼此的视频、声音样本、voice_id 和任务结果；
- 公网无法直接访问内网 8900 或 Voicebox；
- 供应商失败时返回明确阶段和可追踪 task/trace id。
