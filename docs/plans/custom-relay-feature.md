# Plan: Kling/Seedance 自定义中转站 (Custom Relay) 功能

**Date:** 2026-08-03
**Status:** Ready for review
**Goal:** 为 Kling 和 Seedance 视频生成增加自定义中转站（第三方 relay API）支持，作为 fal.ai / Replicate / 官方 API 之外的第四条路径。

---

## 1. 目标概述

当前 Kling 和 Seedance 各有两个后端：

| 模型 | 工具 | 后端 | 环境变量 |
|------|------|------|----------|
| Kling | `kling_video` | fal.ai | `FAL_KEY` |
| Kling | `kling_official_video` | Kling 官方 API | `KLING_API_KEY` + `KLING_API_BASE_URL` |
| Seedance | `seedance_video` | fal.ai | `FAL_KEY` |
| Seedance | `seedance_replicate` | Replicate | `REPLICATE_API_TOKEN` |

用户需要**第五条/第六条路径**：自定义中转站（第三方 relay API）——用户自配 endpoint + API key，relay 服务代理转发到 Kling/Seedance 后端。

这在中文 AI 生态中非常常见（one-api、new-api、各类聚合 API 网关）。

---

## 2. 设计决策

### 2.1 协议：new-api OpenAI-compatible 视频 API

**确认结论：** 本机 `/opt/new-api` 已内置 Kling 和 Seedance(豆包) 中转通道（`relay/channel/task/kling/`、`relay/channel/task/doubao/`），并在端口 3000/13000 运行。OpenMontage relay 工具直接对接 new-api 的 **OpenAI 兼容视频 API**：

```
# 提交任务
POST {VIDEO_RELAY_BASE_URL}/v1/video/generations
Authorization: Bearer {VIDEO_RELAY_API_KEY}
{
  "model": "kling-v2-master",          # 或 "seedance-2-0" / "seedance-2-0-fast"
  "prompt": "...",
  "image": "https://...",              # 可选，图生视频
  "duration": 5.0,
  "metadata": { ... }                  # 厂商参数透传（aspect_ratio, mode, resolution, generate_audio...）
}
→ 返回 {"task_id": "...", "status": "queued"}

# 轮询
GET {VIDEO_RELAY_BASE_URL}/v1/video/generations/{task_id}
→ {
    "task_id": "...",
    "status": "queued" | "processing" | "succeeded" | "failed",
    "url": "https://...",              # succeeded 时返回视频地址
    "format": "mp4",
    "metadata": {...},
    "error": {...}
  }

# 下载 url → 本地 mp4
```

**理由**：
- new-api 已内置 Kling/Seedance 通道，无需在 OpenMontage 侧重新实现中转逻辑
- new-api 提供统一 token 认证、计费、渠道轮询、失败重试
- 协议是标准 OpenAI 风格，也被其他聚合网关（one-api 等）兼容
- 用户已在用 new-api（本机运行中），对接成本最低

### 2.2 配置：统一 relay 端点 + 统一 API key

使用**一套**环境变量同时服务 Kling relay 和 Seedance relay：

```bash
VIDEO_RELAY_BASE_URL=http://127.0.0.1:3000    # new-api 服务端点
VIDEO_RELAY_API_KEY=sk-xxxxxxxx                # new-api 的访问 token（OpenAI 兼容）
```

**理由**：
- 典型的中转站（new-api/one-api）一个 token 一组 endpoint 覆盖所有模型
- 减少配置复杂度（2 个变量，而非 4 个）
- 模型选择由工具内部的 model map 决定（`kling-v2-master` vs `seedance-2-0`）
- new-api 已在本机运行，`VIDEO_RELAY_BASE_URL` 默认填 `http://127.0.0.1:3000`

### 2.3 工具架构：共享 relay 模块 + 两个独立工具

```
tools/video/_relay.py          ← 新增：共享 relay 协议实现（submit/poll/download）
tools/video/kling_relay.py     ← 新增：Kling relay 工具（继承 BaseTool，调用 _relay）
tools/video/seedance_relay.py  ← 新增：Seedance relay 工具（继承 BaseTool，调用 _relay）
```

**理由**：
- 两个工具分别有独立的 provider 字符串（`kling_relay`、`seedance_relay`），便于 `video_selector` 独立评分和选择
- 各自的 input_schema 可独立定义（Kling 和 Seedance 参数有差异）
- 共享的 `_relay.py` 避免 submit/poll/download 逻辑重复
- 匹配现有模式：`_shared.py` 为本地视频生成提供共享辅助函数

### 2.4 Provider 字符串

