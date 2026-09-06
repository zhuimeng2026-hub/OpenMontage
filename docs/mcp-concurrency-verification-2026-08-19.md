# MCP 多进程并发验证报告 — 2026-08-19

> 验证目标：`http://lanes.ymxt.top:8900/mcp`（OpenMontage v1.29.0）
> 工具：`/tmp/conc_probe.py`（multiprocessing，最小 HTTP 客户端；无 PIL/fastmcp 依赖）
> 触发条件：goal=多进程并发验证（受 Stop hook 守卫）
> 关联文档：`docs/mcp-external-call.md`（9b5524c 新增）
> 关联代码：`mcp_server.py` v1.29、`tools/asset_upload_chunk.py`、`lib/render_queue.py`

---

## 1. 测试矩阵与结果

| 场景 | 并发度 | 进程/会话模型 | 全部 200? | wall time | 关键观察 |
|---|---|---|---|---|---|
| A1: initialize | 8 进程 | 8 个独立 sid | ✅ | 121ms | sid 全 32-hex 唯一，延迟 73–107ms |
| A2: initialize | 50 进程 | 50 个独立 sid | ✅ | 529ms | 延迟 70–1181ms，4 个 >1s（fastmcp session manager 抖动） |
| B:  intra-session tools/list | 6 会话 × 4 并发 | 同 sid 内并发 | ✅ | 478ms | 全部 200，响应 26853B 一致 |
| C1: chunk start→append→complete | 10 进程 | 10 个独立 sid | ✅ | 647ms | start 72–168ms / append 132–175ms / complete 104–164ms |
| C2: chunk 完整链路 | 30 进程 | 30 个独立 sid | ✅ | 4338ms | start 132–2174ms / append 55–2186ms / complete 33–2210ms |
| C3: chunk start 仅（.bin 后缀） | 30 进程 | 30 个独立 sid | ✅ | 330ms | start 阶段 100% 200 + 结构化 `success=False error='unsupported media extension'` |
| D:  get_session_assets | 8 session | 8 个独立 sid | ✅ | 200ms | 每个 session 返回自己的 `"assets": []`，隔离有效 |

> 所有 wall time 均 < 5s，所有进程均存活至结束（`alive_procs=0`）。未观察到卡死/超时/5xx。

---

## 2. 服务端并发原语（代码事实）

读 `mcp_server.py` + `lib/render_queue.py` 得到的服务端并发模型：

| 原语 | 位置 | 作用 | 风险 |
|---|---|---|---|
| `asyncio.to_thread(ctx.run, tool.execute, ...)` | `_run_tool_sync` (line 264) + `execute_tool` (line 605) | 把同步 `BaseTool.execute` 丢到默认 executor | 默认 executor 卡死 → 全部后续上传挂起 |
| `await asyncio.wait_for(fut, timeout=900)` | line 287 | 单次工具调用安全网 15 分钟 | 超时后直接 raise，FastMCP 回 500 |
| `_start_executor_health_monitor` | line 301-356 | 每 30s 用 no-op 探测 executor；8s 无响应即 `set_default_executor(new ThreadPoolExecutor(max_workers=32))` | 这是 wedge 自愈机制；客户端无法关闭 |
| `_tool_pending` + `threading.Lock` | line 116-137 | 在飞工具调用计数 | 健康监控据此判断是否还有未消化请求 |
| `_retry_publish_locks: dict[job_id, Lock]` + `threading.Lock` 守护 | line 1162-1168 | 每 job_id 一个重试互斥锁；并发 retry 同一 job 第二个立即拒绝 `in_progress` | 防止同一 job 重复上传微云 |
| `FairRenderGate` + `_fair_gate_guard` | `lib/render_queue.py` | 渲染公平队列 | 提交后等 `threading.Event` 5s 注册到 gate |
| `threading.Thread(target=_run_render_job, daemon=True)` + `threading.Event(queue_ready)` | line 988-996 | 渲染守护线程 + 提交者等 5s 注册 | daemon 线程不阻止进程重启 → 重启丢渲染 |
| `stateless_http=False` | line 383 | 强制要求 `Mcp-Session-Id` 复用 | 客户端必须保留 sid，否则 401/`session required` |
| `contextvars.copy_context()` | line 282 | 把 ASGI 中间件设的 sid 复制到 to_thread 内 | 没这段 to_thread 内 `get_mcp_session_id()` 返回 None → `Mcp-Session-Id is required` |

