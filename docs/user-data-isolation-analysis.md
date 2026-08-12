# 用户数据隔离现状分析

**日期**: 2026-08-13
**状态**: 现状分析（基线文档）
**相关文档**:
- [`plans/remotion-multiuser-concurrency-isolation.md`](plans/remotion-multiuser-concurrency-isolation.md) — 多用户并发与数据隔离的实现规格（本次已落地三个补丁）
- [`render-queue-analysis.md`](render-queue-analysis.md) — 渲染队列可行性分析

---

## 结论

OpenMontage 当前**没有真正的"用户"概念**。区分数据用的是三个维度：**MCP 会话 digest**、**project_id**、**render job（staging_id）**。系统是"会话隔离 + 项目隔离"的逻辑模型，不是多租户用户隔离。

## 一、身份键：会话 → digest

- 客户端建立 MCP 连接时，服务端（`stateless_http=False` 的 streamable-http 会话管理器）发一个不透明 `Mcp-Session-Id`，客户端后续请求带回来。
- 服务端对原始 id **不做落盘/日志**，只在磁盘/日志里使用其哈希：`session_hash(sid) = sha256(sid)[:16]`（`lib/workbuddy_session.py`）。所有磁盘命名都用这个 digest，不是原始 id。

> 会话 id **不是用户账号**——它是每个客户端连接发的随机令牌，不关联任何登录身份。实际上的"用户"= "一个 MCP 会话"。

## 二、各类型数据的隔离边界

| 数据 | 存放位置 | 命名空间 | 隔离粒度 |
|---|---|---|---|
| 会话状态（批次图片清单、渲染状态、分享链接） | `projects/.mcp_sessions/<digest>.json` | `digest` | 每会话一个文件 |
| 上传的素材文件 | `projects/<project_id>/assets/_sessions/<digest>/`（`tools/asset_upload.py`） | `project + digest` | 每会话子目录 |
| 渲染产物 MP4 | `projects/<project_id>/renders/` | `project` | 每项目目录 |
| Remotion 素材 staging | `remotion-composer/public/_staged/<staging_id>/` | `staging_id`（= render job） | **每 job**（2026-08-13 补丁修复，此前为全局共享） |
| 渲染临时 props | `<output_dir>/.remotion_props.<staging_id>.json` | `staging_id` | **每 job**（同上） |
| 渲染任务索引 | `projects/.mcp_sessions/.job_index.json`（`render_job_id → digest`） | `job_id` | O(1) 定位到会话 |

会话状态层还强制了单项目约束：`register_image` 在 `project_id` 变化时报错；同一会话的并发渲染由 `begin_render` 串行拒绝（"already rendering"）。

## 三、关键局限（诚实评估）

1. **鉴权是单一共享 Bearer token**（`MCP_API_TOKEN`）：所有客户端用同一个 token 访问，服务端不区分"是谁"。会话隔离是**客户端连接粒度的逻辑隔离**，不是多租户用户隔离——任何拿到 token 的人都可以开新会话。
2. **逻辑隔离，非硬隔离**：所有数据在同一个进程、同一个 root 用户下读写，文件系统层面无权限区隔。A 会话通过 API 读不到 B 会话的状态（服务端按 digest 定位），但同一进程内/文件系统层面都能访问。
3. **微云上传目标共享**：`_run_render_job` 中 `weiyun_upload` 传 `target_dir: ""`（默认目录），最终视频上传到**公共目标位置**，未按会话/项目分目录。
4. **会话状态只存在本机**：`projects/.mcp_sessions/` 是单机文件，无中心化用户数据库。

## 四、与本次补丁的关系

本次 `plans/remotion-multiuser-concurrency-isolation.md` 的三个补丁，把 **Remotion 渲染内部（staging + 临时文件）** 从"全局共享"补成了"每 job 隔离"——这是渲染管线里最后一个未隔离的环节。

但真正的**多用户**（各自 token/账号、各自配额、各自云端存储）尚不存在，属于该文档 §7 的后续路线：

1. 资源感知调度器（`lib/scheduler.py`）
2. 收敛两套 Remotion 运行时（内置 `npx` 路径 vs `remotion-server` 4000 + Redis/BullMQ）
3. Docker worker 池（安全隔离成硬需求时）——统一队列 + per-job workspace + 对象存储

## 五、后续改造方向（如需多租户）

若目标是「每个用户独立 token + 各自配额 + 各自微云目录」，需要加一层**用户身份模型**（用户 → 会话、配额、存储目录的映射），而不仅是会话隔离。本文档是任何多租户改造的基线。