| 工具 | `name` | `provider` | registry 中的身份 |
|------|--------|-----------|-------------------|
| `kling_relay` | `kling_relay` | `kling_relay` | 独立 provider，仅与 `kling` 共享 `fallback_tools` 链 |
| `seedance_relay` | `seedance_relay` | `seedance_relay` | 独立 provider，仅与 `seedance` 共享 `fallback_tools` 链 |

**理由**：独立的 provider 字符串让评分引擎可以分别评价 relay 路径和 fal.ai 路径。relay 可能更便宜/更快，但也可能更不稳定。

---

## 3. 新增文件

### 3.1 `tools/video/_relay.py` — 共享 relay 协议实现

**职责**：封装 new-api OpenAI-compatible 的 submit → poll → download 流程，供 `kling_relay.py` 和 `seedance_relay.py` 调用。

**核心函数**：

```python
class RelayError(Exception):
    """Raised when the relay endpoint fails (network, API, timeout, or task failure)."""

def generate_via_relay(
    *,
    base_url: str,
    api_key: str,
    model: str,                       # e.g. "kling-v2-master" / "seedance-2-0"
    prompt: str,
    operation: str = "text_to_video", # text_to_video | image_to_video
    image_url: str | None = None,     # 图生视频
    duration: float | None = None,    # 秒
    metadata: dict | None = None,     # 厂商参数透传 (aspect_ratio, mode, resolution, generate_audio...)
    output_path: str | Path,
    poll_interval: float = 5.0,
    poll_timeout: float = 900.0,
) -> dict:
    """Generate a video through a new-api compatible relay endpoint.

    Returns metadata dict for ToolResult.data:
      {
        "gateway": "new-api",
        "task_id": str,
        "model": model,
        "remote_url": str,
        "output": str,
        "output_path": str,
        "format": str,
        **probe_output(output_path),
      }
    Raises RelayError on failure.
    """
```

**实现细节**：
- POST `{base_url}/v1/video/generations`（`Authorization: Bearer {api_key}`）→ 解析 `task_id`
- 轮询 GET `{base_url}/v1/video/generations/{task_id}`，检查 `status`
- `succeeded` → 用响应里的 `url` 下载视频到 `output_path`
- `failed` → 抛 `RelayError`（含 error 消息）
- 超时/网络错误 → 抛 `RelayError`
- 下载后用 `probe_output()` 返回视频元数据

**本地上传**：relay 路径要求 `image_url` 已是公网 URL。本地图由调用方先上传（可复用 fal.ai 上传或中转站自有上传机制）。

### 3.2 `tools/video/kling_relay.py` — Kling Relay 工具

**职责**：将 relay 协议包装为标准的 `BaseTool`，提供 Kling 特定的参数和默认值。

**关键属性**：

```python
class KlingRelay(BaseTool):
    name = "kling_relay"
    provider = "kling_relay"
    capability = "video_generation"
    runtime = ToolRuntime.API
    stability = ToolStability.EXPERIMENTAL

    dependencies = ["env:VIDEO_RELAY_BASE_URL", "env:VIDEO_RELAY_API_KEY"]
    install_instructions = (
        "Set VIDEO_RELAY_BASE_URL and VIDEO_RELAY_API_KEY in .env.\n"
        "VIDEO_RELAY_BASE_URL — new-api / 中转站端点 (e.g., http://127.0.0.1:3000)\n"
        "VIDEO_RELAY_API_KEY — 中转站访问 token"
    )
    agent_skills = ["ai-video-gen"]  # 与 kling_video 一致

    # OpenMontage model_variant → new-api 模型名
    MODEL_MAP = {
        "v2.1/master": "kling-v2-master",
        "v2.1/pro": "kling-v1-6",
        "v2.1/standard": "kling-v1",
        "v3/standard": "kling-v1-6",
    }
    DEFAULT_MODEL = "kling-v2-master"

    fallback_tools = ["kling_video", "kling_official_video",
                      "seedance_video", "seedance_relay",
                      "veo_video", "minimax_video"]
```

**input_schema**：与 `kling_video.py` **一致**（operation, model_variant, duration, aspect_ratio, image_url, output_path），加上：
- `model_name` — 直接覆盖 new-api 模型名（跳过 MODEL_MAP）
- `negative_prompt` / `mode` / `cfg_scale` — 透传进 metadata

### 3.3 `tools/video/seedance_relay.py` — Seedance Relay 工具

**职责**：与 `kling_relay.py` 对称，Seedance 特定的参数和默认值。

**关键属性**：

