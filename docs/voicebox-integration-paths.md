# Voicebox 接入路径：REST vs MCP 决策参考

> Date: 2026-08-21 | Audience: 接 voicebox 进 OpenMontage 的工程师 / 在 OpenMontage 内写管线或 MCP 包装的 agent。
> 范围: 只讨论 **"如何接入"** —— OpenMontage 这一侧在 REST 与 MCP 之间做哪种形态。
> 与 `docs/openmontage-integration.md`（2026-08-19）的 **"谁调用谁"**（Direction A/B/C）是正交问题。本文档不重复它，请配合阅读。

## TL;DR

| 你的调用方是… | 推荐路径 | 入口代码 |
|---|---|---|
| **写 Python 管线 / OpenMontage 内部 agent**（最常见） | **REST 路径 A** | `tools/audio/voicebox_tts.py` BaseTool |
| 用 MCP JSON-RPC 触发 OpenMontage 工具的 agent | MCP 路径 B1（包装层） | `:8900/mcp` + `voicebox_tts` / `voicebox_clone_voice` / `voicebox_list_cloned_voices` |
| Claude Code / Cursor 这类外部 MCP 客户端直连 | MCP 路径 B2（反向代理） | `:8900/voicebox/mcp/*` → `:17493/mcp/*`（用 voicebox 自家的 5 个 tool 名） |

**默认就选 REST。** MCP 是为了 **让无法直接调 Python 的外部 agent 也能复用 voicebox**，不是一个"更好"或"更新"的版本。下面把三条都讲透，外加失败模式 + 决策表。

---

## 1. 背景 —— 为什么有了"两种"而不是"一种"

Voicebox 同时暴露 **REST** 和 **MCP 在同一端口 :17493**：

```
Voicebox backend (FastAPI)
   ├── REST routes: /health, /profiles, /profiles/{id}/samples,
   │                /generate, /generate/{id}/status (SSE), /audio/{id}
   └── FastMCP mount: /mcp/{speak, transcribe, list_captures,
                                list_profiles, analyze_sample}
```

OpenMontage 这边两条都接了：

```
Python pipeline  ─────────►  voicebox_tts BaseTool (REST :17493)  ─────►  voicebox
Claude Code agent ────────►  :8900/voicebox/mcp/*  (ASGI reverse-proxy) ──► voicebox FastMCP
MCP client tool  ─────────►  :8900/mcp/voicebox_tts (FastMCP wrapper) ──► voicebox_tts BaseTool (REST)
```

两条路径并存不是冗余，是 **入口受众不同**：