---

## 3. 文档与实际的偏差（按严重度排序）

### 🔴 P0 — 文档示例照搬会全部失败

| 项 | 文档说 | 实际（实测 + 代码） |
|---|---|---|
| **缺 `Accept` header** | 所有 curl 例只设 `Content-Type` | 实测无 `Accept` 必回 `-32600 Not Acceptable: Client must accept application/json`；必须 `Accept: application/json, text/event-stream` |
| `upload_asset_chunk` 字段 | `file_path`、`chunk_index`、`total_chunks`、`project_id` | 实测 schema：`operation`/`upload_id`/`offset`/`chunk_base64`/`project_id`/`filename`/`total_bytes`/`mime_type`/`sha256`；**完全无 chunk_index/total_chunks** |
| `chunk_size` 建议 | 2MB | 实测 start 响应 `chunk_limit_bytes=1048576`（1 MiB） |
| `upload_asset` 字段 | `file_path`、`project_id`（可选）、`content_type` | 实测 schema：`project_id`(必填 str)、`filename`(必填)、`content_base64`(必填)、`mime_type`、`sha256`、`overwrite`；**MCP 端拒绝客户端本地路径**（代码注释明确："Client-local paths are intentionally not accepted because the MCP server cannot access the caller's filesystem"） |

### 🟠 P1 — 文档描述与实际行为不符

| 项 | 文档说 | 实际 |
|---|---|---|
| 状态机 | `pending → rendering → uploading → published`，可 `failed`（4 步） | `queued → rendering → rendered → uploading → sharing → published`，可 `failed`（6 步）；retry 路径还会回退到 `sharing` 再 `published` |
| 状态注释里的可轮询值 | `pending \| rendering \| uploading \| published \| failed` | `queued \| rendering \| rendered \| uploading \| published \| failed` |
| `expires_at` 示例值 | `"2025-08-20T..."` | 当前日期 2026-08-19；该占位符带过时日期，会被读为真实数据 |
| `upload_asset.id` 示例值 | `"img-20250819-001"` | 同上 — 过时日期 |
| BFF `upload_asset_chunk` 上传示例 | `for i in $(seq 0 4); do ... curl ... chunk_index=$i ...` | 用 `chunk_index`/`total_chunks` 调用服务端会因 `operation` 缺失直接返回参数错误 |
| weiyun 两个工具"会话相关"列 | 留空 | 实际都传 `mcp_session_id` 内部绑定；属于"会话相关" |

### 🟡 P2 — 文档未提及的并发约束

| 约束 | 含义 |
|---|---|
| `stateless_http=False` | 客户端必须复用 `Mcp-Session-Id` 才能访问同一会话；同 session 串行工具调用语义明确，但同 session 并发也已实测支持 |
| `asyncio.to_thread` wedge 自愈 | 服务端有自愈逻辑，但**有 ≤30s + 8s 检测窗口**；这期间客户端请求会全部挂起 |
| 单工具硬超时 900s | chunk append/complete 在极端情况下会被服务端硬切 |
| 渲染调度在 daemon 线程 | 重启 MCP 会丢正在渲染的任务；`recover_orphans_and_rebuild_index` 只救已持久化的 job |
| FairRenderGate 5s 注册等待 | `create_remotion_video_share` 内部会等 worker 进入 gate，超时仍返回 queued 但实际未入队 |
| per-job publish 重试锁 | `retry_render_publish` 对同一 `render_job_id` 并发调用：第一个获得锁；其余立即返回 `stage=in_progress error="A publish retry is already in progress for this render job"` |

### 🟢 P2 — 文档正确但可补强

- 工具清单中 `upload_asset` 标"复用同一 MCP 会话"是对的，但**没说 BFF/Curl 必须显式带 `Cookie: ff_sid=`（BFF）或 `Mcp-Session-Id:`（直连）**——否则服务无法把请求关联到 session 资产。
- "BFF 路径 = 浏览器前端路径"没说明 `get_render_status` 返回里有 `queue_position/queue_depth`（SSE 友好字段，浏览器轮询可利用）。

---

## 4. 并发安全结论

