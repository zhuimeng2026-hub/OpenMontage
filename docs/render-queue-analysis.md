---
name: render-queue-feasibility
description: 渲染队列系统可行性分析 — 当提交超过系统并发处理能力的任务时如何排队
metadata:
  type: reference
---

# 渲染队列系统可行性分析

**日期**: 2026-08-03
**结论**: 完全可行，架构天然适合接入队列。

## 当前架构

OpenMontage 是 AI Agent 驱动的视频生产系统，三层架构：Agent → Pipeline → Python Tools (BaseTool子类)。

关键现状：

- 所有工具 `ExecutionMode.SYNC`，阻塞直到完成
- **无并发控制** — 没有信号量、锁、worker pool、或资源调度
- MCP Server 用 `asyncio.to_thread()` 包裹同步执行，但没有并发限制
- `ResourceProfile` (cpu_cores/ram_mb/vram_mb) 已声明在 BaseTool 上，但**仅用于展示，未被调度器消费**
- 远程 API 工具（Kling/Minimax）用 `_relay.py` 的 submit→poll→download 模式，不争抢本地资源
- Remotion 内部用 `REMOTION_CONCURRENCY` 限制 Chromium 标签页数 (min(cores,8))，但仅限单次渲染内部
- `clip_cache.py` 有 filelock 用于并发安全

## 问题场景

当多个 Remotion 渲染 + FFmpeg 编码同时执行时，CPU/内存竞争导致 OOM、超时、渲染失败。

## 推荐方案

### 第一步（止血）: Semaphore 限流

在 `mcp_server.py` 的 `execute_tool()` 中加 `asyncio.Semaphore(N)`，限制 `video_post` capability 并发度 1-2。约 10 行改动。

### 第二步（根治）: 资源感知调度器

新增 `lib/scheduler.py`，基于 `ResourceProfile` 做准入控制：

```
RenderScheduler:
  - available_slots = {cpu, ram, vram}
  - pending: FIFO 等待队列
  - active: 正在执行的任务
  - submit(job) → {status: "active"|"queued", position: N}
  - on_complete() → release + drain_queue()
```

接入点：
- MCP Server 层: 全局准入（简单，改动小）
- video_compose 工具层: 资源感知准入（精确，改动大）
- Backlot SSE 事件系统可直接复用来推送排队进度

## 接口设计（草案）

```python
@dataclass
class RenderTicket:
    status: Literal["active", "queued"]
    position: int          # 0=正在执行, >0=队列位置
    estimated_wait_s: float | None

class RenderScheduler:
    def submit(tool_name: str, resource_profile: ResourceProfile) -> RenderTicket
    def release(job_id: str) -> None
    def status(job_id: str) -> RenderTicket
```

## 不需要队列的场景

Kling、Minimax 等外部 API 工具已对接 fal.run 的异步队列，只消耗网络带宽和少量 CPU（下载），不与 Remotion/FFmpeg 争抢本地资源。

**Why:** 当多个渲染任务同时提交时，系统没有资源调度机制，CPU 和内存竞争会导致所有渲染变慢甚至失败。加入队列系统可以串行化重负载渲染任务，保护系统稳定性。

**How to apply:** 参考此文档理解架构边界和接入点；实施时先做 Semaphore 限流（改动最小），再按需升级到资源感知调度器。外部 API 类工具不需要纳入本地队列。
