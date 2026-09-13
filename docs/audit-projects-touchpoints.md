# Audit Report: `projects/` filesystem touchpoints in OM

> Date: 2026-09-02
> Scope: 全量 .py + .md 关键契约段 + 配置
> Branch: `OpenMontage_Voicebox` (HEAD `d3dffac`, uncommitted Phase B work)
> Output: 仅 audit 报告,不动代码
> Snapshot note: 本文记录的是 Phase C 改造前的 touchpoint inventory，行号与“未改”描述不是当前验收状态。上传、素材读取和一条 Remotion 主路径已在随后 working tree 中改造；其余命中仍需逐项关闭。当前复核结论以 `docs/user-isolation-via-mcp-session.md` 的 “2026-09-02 code/document alignment review” 为准。

---

## Section 1: 上传与素材写入 (asset / image / video)

### `tools/asset_upload.py` — single-shot upload

- 路径构造: `(REPO_ROOT / "projects").resolve() / <project_id> / "assets" / "_sessions" / <session_digest> / ` (lines 114–162)
- 用户归属: **未携带 user_id**;只用 sanitized `project_id` + sha256(`Mcp-Session-Id`)[:16](`session_digest`)作为路径 (line 160)
- 隔离风险: **HIGH**
  - 同一 `project_id` 在两个用户手里落到 **完全相同的目录**;A 用户上传同名文件 + 同 session_digest 会直接覆盖 B 用户的同名文件。
  - 唯一的防护是 `register_image` 的 sha-dedup (line 169–173):不同字节就是 hard error。
  - Phase C 必须把根目录从 `projects/<id>/...` 改成 `projects/users/<namespace_key>/<id>/...`,否则不可能物理隔离。
- 写入语义: 一次性 `os.replace(tmp, target)` (line 181);覆盖式但带 dedup;`overwrite=True` 时强制覆盖 (line 168)
- 并发: 进程内通过 `register_image` 拿到 `_lock_for(session_digest)` + `_flock_for(session_digest)` 双锁 (走 workbuddy_session 的 flock,line 332)

### `tools/asset_upload_chunk.py` — resumable chunked upload

- 路径构造:
  - `projects/.uploads/{upload_id}.json` + `{upload_id}.part` (lines 91–92)
  - 最终落点 `projects/<project_id>/assets/_sessions/<session_digest>/` (line 169)
- 用户归属: **未携带 user_id**;`upload_id` 是调用方传入的 32-hex(uuid4),session_digest 是 sha256(session_id)[:16]
- 隔离风险: **HIGH**
  - `.uploads/<upload_id>.json` 是 **shared global namespace**,任何用户只要拿到一个未过期的 upload_id 就可继续上传 (line 140–143 只校验 `state_path.is_file()`,**不做 user 归属校验**)。
  - 唯一的"归属"防线是 line 147:`if state.get("session_hash") != current_session_hash: raise`。这只防"两个会话",不防"两个用户共用一个会话"。
  - `append`/`complete` 的 `target` 路径(line 169)与 `asset_upload.py` 共享同一个根问题,需要 Phase C 改造。
- 写入语义:
  - `.uploads/<upload_id>.json`:覆盖式 `state_path.write_text(...)` (line 136)
  - `.uploads/<upload_id>.part`:`append` 模式 (line 157)
  - 最终落点:`os.replace(part_path, target)` (line 190) 一次性原子替换
- 并发: 单 upload_id 上的 offset 校验 (line 152–154) 是唯一串行化

### `tools/asset/read_session_asset.py` — BFF 拉缩略图

- 路径构造: `(_REPO_ROOT / norm).resolve()` (line 69),校验后必须落在 `_PROJECTS_ROOT` 之下 (line 73–77)
- 用户归属: **未携带 user_id**;只校验"在 `projects/` 下",**不校验"在当前 principal 的 namespace 下"** —— v2 doc §Phase C line 142 已点名
- 隔离风险: **MEDIUM**
  - relative_path 是用户提供的字符串,sanitize 后必须以 `projects/` 为前缀;但 `projects/<project_id>/assets/...` 这一段对所有用户是同一个根,**没有任何用户归属字段**。
  - 利用门槛:攻击者需要知道确切文件名 + 路径;但 `assets/_sessions/<session_digest>/` 一旦泄露就跨用户可达(见 `tests/test_read_session_asset.py:81` 的 negative test)。
  - **Phase C 必须额外验证 `path.resolve()` 落在 `current_principal().namespace_key` 的子树之内**。

### `tools/asset/read_session_asset_image.py` — MCP `Image` content 变体