```python
class SeedanceRelay(BaseTool):
    name = "seedance_relay"
    provider = "seedance_relay"
    capability = "video_generation"
    runtime = ToolRuntime.API
    stability = ToolStability.EXPERIMENTAL

    dependencies = ["env:VIDEO_RELAY_BASE_URL", "env:VIDEO_RELAY_API_KEY"]
    # install_instructions 同上

    agent_skills = ["seedance-2-0", "ai-video-gen"]  # 与 seedance_video 一致

    # OpenMontage model_variant → new-api 豆包模型名
    MODEL_MAP = {
        "standard": "seedance-2-0",
        "fast": "seedance-2-0-fast",
    }
    DEFAULT_MODEL = "seedance-2-0"

    fallback_tools = ["seedance_video", "seedance_replicate",
                      "kling_video", "kling_relay",
                      "veo_video", "minimax_video"]
```

**input_schema**：与 `seedance_video.py` **一致**（operation, model_variant, duration, aspect_ratio, resolution, generate_audio, image_url, seed, output_path），去掉需要 `upload_image_fal` 的本地路径字段，加上：
- `model_name` — 直接覆盖 new-api 模型名
- 参数经 metadata 透传给 new-api（`aspect_ratio`, `resolution`, `generate_audio`）

---

## 4. 修改文件

### 4.1 `.env.example` — 新增 relay 环境变量

在 `# --- Kling official direct API ---` 区块之后新增：

```bash
# --- Custom relay / 中转站 ---
VIDEO_RELAY_BASE_URL=         # 自定义中转站端点，fal.ai 兼容协议
                              # 例: https://api.your-relay.com
VIDEO_RELAY_API_KEY=          # 中转站 API key
```

### 4.2 无需修改的文件

- **`tools/tool_registry.py`** — 无需修改。registry 自动发现 `tools/video/` 下所有 `BaseTool` 子类。
- **`tools/video/video_selector.py`** — 无需修改。selector 自动发现所有 `capability="video_generation"` 的工具。
- **`lib/scoring.py`** — 无需修改。但如果想给 relay 路径一个合理的默认 quality_score，可以在工具类上设置 `quality_score = 0.80`（低于直接 fal.ai 的 0.95，高于本地模型）。
- **`tools/cost_tracker.py`** — 无需修改。

---

## 5. 配置说明（给用户的）

用户在 `.env` 中添加两行即可启用：

```bash
VIDEO_RELAY_BASE_URL=https://api.their-relay.com
VIDEO_RELAY_API_KEY=sk-xxxxxxxx
```

设置后，`kling_relay` 和 `seedance_relay` 两个工具会在 registry 中自动变为 `AVAILABLE`，并出现在 `video_selector` 的候选中。

中转站 API 必须支持以下行为：
1. 接受 `POST {BASE_URL}/{model_path}` 格式的请求（model_path 如 `kling-video/v3/standard/text-to-video` 或 `bytedance/seedance-2.0/text-to-video`）
2. 返回 `{"status_url": "...", "response_url": "..."}` 用于异步轮询
3. 轮询 `status_url` 返回 `{"status": "COMPLETED"}` 表示完成
4. `response_url` 返回 `{"video": {"url": "https://..."}}`

这本质上是 fal.ai queue API 的协议。大多数中转站已兼容。

---

## 6. 实现步骤

以下步骤编号表示实现顺序，每个步骤应作为一个独立 commit：

### Step 1: 创建 `tools/video/_relay.py`

实现共享的 `generate_via_relay()` 函数：
- 参数校验（base_url 格式、api_key 非空）
- POST submit → 解析 status_url / response_url
- 轮询循环（可配置间隔、超时）
- 结果下载
- 错误处理（网络错误、API 错误、超时）
- 定义 `RelayError` 异常类
- 使用 `probe_output()` 返回视频元数据

### Step 2: 创建 `tools/video/kling_relay.py`

- 继承 `BaseTool`
- 定义所有元数据字段（name, provider, tier, capability, ...）
- 定义 input_schema（与 `kling_video.py` 对齐 + `model_path_override`）
- `get_status()` → 检查 `VIDEO_RELAY_BASE_URL` + `VIDEO_RELAY_API_KEY`
- `estimate_cost()` → 保守估算（relay 价格未知，使用与 fal.ai 相同或略低的估算）
- `execute()` → 调用 `generate_via_relay()` + 组装 ToolResult

### Step 3: 创建 `tools/video/seedance_relay.py`

- 与 Step 2 对称
- input_schema 与 `seedance_video.py` 对齐（去掉需要 `upload_image_fal` 的字段）
- `estimate_cost()` 对齐 `seedance_video.py`