- **REST 路径**面向 **Python 调用方**（管线、agent 主循环、storyboard 编排代码）。
- **MCP 路径**面向 **MCP 客户端**（Claude Code、Cursor、任何 [MCP 协议](https://modelcontextprotocol.io/) 兼容 agent runtime）。

如果你写的是 OpenMontage 内部代码 → 选 REST。如果你从 OpenMontage **外**接入一个 MCP-aware agent → 选 MCP。下面分别讲。

---

## 2. 三条接入路径

| 路径 | 接入端 | 鉴权 | 中间环节 | 鉴权注入 | 流式（SSE） | 测试 |
|---|---|---|---|---|---|---|
| **A · REST 直连** | Python BaseTool | `X-Voicebox-Client-Id` header | 无，直接 HTTP | 工具自带常量 `openmontage-tts` | 自己读 SSE / 超时由工具封装 | `tests/integration/test_voicebox_rest.py` |
| **B1 · MCP 包装层** | `:8900/mcp/` JSON-RPC | `Authorization: Bearer $MCP_API_TOKEN` | `mcp_server.py:725-870` 调 BaseTool | Bearer 强制；工具层再写 `X-Voicebox-Client-Id` | **不暴露 SSE**：MCP 只返回 `generation_id`，轮询走 REST | `tests/integration/test_voicebox_mcp_via_openmontage.py` |
| **B2 · MCP 反向代理** | `:8900/voicebox/mcp/*` JSON-RPC | `Authorization: Bearer $MCP_API_TOKEN` | ASGI `_voicebox_proxy_handler` (2502-2683) | Bearer 通过，proxy **剥离** `Authorization` 并强制注入 `X-Voicebox-Client-Id: voicebox-relay` | **保持 SSE 流**：`aiter_raw()` 直接转发 | `tests/integration/test_voicebox_mcp_direct.py` |

**重点：路径 B1 和 B2 是两套不同的 MCP 端点，不是同一个东西的两种调用方式：**

- B1 是 OpenMontage **自己的** FastMCP server 把 voicebox_tts 包成它自己命名的 `voicebox_tts` / `voicebox_clone_voice` / `voicebox_list_cloned_voices` 三个 MCP tool；底层仍然走 REST。
- B2 是 OpenMontage 完全 **透传** voicebox 自家的 FastMCP server，工具名是 voicebox 原生的 `voicebox.speak` / `voicebox.transcribe` / `voicebox.list_profiles` / `voicebox.list_captures` / `voicebox.analyze_sample`。

---

## 3. 路径 A · REST BaseTool（推荐默认）

`tools/audio/voicebox_tts.py` 是 OpenMontage 注册的 `BaseTool`，由 `tools/tool_registry.py` 自动发现；走 `ToolResult` 合约。任何 `execute_tool("voicebox_tts", {...})` 或 `registry.get("voicebox_tts").execute(...)` 调用都会命中它。

### 3.1 调用契约

```python
from tools.tool_registry import registry

result = registry.get("voicebox_tts").execute({
    "operation": "text_to_speech",   # or "clone_voice" / "list_cloned_voices"
    "text": "今天我们讲讲 Qwen3-TTS。",
    "profile_id": "prof_abc123",
    "language": "zh",                # 见 .agents/skills/voicebox/SKILL.md 的 23 语种表
    "engine": "qwen",
    "output_path": "/abs/path/to/projects/<id>/assets/audio/voicebox_x.wav",
    # 或者不传 —— 工具会调 infer_project_dir() 推到 projects/<id>/assets/audio/
    "timeout_seconds": 600,          # 默认 600s；CPU 主机长脚本可上调
})
result.success, result.artifacts, result.error, result.data["generation_id"]
```

三套 `operation` 的请求字段见 `voicebox_tts.py` 里 `input_schema`。要点：

- `clone_voice` 必须传 `name` + `audio_paths`（≥1 个）+ `default_engine ∈ CLONING_ENGINES`（`qwen / luxtts / chatterbox / chatterbox_turbo / tada`，**不能** `kokoro`/`qwen_custom_voice`）。可选 `reference_texts`（1:1）或 `reference_text`（fallback）。
- `text_to_speech` 必须传 `text` + `profile_id`。`engine` 可省略，回落 profile 的 `default_engine`。
- `list_cloned_voices` 默认只返 `voice_type=cloned`；`include_presets=True` 加 preset / designed。

### 3.2 内部时序（REST 端到端）

```
Python caller
  └── voicebox_tts.execute()
        ├── POST /profiles/{id}/samples  (clone_voice 路径，跳过)
        ├── POST /generate              ──► { id: <gen_id>, status: "queued" }
        ├── GET  /generate/{id}/status  (SSE，1s 心跳，封顶 600s)
        │      ├─ status=completed  ──► keep reading duration field
        │      ├─ status=failed     ──► ToolResult(success=False, error=voicebox_error)
        │      └─ stream closes early ──► ToolResult(... status="timeout")
        └── GET  /audio/{id}  (binary, 64KB chunks) ──► fs write to projects/<id>/assets/audio/
```

返回的 `ToolResult`：

```python
ToolResult(
    success=True,
    data={
        "provider": "voicebox",
        "generation_id": "...",
        "profile_id": "...",
        "engine": "qwen",
        "language": "zh",
        "duration": 3.42,
        "output": "/.../voicebox_<gen_id>.wav",
        "model": "qwen",
    },
    artifacts=["/.../voicebox_<gen_id>.wav"],
    model="qwen",
)
```

### 3.3 健康检查与 fallback

- `get_status()` 探 `GET {VOICEBOX_REST_URL}/health`：200 → AVAILABLE；502/503/504 → DEGRADED；连不上 → UNAVAILABLE。这就是 `make preflight` 列出来的状态。
- `fallback = "elevenlabs_tts"`, `fallback_tools = ["elevenlabs_tts", "piper_tts"]`。voicebox 不可用时 `tts_selector` 自动降级到 ElevenLabs → OpenAI → Piper。
- Voicebox 离线 + ElevenLabs 也没 API key？**`tools/audio/voicebox_tts.py:392-400`** 会回 `Voicebox REST call failed: ... Is Voicebox running at {base}?`，`tts_selector` 再走下一档。

### 3.4 REST 路径的硬要求

| 项 | 行为 |
|---|---|
| `X-Voicebox-Client-Id` header | **强制**。工具默认写 `openmontage-tts`。Voicebox 的 loopback middleware 用它做 per-client 策略（audio_path gating、default voice binding）。loopback 调用也要写。 |
| `output_path` 必须落在 `projects/<id>/` 下 | Backlot board 只扫描这里。`infer_project_dir(inputs)` 解析不出就退到 cwd —— **这违反工作区契约，要尽量避免**。 |
| 长轮询上限 | `DEFAULT_GENERATION_TIMEOUT_S = 600`。CPU 主机跑 `qwen 1.7B` 极长脚本要调到 `timeout_seconds` 或环境层兜底。 |
| Cache & 模型权重 | Kokoro 340 MB / Qwen 1.7B 3-4 GB 等。详见 `docs/voicebox-prerequisites.md` —— 没下完 voicebox 不干活。 |
| 文件上传限制 | 多 part `POST /profiles/{id}/samples`，timeout 300s/每文件。整请求体受 ASGI proxy 256 MB 上限（A 路径不走这条，所以实际只受 voicebox 自身限制）。 |

### 3.5 失败模式速查

| 现象 | 原因 | 工具返回 |
|---|---|---|
| `Voicebox REST call failed: ConnectionError … Is Voicebox running at …?` | voicebox 没起 / 端口错 | `success=False`，**`tts_selector` 会切下一个 fallback** |
| `voicebox_tts: 400 {"detail": "engine: 'xyz' not in …"}` | `engine` 拼错 / 不在该 engine 的白名单里 | `success=False`，错误有 voicebox 原生 `detail` 直通 |
| `generation {id} did not complete: timeout after 600s` | voicebox worker 卡死；或 CPU 跑 1.7B 太慢 | `success=False`，`data.status="timeout"` —— **不会自动 fallback**，需要上层决策 |
| `clone_voice created profile X but no samples were uploaded successfully. failures=[…]` | 样本路径不存在或后缀不在 `{wav,mp3,m4a,ogg,flac,aac,webm,opus}` | `success=False`，但 profile 已创建 —— 显式告诉你"半成品"状态；agent 决定 retry 上传还是 `DELETE /profiles/{id}` |
| `voicebox_tts: 502 ...` | 模型还在加载 / 上游炸 | `_http_error` 包成 `success=False`；`RetryPolicy(max_retries=1, retryable_errors=["502", ...])` 自动重试一次 |
| `Requests dependency missing` | `pip install requests` 没装（基础 dev deps 里有，但生产环境要确认） | ImportError —— 修依赖 |

---

## 4. 路径 B · MCP（两条子路径）

### 4.1 路径 B1 · OpenMontage MCP 服务器包装层

代码：`mcp_server.py:725-870`。三个 FastMCP tool（`@mcp.tool()` 装饰器）：

```
voicebox_tts            registry.get("voicebox_tts").execute({"operation":"text_to_speech", ...})
voicebox_clone_voice    registry.get("voicebox_tts").execute({"operation":"clone_voice", ...})
voicebox_list_cloned_voices  registry.get("voicebox_tts").execute({"operation":"list_cloned_voices", ...})
```

每一个都是把 BaseTool 包成 MCP 工具，传到 `asyncio.to_thread(ctx.run, tool.execute, inputs)` 跑同步 BaseTool 然后把 `ToolResult` 重新包成 `ExecuteResult`。

**调用方**：

```bash
curl -s -X POST http://127.0.0.1:8900/mcp/ \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"voicebox_tts","arguments":{"text":"…","profile_id":"…","engine":"qwen","language":"zh"}}}'
```

**鉴权栈**：

```
caller --[Bearer MCP_API_TOKEN]--> :8900/mcp/* (BearerTokenAuthMiddleware)
                                          │
                                          └── mcp_server.py::voicebox_tts  (FastMCP wrapper)
                                                    │
                                                    └── voicebox_tts BaseTool.execute(...)
                                                              │  (tool 自己再写 X-Voicebox-Client-Id)
                                                              └── Voicebox :17493
```

### 4.2 路径 B2 · ASGI 反向代理

代码：`mcp_server.py:2502-2683` 的 `_voicebox_proxy_handler` + `_VoiceboxProxyApp`，mount 在 `:8900/voicebox/mcp/{path:path}`（line 2751）。

**和 B1 的区别：**

| 维度 | B1（包装层） | B2（反向代理） |
|---|---|---|
| 暴露的 MCP 工具名 | `voicebox_tts` / `voicebox_clone_voice` / `voicebox_list_cloned_voices` | `voicebox.speak` / `voicebox.transcribe` / `voicebox.list_profiles` / `voicebox.list_captures` / `voicebox.analyze_sample` |
| 工具定义在哪 | OpenMontage mcp_server.py | voicebox 自己的 FastMCP server (`/opt/voicebox/backend/mcp_server/`) |
| 底层 | **REST BaseTool**（B1 走的是 A 路径的工具） | **直连 voicebox MCP**（B2 一次性穿透） |
| SSE 行为 | 不暴露 —— MCP 只返 generation_id，需要时再走 `wait_for_generation` 助手段 | **流式原样转发** —— `/generate/{id}/status` 的 SSE 边到边抵达 |
| 鉴权 | Bearer 必须，由 OpenMontage 强制 | Bearer 必须；proxy **剥离** Authorization，再注入 `X-Voicebox-Client-Id: voicebox-relay`（loopback 受信） |
| Body cap | FastMCP 的 schema 限制 | ASGI 显式 **256 MB** 上限（`_VOICEBOX_MAX_BODY_BYTES`），防止 OOM |
| Hop-by-hop 头处理 | 不涉及 | 显式过滤 `connection/keep-alive/transfer-encoding/host/content-length/upgrade` |

**什么时候 B1，什么时候 B2：**

- 调 `voicebox_tts` / `voicebox_clone_voice` / `voicebox_list_cloned_voices`（OpenMontage 自己的命名） → **B1**
- 调 `voicebox.speak` / `voicebox.list_profiles` / `voicebox.analyze_sample` 等 voicebox 原生 5 个 MCP tool → **B2**

注意 B1 不暴露 `speak`，B2 不暴露 `voicebox_tts`。**两个端点的工具集不重合**。

### 4.3 MCP 调用方典型：Claude Code 之类的 agent runtime

`.mcp.json`（已存在，见 `docs/openmontage-integration.md` 的 Wired MCP Configuration 段）：

```jsonc
{
  "mcpServers": {
    "voicebox": {
      "type": "http",
      "url": "http://127.0.0.1:17493/mcp",     // 不走 OM，直接 dial voicebox
      "headers": { "X-Voicebox-Client-Id": "claude-code" }
    }
  }
}
```

但 Claude Code 也可以配置走 `:8900/voicebox/mcp/*`（B2 路径），把 Bearer 鉴权集中到 OpenMontage 一处管理 —— 见部署 runbook 的多上游段。两种都能跑，关键看你要不要 **统一 Bearer 鉴权**。

---

## 5. REST vs MCP 决策表

挑哪条路径，不看"哪个更现代"，看下面这个矩阵：

| 场景 | 推荐 | 理由 |
|---|---|---|
| OpenMontage 内部 pipeline（tts_selector / 任何 `execute_tool(...)`） | **A · REST** | `ToolResult` 直接对齐；自动 fallback 链；同进程内无 JSON-RPC 开销 |
| OpenMontage 内部的 agent 主循环写代码 | **A · REST** | 同上；并且可以直接 `import tools.audio.voicebox_tts` 走到工具 |
| 从 OpenMontage **外部**的 Claude Code / Cursor / Windsurf 实例接入 | **B2 · MCP 反向代理** | 客户端原生 MCP，无须 Bearer 之外的额外设置；tool 名是 voicebox 标准集合 |
| 想统一管理所有外部 agent 鉴权到 OpenMontage | **B2 · MCP 反向代理** | Bearer 在 `:8900` 一处强制；Voicebox 自己不用放任何 token |
| MCP 客户端需要 `voicebox_tts` / `voicebox_clone_voice` 这种 **OpenMontage 命名**的 tool | **B1 · 包装层** | 这俩名字只有 B1 暴露 |
| MCP 客户端要 voicebox 的原生 `speak` / `transcribe` / `list_captures` / `analyze_sample` | **B2 · 反向代理** | 这 5 个名字只有 B2 暴露 |
| 极简部署：完全不想跑 `mcp_server.py` | **A · REST**（仅 client 端直连 `:17493`） | OM 不参与；但失去自动 Bearer、失去 fallback 注入、失去 OM 的健康监控 |
| 浏览器 SPA / Web UI 想调 voicebox | 都不行 | MCP 是有状态长协议，浏览器 fetch 不能裸用 —— 见 `docs/openmontage-integration.md` 的 "Voicebox 网页调用限制" 段。需要 BFF。 |

---

## 6. 三条路径的链路延迟 & 失败放大器

> 用 X-Voicebox-Client-Id 注入 + JSON-RPC schema 校验 + SSE 透传层数衡量"如果中间坏了，难调多少"。

| 维度 | A · REST | B1 · MCP 包装 | B2 · MCP 反向代理 |
|---|---|---|---|
| 进程内调用 | 0 | 0（`asyncio.to_thread`） | 0 |
| JSON / SSE parse | 工具自己（`requests` + `iter_lines`） | OpenMontage FastMCP 框架解一层 | 客户端 MCP 框架解 + proxy 流式转发 |
| 鉴权层数 | 0 | 1（OM Bearer） | 2（OM Bearer + Voicebox Client-Id 注入） |
| 出错时优先怀疑 | voicebox REST | voicebox REST 或 FastMCP schema 不一致 | proxy 配置 / body 上限 / SSE 中断 |
| SSE 长连 | 自己控制超时 | **不暴露 SSE** | **保真透传** |
| 失败时 fallback | 工具层 + tts_selector 双层 | 双层（同左） | 没有 fallback 注入层 —— 客户端自己处理 |

---

## 7. 自检 / 排障 check-list

### A · REST

```bash
# Voicebox 在跑？
curl -s -H 'X-Voicebox-Client-Id: probe' http://127.0.0.1:17493/health
# 200 → AVAILABLE；502/503 → DEGRADED；连不上 → UNAVAILABLE

# 一个最小化 clone
python -c "from tools.tool_registry import registry; \
r = registry.get('voicebox_tts').execute({ \
  'operation':'clone_voice','name':'smoke','audio_paths':['/abs.wav'], \
  'default_engine':'qwen'}); print(r)"
```

### B1 · MCP 包装

```bash
# Bearer 必须
curl -s -X POST http://127.0.0.1:8900/mcp/ \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name' | grep voicebox
# 应该看到 voicebox_tts / voicebox_clone_voice / voicebox_list_cloned_voices
```

### B2 · MCP 反向代理

```bash
curl -s -X POST http://127.0.0.1:8900/voicebox/mcp/ \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
# 应该看到 voicebox.speak / .transcribe / .list_profiles / .list_captures / .analyze_sample
# 这五个名字来源于 /opt/voicebox/backend/mcp_server/tools.py
```

### 看 proxy 是否把 Authorization 正确剥离 + 注入 Client-Id

```bash
# 让 :8900 上日志能说话；如果看到 403，99% 是 proxy 把 Authorization 透传过去了
grep -E "voicebox_proxy|RequestEntityTooLarge" /opt/OpenMontage_Voicebox/mcp_server.log
```

### 集成测试

```bash
# A
python -m pytest tests/integration/test_voicebox_rest.py -v
# B1（要 Bearer + voicebox 可达）
python -m pytest tests/integration/test_voicebox_mcp_via_openmontage.py -v
# B2（直连 voicebox :17493）
python -m pytest tests/integration/test_voicebox_mcp_direct.py -v
```

---

## 8. 与现成文档的关系（不要再造轮子）

| 看什么 | 文档 | 为什么不算重复 |
|---|---|---|
| 谁调用谁（Direction A/B/C） | `docs/openmontage-integration.md` | 关注的是 **方向**，不是 **形态**；本文是它的 **子集具体化** |
| 装 voicebox 的前置（HF 代理、模型权重） | `docs/voicebox-prerequisites.md` | 三条路径共用的 prerequisite，跟形态无关 |
| 装 voicebox 的常见坑 | `docs/voicebox-installation-pitfalls.md` | 同上 |
| Voicebox 怎么挑引擎、语种表 | `.agents/skills/voicebox/SKILL.md` | Layer 3 vendor 知识，三条路径都读它 |
| Voicebox REST/SSE 端点字段 | `.agents/skills/voicebox/reference.md` | API 字典 |
| OM agent 怎么把 voicebox 当可用工具（运行时判断） | `skills/pipelines/...` 的 stage director + 这个文件的 §5 决策表 | 把 §5 接到 stage skill 的 "tool picker" 段即可 |

---

## 9. 推荐落地动作

1. **默认 REST（A）。** OpenMontage 主线已经全部走 A，不要为了"统一 MCP"把管线内部也改 JSON-RPC，得不偿失。
2. **保留 B1 + B2。** 它们面向 **OpenMontage 之外的 MCP-aware agent 客户端**，和 A 是补充关系，不是替代。
3. **新加 voicebox 能力时**：先扩 `voicebox_tts.py` BaseTool（统一 fallback、idempotency、`get_status`），再让 B1 包装层通过 `inputs` 转发；B2 一般不需要改，Voicebox 自家的 MCP server 已经覆盖常见 5 个工具。
4. **改 voicebox REST 字段时**：先看 `.agents/skills/voicebox/reference.md` 是否还准；如果加了 endpoint，记得在 `voicebox_tts.py` 加 + `tests/integration/test_voicebox_rest.py` 加回归。
5. **若增加 web UI 入口**：先看 `docs/openmontage-integration.md` 的 "Voicebox 网页调用限制" 段；纯 MCP 不能裸接浏览器的 fetch，要先在 Voicebox 一侧（或 OpenMontage BFF）加 REST 适配。