- 路径构造: 复用 `ReadSessionAsset._validate_relative` (line 90) —— 共享同一份白名单
- 用户归属/隔离风险: 同上,**MEDIUM**

### `tweak_server/app.py` + `tweak_server/assets.py` — Sidecar FastAPI 旁路

- 路径构造:
  - `tweak_server/app.py:152,305`: `PROJECTS_DIR / project_id` 直接拼用户传入的 `project_id`
  - `tweak_server/assets.py:51`: 同样,`_project_dir(project_id)` 用 `_SAFE_PROJECT`-等价的 grep 校验
- 用户归属: **未携带 user_id**;只校验 `TWEAK_SERVER_BEARER` (一个共享 token,见 `tweak_server/app.py:43` `auth.py:require_token`)
- 隔离风险: **HIGH**
  - 这是 OM 旁路出来的 sidecar,**完全不在 BearerTokenAuthMiddleware 的范围**。任何拿到 `TWEAK_SERVER_BEARER` 的人都可以读写 `projects/<任意 project_id>/...`。
  - `_project_dir` 只防 `..`/路径分隔符,**不防 user 误填另一个用户下的 project_id**。
  - 已知可命中: 决策日志 (`/api/projects/{id}/tweak` 写 `decision_log_tweak_revNNN.json`,line 199–215)、`remotion_props.json` 模板加载 (line 178–186)、`/renders/{project_id}/{filename}.mp4` 文件直送 (line 141–159)。
- 写入语义:
  - `assets.py:101–118`: 增量 chunk 写,超 limit 即 unlink
  - 同名文件追加 `_1/_2/...` 后缀 (line 90–95) 防覆盖
  - 决策日志: append-only (line 199) by design
- 并发: 单进程 FastAPI 内部;无跨进程锁

---

## Section 2: 会话状态

### `lib/workbuddy_session.py` — 每 MCP session 的 batch 状态

- 路径构造: `projects/.mcp_sessions/<session_digest>.json` (line 84)
- 用户归属: **仅用 `session_hash(sha256(Mcp-Session-Id)[:16])` 命名**,不携带 user_id / namespace_key (line 28–32)
- 隔离风险: **MEDIUM**
  - session_id 由 FastMCP 生成,32-hex/UUID,碰撞概率极低;**但与 user_id 完全无关** —— 同一进程内 user_A 和 user_B 各自有自己的 session,落到不同文件,**目录层面安全**,但语义上仍是 session-scoped 而非 principal-scoped。
  - `.mcp_sessions/.job_index.json` (line 139–144) 是 **全进程共享** 的 `render_job_id → digest` 反查表;**任意 user 的 render_job_id 都能被任意 user 通过 SSE 订阅到** (详见 Section 5)。
  - `.mcp_sessions/.locks/<digest>.lock` (line 53, 64) 是 POSIX advisory flock,Windows fallback 到 no-op (line 67–72)。
- 写入语义:
  - 会话文件: tmp + `os.replace` 原子替换 (line 91–121),with Windows "Access Denied" retry
  - job_index: tmp + `os.replace` (line 158–169)
  - 单会话并发: RLock + flock 双锁
- Orphan recovery (`recover_orphans_and_rebuild_index`,line 239–295)扫描整个 `STATE_DIR` 并重建 index —— **跨 user 扫描所有 session 文件**。

### `lib/principal_registry.py` — Phase B 新加的 session→principal registry

- 路径构造: `projects/.mcp_sessions/principals.db` (line 87) —— 与 workbuddy_session 共享同一个 STATE_DIR
- 用户归属: **写入 principal_id + tenant_id + namespace_key**(line 247–249)
- 隔离风险: **LOW**
  - 这是 v2 的修复点;`namespace_key = HMAC_SHA256(secret, principal_id)[:16].hex()` (line 129–136) 保证不可被反推,也无法从 header 注入到路径。
  - 当前只有 `lookup/bind/require/unbind` 4 个 API 被使用;**实际业务代码(mcp_server、tools/)还没有调用 `current_principal()` 来约束路径** —— 这是 Phase C 的核心工作。
  - `get_mcp_session_id_from_scope` (line 420–441) 是 header 解析的唯一入口,sanitize 后再返回;不会泄露 header 原值。

### `lib/render_queue.py` — 渲染队列 + 持久化 job record