### Step 4: 更新 `.env.example`

- 新增 `VIDEO_RELAY_BASE_URL` 和 `VIDEO_RELAY_API_KEY` 注释块

### Step 5: 添加单元测试

**新文件：`tests/tools/test_kling_relay.py`**
- `get_status()` 在无 env var 时返回 UNAVAILABLE
- `get_status()` 在 env var 已设时返回 AVAILABLE
- `check_dependencies()` 正确检测缺失的 env var
- `execute()` 使用 mock HTTP 响应的集成测试
- 空 prompt 被拒绝
- 网络错误正确处理

**新文件：`tests/tools/test_seedance_relay.py`**
- 同上对称

**新文件：`tests/tools/test_relay_shared.py`**
- `generate_via_relay()` 的正常流程
- 轮询超时
- API 返回 FAILED 状态
- 无效 URL 格式
- response 缺失 video.url

### Step 6: 验证集成

```bash
# 设置测试环境变量后
python -c "
from tools.tool_registry import registry
registry.discover()
print(registry.provider_menu_summary())
"
# 应看到 kling_relay 和 seedance_relay 出现在 video_generation 下
```

---

## 7. 测试策略

| 层级 | 内容 | 工具 |
|------|------|------|
| **单元测试** | `_relay.py` 的 submit/poll/download 逻辑（mock HTTP） | pytest + responses / unittest.mock |
| **工具合约测试** | `kling_relay` / `seedance_relay` 的 status/dependency/execute 契约 | pytest |
| **Registry 集成** | 确认 relay 工具被自动发现并可被 selector 选中 | pytest |
| **手动端到端** | 配置真实 relay endpoint 后跑一次完整生成 | 手动 |

---

## 8. 与其他系统的交互

### 8.1 video_selector 选择逻辑

`video_selector` 自动发现所有 `capability="video_generation"` 工具并评分排序。`kling_relay` 和 `seedance_relay` 会**自动**出现在候选中，无需修改 selector。

如果 relay 服务更快/更便宜，可以通过设置 `quality_score`、`historical_success_rate`、`latency_p50_seconds` 来影响评分。

### 8.2 回退链 (Fallback Chain)

```
kling_relay → kling_video (fal.ai) → kling_official_video → seedance_video → ...
seedance_relay → seedance_video (fal.ai) → seedance_replicate → kling_video → ...
```

如果 relay 挂了，自动回退到 fal.ai 直连。

### 8.3 成本追踪

`cost_tracker.py` 无需修改。Relay 工具通过 `estimate_cost()` 返回估算值，具体价格由 relay 服务商定义。未来可在工具类上加 `price_per_second` 等字段让用户配置。

### 8.4 Backlot Board

`BaseTool.__init_subclass__` 自动为 `execute()` 注入 Backlot 事件。无需额外代码——relay 工具的执行会自动出现在项目 activity ticker 中。

---

## 9. 设计限制 & 后续扩展

| 限制 | 理由 | 后续可能扩展 |
|------|------|-------------|
| 仅支持 fal.ai 兼容协议 | 覆盖面最广，实现最简单 | 支持 OpenAI-compatible 协议作为第二协议 |
| 不支持本地上传（relay 需要 URL） | relay 服务通常有自己的上传机制 | 如果 relay 提供 upload endpoint，加入 `upload_image_relay()` |
| 统一 endpoint + 统一 key | 减少配置复杂度 | 支持 per-provider relay endpoint（`KLING_RELAY_BASE_URL` 等） |
| 无 relay 服务发现/健康检查 | relay 是用户自配的，不需要注册中心 | 增加 relay ping/heartbeat |
| `model_path_override` 需手动填 | 不同 relay 可能用不同的 model path 映射 | 自动 model path 映射表 |

---

## 10. 文件清单

```
新增:
  tools/video/_relay.py              # 共享 relay 协议实现
  tools/video/kling_relay.py         # Kling relay 工具
  tools/video/seedance_relay.py      # Seedance relay 工具
  tests/tools/test_relay_shared.py   # 共享 relay 协议单元测试
  tests/tools/test_kling_relay.py    # Kling relay 工具测试
  tests/tools/test_seedance_relay.py # Seedance relay 工具测试

修改:
  .env.example                       # 新增 VIDEO_RELAY_BASE_URL / VIDEO_RELAY_API_KEY

无需修改:
  tools/tool_registry.py             # 自动发现
  tools/video/video_selector.py      # 自动发现
  lib/scoring.py                     # 可选设置 quality_score
  tools/video/_shared.py             # 不依赖 fal.ai upload
```