| 维度 | 结论 |
|---|---|
| Session 唯一性 | 50 并发 initialize 全独立 sid，无冲突 ✅ |
| Session 隔离 | 8 并发 get_session_assets 各自返回空资产，无串扰 ✅ |
| 同 session 并发 | 6 session × 4 tools/list 全 200，fastmcp 内部分发正确 ✅ |
| 多进程并发 chunk 链路 | 30 并发 start→append→complete 全 200，无 wedge ✅ |
| 多进程并发失败路径 | `.bin` 后缀被服务拒绝（结构化错误返回，无 hang） ✅ |
| 进程死亡/超时 | 所有进程正常退出；无超时（默认 30s 内完成） ✅ |
| 限流迹象 | 50 并发 initialize 出现 4 个 >1s 请求（占 8%），未触发 5xx；但存在 soft 抖动 ⚠️ |

**未观察到 wedge**：仓库 `monitor_render/load_test.py` 的攻击面是 `asyncio.to_thread` 卡死，需要更高并发 + 慢工具实现才会复现（当前远程 30 并发未触发）。建议长期监控 `_log.error("executor.health.wedge ...")` 与 `_log.error("tool.sync.timeout ...")` 出现频率。

---

## 5. 修复建议（按 ROI 排序）

1. **【文档 P0】** 所有 curl/BFF/JSON-RPC 示例加 `Accept: application/json, text/event-stream`。否则读者照搬必败。
2. **【文档 P0】** 整段重写 `upload_asset` / `upload_asset_chunk` 章节，按 `tools/list` 真实 schema + `mcp_server.py` 真实签名填表。
3. **【文档 P0】** 改 chunk_size 建议为 ≤ 1 MiB（即服务端响应里的 `chunk_limit_bytes`）；并说明这个值是服务端约定的，客户端必须按它切片而不是预估。
4. **【代码 P1】** 在 `mcp_server.py` 鉴权层前加一行：缺 `Accept` header → 返回 400 + 一段更清晰的错误文案（不要走 JSON-RPC 层，因为客户端很可能不是 MCP 客户端而只是 REST 客户端）。可避免文档示例路径全部 422。
5. **【文档 P1】** 状态机补齐 `queued / rendered / sharing`，并加状态转换示意（明确 sharing 只在 retry 路径出现）。
6. **【文档 P1】** 删除占位符里的过时日期（`2025-08-20`、`20250819-001`）；用 `<ISO8601>` / `<job-uuid>` 等通用占位。
7. **【文档 P2】** 新增"并发与限流"小节：Session 复用、`asyncio.to_thread` wedge 风险 + 自愈窗口、单工具 900s 硬超时、`retry_render_publish` per-job 锁。
8. **【监控 P2】** `om_mcp-probe` 加计数器：`executor_wedge` 计数 / `tool_sync timeout` 计数 / 50 并发响应 p99。当前 `mcp_health.log` 已记录但无聚合。
9. **【测试 P2】** `monitor_render/load_test.py` 仅 8 行 `tool` 列表全是 `upload_asset_chunk`；建议扩到 30 进程 × 5 分钟（对齐实测能力），覆盖 `weiyun_*` 并发、retry_render_publish 同 job 互斥、SSE progress 稳定性。

---

## 6. 复测清单

如要复现本次验证：

```bash
TOKEN="h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE"
URL="http://lanes.ymxt.top:8900/mcp"

# 单条探针（确认文档示例失败）
curl -sS -m 5 -X POST "$URL" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1"}}}'
# 期望: -32600 Not Acceptable

# 多进程并发
python3 /tmp/conc_probe.py A    # 8 进程 initialize
CONC_N=50 python3 /tmp/conc_probe.py A
python3 /tmp/conc_probe.py B    # 6 session × 4 并发 tools/list
CONC_N=10 python3 /tmp/conc_probe.py C   # 10 进程 chunk 全链路
CONC_N=30 python3 /tmp/conc_probe.py C
python3 /tmp/conc_probe.py D    # 8 session 资产隔离
```

> 注：`/tmp/conc_probe.py` 不会写入仓库；如需固化进 `monitor_render/` 仓库可复制并改名 `mp_conc_probe.py`。

---

报告作者：Claude（MiniMax-M3）
触发 goal：多进程并发验证（Stop hook 已自动清除）