- 路径构造: `projects/.mcp_sessions/.render_jobs.json` (line 51, 274–275)
- 用户归属: **仅含 `owner_id` 字段 (line 61),语义上是 "session" 而非 "principal"**;`FairRenderGate` 用 `owner_id` 做轮询公平 (line 89–99)
- 隔离风险: **MEDIUM**
  - `owner_id` 来自 `_run_render_job` 的调用方(line 60–63 `RenderTicket.owner_id`),Phase 3 后是 session_hash,Phase B 未改。
  - 队列状态(等待 set、active 计数)是 **进程内 in-memory**;持久化只在 `.render_jobs.json`(line 274–339)。
  - 跨进程并发: `_jobs_lock = threading.Lock()` (line 53);无 fcntl 锁,但持久化用 `tmp + os.replace`。
  - Phase C 必须把 `owner_id = principal.namespace_key`,并按 namespace 做 cap(目前是全局 `max_per_owner=1`,line 79)。

### `mcp_server.py` — SSE progress + Phase B bind

- 路径构造: `mcp_server.py:1762` `root = _PROJECT_ROOT / "projects" / project` 是当前 **唯一进入 project 写区** 的入口
- 用户归属: **完全没查 principal**;`project` 直接来自 `state["project_id"]` (line 1727)
- 隔离风险: **HIGH**
  - 这是 Phase C 改造的核心命中点:即便 Phase B 已经 bind 了 principal,`create_remotion_video_share` 仍是按 session-bundle-state 里的 project_id 直接定位,**任何 user 都可以读到任何其他 user 的 session asset**,只要 session_state 落到 disk 上。
  - line 1767 的 `path.relative_to(root.resolve())` 只防目录逃逸,不防跨 user。
- SSE 进度订阅 (`render_progress_sse`,line 2934–2995):通过 `lib/render_progress.subscribe(job_id)` 拿队列;`publish` 是按 job_id fan-out (line 50–61),**任何持有 job_id 的客户端都能订阅**;job_id 是 uuid4.hex 不可猜,但**没有 principal 校验**(lib/render_progress.py 全文件都没有 principal)。
- Phase B bind (line 2860–2894):绑定发生在 middleware 路径,`current_principal()` API 已就绪,但下游工具还在用 `current_user_id()` fast-path (line 263) 或 `principal_registry.require(sid)` (line 269) 这两个 "look up 但不强制用于路径" 的入口。

---

## Section 3: 项目级产出 (renders / artifacts / checkpoints)

### `lib/checkpoint.py` — pipeline checkpoint 写盘

- 路径构造: `pipeline_dir / project_id / checkpoint_<stage>.json` (line 174–175),`pipeline_dir` 默认 `PROJECTS_DIR` (line 88)
- 用户归属: **未携带 user_id**;`project_id` 即用户传入的字符串
- 隔离风险: **MEDIUM**
  - 校验:`init_project` 不校验 project_id 格式,任何字符串都可;`write_checkpoint` 也只校验 stage 是否在 pipeline 合法列表 (line 371–379)。
  - 同样的根问题:Phase C 必须把根改成 `projects/users/<namespace_key>/<project_id>/`。
- 写入语义:
  - checkpoint: tmp + `os.replace` 原子替换 (line 461–468)
  - 旧版本 archive 到 `history/` 目录,带 mtime (line 266–302)
  - decision_log: 读-合并-写 (line 309–336),append-only 语义
- 并发: 无文件锁;靠 RLock 在 mcp_server 调用方串行化

### `tools/publishers/export_bundle.py` — 把 renders/ 打包到 exports/

- 路径构造: `Path(result.data["export_path"])` (line 148, 测试代码可见),生产路径推断:line 291–303 默认 `projects/<name>/exports/`
- 用户归属: **未携带 user_id**;仅靠 `name` 推断 project_id
- 隔离风险: **LOW** —— 这是打包工具,只读自己 project 下 render,风险面同 Section 1 的 root

### `tools/external/claude_video.py` — **已部分实现 user isolation**(历史债)

- 路径构造: `projects/users/<user_openid>/<project_id>/{artifacts,assets,renders}/` (line 107, 357, 392–400)
- 用户归属: 使用 raw `user_openid` (微信 openid) 作为 namespace,**未走 HMAC namespace_key**
- 隔离风险: **MEDIUM**
  - 与 Phase C 的 v2 plan (`docs/user-isolation-via-mcp-session.md:121–127` `projects/users/<HMAC(uid)>/...`) 不一致:用了 user_id 原文而非 HMAC,意味着:
    - openid 直接进路径,如果 openid 字符里出现 `..`/分隔符(目前 sanitize 后应该不会)就会爆
    - rotate secret 时不能保留旧 namespace_key —— 与 doc §Phase D line 153 的 "namespace version + key stored on session/job creation" 不兼容
  - Phase C 必须改造为 `projects/users/<namespace_key>/...`,并把 `user_openid` 改名 `principal_id` 后通过 `Principal` 路径。
- 写入语义: `mkdir(parents=True)` + `shutil.copyfile` 拷贝 (line 31, 392)

### `tweak_server/app.py:316` — tweak form 输出 mp4

- 路径构造: `PROJECTS_DIR / project_id / "renders" / f"tweak-<timestamp>.mp4"` (line 318)
- 用户归属: **未携带 user_id**;沿用 tweak_server 的共享 token
- 隔离风险: **HIGH** —— 同 Section 1,这是 sidecar 旁路

---

## Section 4: 临时/索引文件 (.uploads / .identity_sessions / global job index)

### `projects/.mcp_sessions/` — 跨切关注点

| 子路径 | 写入者 | 读出者 | 用户归属 | 风险 |
|---|---|---|---|---|
| `<digest>.json` | `workbuddy_session.register_image / begin_render / update` | `workbuddy_session._read`, `find_session_by_job_id`, `recover_orphans_and_rebuild_index` | session_hash (无 user) | **MEDIUM** |
| `.job_index.json` | `_index_upsert` (line 172–185) | `find_session_by_job_id`, `update_session_by_job_id`, `mcp_server._drain_queued_jobs`, `mcp_health_monitor` 全局扫描 | 全进程共享,**无 user 过滤** | **HIGH** |
| `.render_jobs.json` | `render_queue.save_job_record` | `render_queue.load_job_record`, `all_job_records`, `_drain_queued_jobs` | 全进程共享,**无 user 过滤** | **MEDIUM** |
| `.locks/<digest>.lock` | `_flock_for` | 同上 | session-scoped | **LOW** |
| `principals.db` | `principal_registry.bind` | `principal_registry.lookup/require` | **唯一带 principal_id/namespace_key 的索引** | **LOW** (待 Phase C 真正使用) |

### `projects/.uploads/` — chunked upload scratch

- 写入者: `asset_upload_chunk.py` (`start` 时建 `.json` + 空 `.part`,`complete` 后两个一起 unlink;line 137, 185–186, 233)
- 读出者: 同文件 (`append`/`complete`)
- 用户归属: **未携带 user_id**;仅按 upload_id(uuid4) 索引
- 隔离风险: **HIGH** —— 任何拿到 upload_id 的会话可继续;无 TTL/cleanup 机制(stale 文件不会被 sweep)
- Phase C 需要: (a) 路径改为 `projects/users/<namespace_key>/.uploads/` 或 (b) 在 upload 状态里写 namespace_key,complete 时校验

### `projects/.users/users.sqlite3` — Web/BFF OAuth + 用户身份

- 写入者: `lib/user_auth.py` 的 `UserAuthStore` (line 40–234)
- 读出者: `lib/web_auth_app.py` (`build_web_routes`)
- 用户归属: **使用 raw `users.id` (= `u_<sha256(provider:subject)[:24]>`) 作为 namespace key** (line 36–37, 176–202)
- 隔离风险: **MEDIUM**
  - 已实现 "users/<user_id>/<project_id>/" 的物理隔离 (line 177),但与 Phase C v2 的 `namespace_key = HMAC(...)` 命名不一致 (见 doc line 166) —— rotate key 时无法保留旧 namespace_key。
  - 与 `lib/principal_registry` **不互通**:web 上来的 user 在 `principal_registry` 没有对应 binding;反过来 MCP-only session 也不会在 `users.sqlite3` 出现。两套身份体系需要 Phase C 统一。
- 写入语义: SQLite + WAL (line 54);HTTP cookie session (line 79–84) 7 天 TTL

### `projects/_share_expiry/index.jsonl` — Weiyun 分享过期索引

- 写入者: `tools/publishers/weiyun_share_link.py:_append_expiry_entry` (line 49–77)
- 读出者: `tools/publishers/weiyun_expiry_sweep.py:_iter_rows / sweep` (line 49–106)
- 用户归属: 行内含 `project_id` 字段(line 70)但 **没有 user_id**;`sweep` 支持按 `project_id` 过滤(line 97),不按 user 过滤
- 隔离风险: **MEDIUM**
  - 单条 entry 公开给所有可读该 JSONL 的人(目前只有 sweeper + share_link 本身);本身不含敏感数据(file_ids / pdir_keys / short_url),泄漏面小。
  - Phase C 建议:每行写入 `namespace_key`,sweep 支持 `--namespace-key` 过滤。

### `projects/<project_id>/events.jsonl` — 工具事件流

- 写入者: `lib/events.py:emit_event` (line 77–95),由 `tools/base_tool.py` 的 BaseTool 在每次工具执行后调用
- 读出者: `lib/events.py:read_events` + Backlot board
- 用户归属: **未携带 user_id**;event 行内字段也不带 principal 信息
- 隔离风险: **MEDIUM**
  - `infer_project_dir` (line 46–74) 从 tool input 的 `project_dir/output_dir/...` 推断出 project,然后写 `<project_dir>/events.jsonl`。
  - 跨用户路径:`project_id` 不同则 events.jsonl 不同,所以"路径层"安全,但 Backlot board 把所有 project 列在一起 (line 287–296),若 Backlot 端做列目录(见 backlot/state.py 第 3 段),可能跨 user 看到别人的 project 名。

### `projects/.identity_sessions/` — **不存在**

- 全仓库搜索 `projects/.identity_sessions` 和 `.identity_sessions`:**0 命中**
- 此目录是用户问题描述里假设的;实际上 Phase B 走的是 `projects/.mcp_sessions/principals.db`,命名已经收敛。

---

## Section 5: 跨切关注点

### 5.1 路径白名单与 namespace 校验

- `tools/asset/read_session_asset.py:_validate_relative` (line 60–78) 只校验"在 `projects/` 下",**不校验"在 `current_principal().namespace_key` 子树"** —— v2 doc §Phase C line 142 已点名要求加固。
- `mcp_server.py:1762` `root = _PROJECT_ROOT / "projects" / project` 是 **当前唯一进入 user-data 区的入口**,但完全不做 principal 校验。
- `tweak_server/` 全程没有 principal 概念,只靠共享 `TWEAK_SERVER_BEARER` (global secret),Phase C 必须把它接进 `PrincipalResolver` 或单独声明"内部受信旁路"。
- `lib/checkpoint.py` 把 `pipeline_dir` 当参数 (line 339, 473 等),生产默认走 `PROJECTS_DIR`;`pipeline_dir` 可以被外部覆盖,需要 Phase C 锁定成 `ProjectWorkspace.root`。

### 5.2 状态文件 per-user 隔离

- `projects/.mcp_sessions/` 是 **全进程共享** 的目录:
  - `<digest>.json` 按 session 隔离,但 directory 不是 per-user(同一 user 多次 session 会落同一目录)
  - `.job_index.json` / `.render_jobs.json` 是 **跨 user 共享** 的全局表
  - `.locks/` 按 digest 隔离
- `projects/.uploads/` 全局共享,按 upload_id 索引,**无 user 隔离**(见 Section 4 HIGH)
- `projects/.users/users.sqlite3` per-file 已经是 web 路径的 per-user 隔离,但 **未与 `principal_registry` 互通**
- Phase C 必须决定:每个 principal 一个 `.mcp_sessions/users/<namespace_key>/` 子目录 + 一份独立 `.job_index.json`?还是保留全局 index 但 key 含 namespace_key?

### 5.3 SSE / 进度通知 / job 索引的过滤

- `lib/render_progress.py:publish/subscribe` (line 28–61) **完全没有 principal 概念**;通过 `job_id` 订阅者拿到所有事件。
- `mcp_server.render_progress_sse` (line 2934–2995):只校验 `job_id` 存在性 (`find_session_by_job_id`),**不校验 `job_id` 属于当前 principal**。
- `tools/mcp_health_monitor._candidate_index_paths` (line 463–502):直接读 `.job_index.json` 找 sentinel job,**不做 user 过滤**;但这是只读 monitor,不是 user-facing API,风险等级 **MEDIUM**。

### 5.4 跨用户访问风险汇总 (从代码静态分析)

| 入口 | 谁触发 | 风险等级 | 原因 |
|---|---|---|---|
| `upload_asset` / `upload_asset_chunk` | MCP tool | **HIGH** | 同 `project_id` 落到同目录,仅靠 sha-dedup 阻挡覆盖 |
| `create_remotion_video_share` | MCP tool | **HIGH** | `state["project_id"]` 直用,无 principal 校验 |
| `read_session_asset` | MCP tool / BFF | **MEDIUM** | 路径在 `projects/` 下即放行,未校验 namespace |
| `tweak_server/*` | Sidecar HTTP | **HIGH** | 共享 token,无 principal 概念 |
| `_run_tool_sync(asset_upload)` | MCP internal | inherits caller | 同 upload_asset |
| `subscribe(job_id)` (SSE) | MCP SSE | **MEDIUM** | job_id 持有者 = 订阅者,无 principal 校验 |
| `mcp_health_monitor._discover_published_sentinel` | Operator cron | **MEDIUM** | 读全局 job_index,跨 user;非 user-facing |
| `weiyun_share_link._append_expiry_entry` | MCP tool | **LOW** | 行内无敏感数据 |
| `lib/events.emit_event` | Tool instrumentation | **MEDIUM** | 写 `<project_dir>/events.jsonl`,project_dir 推断可能跨 user |
| `register_image / begin_render` (workbuddy_session) | MCP tool | **MEDIUM** | session_hash 命名,无 principal;同一 user 多 session 散在同目录 |

### 5.5 Web/BFF API 路径泄露

- `lib/web_auth_app.py:projects` (line 84–93):返回 `store.list_projects(user["id"])` —— **已 per-user 过滤**,**LOW**
- `lib/web_auth_app.py:project_detail` (line 95–113):返回 `assets/renders` 列表,**已 per-user 过滤**,**LOW**
- `lib/web_auth_app.py:upload_asset` (line 115–124):走 `store.save_asset(user["id"], project_id, ...)` —— **已 per-user**,**LOW**
- `tweak_server/app.py` 路径参数全是 raw `project_id`,**没有 user filter**,**HIGH**
- `backlot/state.py:_resolve_asset_path` (line 271–296):尝试 4 种 raw_path 形式(绝对 / project-relative / repo-relative / project-prefixed-relative),line 287–289 显式处理 `parts[0] == "projects"` —— 这条逻辑在 Phase C 后会"正确性改变",因为路径变成 `projects/users/<namespace_key>/...` 后,`parts[0] == "projects"` 仍 true,但后续 prefix 要随 namespace_key 重写。

### 5.6 Phase B 已就绪但未使用的入口

| API | 文件 | 调用者 | Phase C 应做的改造 |
|---|---|---|---|
| `current_principal()` | `mcp_server.py:242` | 仅 `mcp_server._user_id_ctx` 体系内部引用 | 必须被 `create_remotion_video_share / asset_upload* / read_session_asset` 调用来构建 `ProjectWorkspace` |
| `Principal.namespace_key` | `lib/principal_registry.py:161` | 仅 `compute_namespace_key` 测试 | 必须进 `ProjectWorkspace.root` 路径 |
| `sanitize_principal_id` | `lib/principal_sanitize.py:74` | `mcp_server._sanitize_vclaw_user_id` 别名 | Phase C 在 ProjectWorkspace 里复用 |
| `lib/paths.PROJECTS_DIR` | `lib/paths.py:17` | 多处 import | 应该演化成 `ProjectWorkspace.root`,旧 `PROJECTS_DIR` 仅作基线/legacy 测试用 |

---

## Appendix: 文件计数

| 类别 | 数量 | 说明 |
|---|---|---|
| 生产代码文件 (`.py`,非 test/scripts/示例) 引用 `projects/` 字面量 | **25** | 见下方列表 |
| 测试文件引用 `projects/` | **17** | tests/integration/test_principal_registry.py, tests/test_*.py 等,均为 fixture,不计入风险 |
| 写入 `projects/` 树的文件 (含覆盖 + append) | **14** | workbuddy_session, render_queue, asset_upload*, asset_upload_chunk, checkpoint, mcp_server, tweak_server/assets, tweak_server/app, weiyun_share_link, events, principal_registry, user_auth, claude_video, mcp_health_monitor(write scan log) |
| 读取 `projects/` 树的文件 | **18** | 上面的 14 个 + read_session_asset*, read_session_asset_image, backlot/state, tools/decompose_health_monitor, lib/corpus |
| 含路径拼接(可能引入 user-controlled component) | **9** | mcp_server.py:1762, asset_upload.py:114–162, asset_upload_chunk.py:91, 169, workbuddy_session.py:23, 53, render_queue.py:50, tweak_server/assets.py:51, tweak_server/app.py:152, claude_video.py:107 |
| 含 namespace/principal 白名单校验的文件 | **1** | `tools/asset/read_session_asset.py` (仅 projects/-level,缺 namespace_key 校验) |
| 配置文件(`.yaml`/`.toml`/`.env`)指向 `projects/` 路径 | **0** | `config.yaml` 的 `paths.*` 都是 `pipeline/library/styles/skills/output`,与 `projects/` 无关;`OPENMONTAGE_PROJECTS_DIR` 是进程 env,可在 .env 设置但未在 .env.example 出现 |

### 生产代码命中文件清单 (25 个)

```
backlot/state.py                       (line 287–296 path-prefix normalization)
frameflow/bff/frameflow_e2e.py         (line 274 --output-root default "projects")
lib/corpus.py                          (line 78 demo fixture)
lib/events.py                          (line 22 PROJECTS_DIR; write events.jsonl)
lib/paths.py                           (line 17 PROJECTS_DIR env override)
lib/principal_registry.py              (line 83 STATE_DIR = projects/.mcp_sessions)
lib/render_queue.py                    (line 50 STATE_DIR; .render_jobs.json)
lib/user_auth.py                       (line 238 projects/.users/users.sqlite3)
lib/web_auth_app.py                   (line 88–113 /web/api/projects handler)
lib/workbuddy_session.py               (line 23 STATE_DIR; full session bookkeeping)
mcp_server.py                          (line 1762 root = projects/project; SSE)
render_demo.py                         (line 26 projects/demos/renders, demo only)
tools/analysis/frame_sampler.py        (line 40–43 _WORKSPACE_PROJECT_ROOT guard)
tools/analysis/video_analyzer.py       (line 168 hardcoded Path("projects/_analysis"))
tools/asset/read_session_asset.py      (full path whitelist validator)
tools/asset/read_session_asset_image.py (delegates to above)
tools/asset_upload.py                  (full upload writer)
tools/asset_upload_chunk.py            (full chunked upload writer)
tools/character/character_animation.py (line 521 default output_path)
tools/decompose_health_monitor.py      (line 56 PROJECTS_DIR; workspace violation probe)
tools/external/claude_video.py         (uses raw openid namespace; v2-incompatible)
tools/mcp_health_monitor.py            (line 492 candidate job_index paths)
tools/publishers/weiyun_expiry_sweep.py (line 45 projects/_share_expiry)
tools/publishers/weiyun_share_link.py  (line 45 projects/_share_expiry)
tweak_server/app.py                    (multiple PROJECTS_DIR paths)
tweak_server/assets.py                 (PROJECTS_DIR + subdir whitelist)
```

### .md 契约段命中 (实现细节,非叙述)

| 文件 | 行 | 契约段摘要 |
|---|---|---|
| `docs/user-isolation-via-mcp-session.md` | 119–127 | v2 layout:`projects/users/<user_namespace_key>/<project_id>/...`,`services/`,`_system/principal_registry.sqlite` |
| 同上 | 129–141 | Phase C audit checklist(命中 14 个文件,本报告全部覆盖) |
| 同上 | 142 | `read_session_asset` 必须独立校验 namespace 内 |
| `docs/user-data-isolation-analysis.md` | 26–32 | 现状(legacy)路径表:`.mcp_sessions/<digest>.json`,`assets/_sessions/<digest>/`,`renders/`,`.job_index.json` |
| `docs/bugs/render-job-registry-bug-om-side-audit-2026-08-31.md` | 110–114, 152, 238–243 | 渲染任务索引存储位置和诊断方法 |
| `docs/claude-video-integration.md` | 22, 40–50, 101–115, 124, 202, 224 | `projects/users/<user_openid>/...` 约定(与 v2 HMAC 不一致) |
| `docs/web-multiuser-auth.md` | 35 | `projects/users/<user_id>/` + `projects/.users/users.sqlite3` |
| `docs/plans/workbuddy-session-remotion-share.md` | 141–142 | session state 路径约定 |

### .json/.yaml/.toml 配置命中

- **0 条** 配置文件硬编码 `projects/` 路径
- 唯一与 `projects/` 相关的 env override 是 `OPENMONTAGE_PROJECTS_DIR`(lib/paths.py:17),目前在 `.env.example` 未出现(grep `OPENMONTAGE_PROJECTS_DIR` 在 .env.example 0 命中)
- `.mcp.json` / `integration/voicebox.mcp.json` / `integration/claude-video.mcp.json` 描述 MCP server 命令,不指向 `projects/`

---

## 风险总结

- **HIGH 风险点: 6 个**
  1. `tools/asset_upload.py` 写入 `projects/<id>/assets/_sessions/<digest>/` 无 user 隔离
  2. `tools/asset_upload_chunk.py` 写入 `projects/.uploads/` 完全 shared
  3. `mcp_server.py:1762` `create_remotion_video_share` 读 `projects/<id>/` 无 principal 校验
  4. `tweak_server/app.py` + `tweak_server/assets.py` 旁路共享 token 路径
  5. `lib/workbuddy_session.py:.job_index.json` 全进程共享,`render_progress` 订阅跨 user 可达
  6. `tools/external/claude_video.py` 已实现 user 隔离但用 raw openid,与 v2 HMAC plan 不兼容(必须改造)

- **MEDIUM 风险点: 8 个**
  1. `tools/asset/read_session_asset.py` 路径白名单缺 namespace_key 校验
  2. `lib/workbuddy_session.py:<digest>.json` session-scoped 而非 principal-scoped(目录级安全但语义错)
  3. `lib/render_queue.py:.render_jobs.json` 全局共享,owner_id 是 session_hash
  4. `lib/events.py:emit_event` 按 project_dir 写,无 user 字段
  5. `lib/checkpoint.py` pipeline_dir 可被覆盖;生产默认走 `PROJECTS_DIR`,无 principal 校验
  6. `tools/mcp_health_monitor.py` 读全局 `.job_index.json` 找 sentinel
  7. `lib/user_auth.py` 的 `users/<user_id>/` 命名与 v2 `HMAC(namespace_key)` 不一致,且与 `principal_registry` 不互通
  8. `tools/analysis/video_analyzer.py:168` `Path("projects/_analysis")` hardcoded,无 user 归属

- **LOW 风险点: 4 个**
  1. `tools/character/character_animation.py:521` 硬编码 `projects/character-preview/preview.html`(demo default)
  2. `lib/render_progress.py` 不写盘,只 in-memory pub/sub,SSE 风险已在 Section 5.3 列入 MEDIUM
  3. `tools/publishers/weiyun_share_link.py:45` `_share_expiry/index.jsonl` 行内无敏感数据
  4. `lib/principal_registry.py` 唯一带 namespace_key 的索引,但还没被业务代码用来约束路径

---

## Phase C 抽象建议(仅建议,不实现)

1. **`ProjectWorkspace`** dataclass(文档 §Phase C line 104–115 已规定)
   - 输入:`Principal` + `project_id`(已 sanitize)
   - 暴露:`root / assets / artifacts / renders / checkpoints / session_state / upload_state`
   - 强制:路径下任何文件写入必须先通过 `_validate_inside(root)`
2. **`PrincipalResolver`**(文档 §Phase C line 96–103 隐含)
   - 入口:`PrincipalResolver.current()` 返回 `Principal | raise PrincipalNotFound`
   - 实现:优先 ContextVar(同 middleware 路径),fallback `principal_registry.require(sid)`
   - 在 FastMCP tool background task 必须能拿到(`current_principal()` 已就绪,见 mcp_server.py:242)
3. **路径下推点**:所有 6 个 HIGH 命中点改成 `workspace = ProjectWorkspace.for_current_principal(project_id)`,不再用 `PROJECTS_DIR / project_id`。
4. **`.mcp_sessions` 重构**:保留 `principals.db` 在根(`projects/.mcp_sessions/principals.db` 系统级),把 `<digest>.json` / `.job_index.json` / `.render_jobs.json` / `.locks/` 全部移进 `projects/users/<namespace_key>/.mcp_sessions/`(或 `services/`) —— 这是 Section 4 HIGH 风险的核心缓解。
5. **`.uploads` 重构**:跟随 `PrincipalResolver`,成为 `ProjectWorkspace.upload_state` 的一部分;upload_id 命名空间仍然是 uuid4,但路径前缀变为 per-principal。
6. **`tweak_server` 决策**:要么接进 `PrincipalResolver`(让 `TWEAK_SERVER_BEARER` 仅作 transport auth,principal 仍走 mcp session),要么在 Phase D 文档里明确 "tweak_server 是内部受信旁路,不参与 user isolation"。

---

## 主线程下一步拍板点

1. **`ProjectWorkspace` 命名**:文档说 `users/<namespace_key>/`,与现有 `tools/external/claude_video.py` 的 `users/<user_openid>/` **冲突**。需要拍板:Phase C 改造时是 (a) 一次性把 claude_video 也迁移到 HMAC namespace,还是 (b) 双轨共存接受不一致?
2. **`tweak_server` 的命运**:作为独立的 sidecar(共享 token),Phase C 是否纳入 `PrincipalResolver` 改造范围?(技术上可行,但工作量大;不改造就是承认一个已知 risk accepted)
3. **`.mcp_sessions/` 物理迁移**:每个 principal 一份 `<digest>.json` / `.job_index.json`,还是全局共享但 key 含 namespace_key?(前者干净但破坏 Phase D 的 rollback 简化;后者向前兼容但读路径要重写)
4. **Backlot board 路径层兼容**:`backlot/state.py:288` 的 `parts[0] == "projects"` 判断在 Phase C 后仍然 true,但 `parts[2]` 不再是 `<project_id>` 而是 `<user_namespace_key>/<project_id>`,需要 Backlot 配合。
5. **`config.yaml` 是否要新增 `paths.projects`**:目前配置里 `paths.*` 不含 `projects`,Phase C 引入 `ProjectWorkspace` 后是否需要让 staging / production 切环境时方便地改 root?(参考 `OPENMONTAGE_PROJECTS_DIR` 的现状)
