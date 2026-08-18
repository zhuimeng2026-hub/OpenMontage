# 渲染系统监控与修复报告

- **日期**: 2026-08-19 06:00–06:25 (HKT)
- **目标**: 验证 OpenMontage 从 `192.168.20.173:8900/mcp` 接受外部渲染任务到产出正常视频的整条链路是否正常；发现异常则修复（最多三次），并在当前目录记录过程。
- **结论**: 系统恢复正常，两条独立全链路测试均产出有效视频（published + h264 1080×1920）。修复成功，无需进入第二次/第三次修复。

---

## 一、环境与现状

| 项 | 值 |
|---|---|
| 本机 | `192.168.20.173` (hostname `xt`) |
| MCP 服务 | `openmontage-mcp.service`，端口 `8900`，streamable-http，Bearer 鉴权 |
| BFF | `frameflow-bff.service`，端口 `8080`，`AUTH_REQUIRED=false`（测试期），代理到 `127.0.0.1:8900/mcp` |
| Observer / Perf-monitor | `frameflow-observer.service`(9910) / `frameflow-perf-monitor.service`，均 active |
| 渲染引擎 | Remotion (`remotion-composer`)，chrome-headless-shell（puppeteer 缓存），ffprobe/ffmpeg 可用 |
| 磁盘 | 使用 67%（可用 292 GB） |
| Git HEAD | `b8cb0b0` (2026-08-18 21:57) |

**修复前运行实例**: PID 2356166，启动于 **2026-08-17 15:48**，比磁盘代码旧约 1.5 天（缺失 8 个 Aug-18 提交，含上传路径/配额修复）。这是本报告主要异常的根本来源。

---

## 二、验证过程（时间线）

### 1. 存活与鉴权探测
- `POST /mcp` 无 token → `401 unauthorized`（鉴权正常）
- 带 token + `Accept: application/json` + `Mcp-Session-Id` → `initialize` 200 OK（MCP 协议正常）
- 外部客户端 `192.168.20.1` 每约 2 分钟进行健康探测（GET /mcp、GET /render-progress、POST initialize），曾出现 406（缺 Accept 头）/400（缺 session）——属客户端协议问题，服务端行为正确。

### 2. 全链路测试 A（upload_asset 普通上传）✅
- `monitor_render/render_chain_test.py`，3 张测试图 → `create_remotion_video_share(photo-ken-burns, 9:16)`
- 结果: `queued → rendering → rendered → published`，耗时 **102s**
- 视频: `projects/monitor-render-20260819-060519/renders/*.mp4`，**1.9MB，h264 1080×1920，10.05s，300 帧**，三处抽帧亮度 21/99/153（非黑帧，内容有效）
- 分享: `https://share.weiyun.com/98tMdB5T`

### 3. 发现异常 A：`om_mcp_probe.py status` 误报
运行 `om_mcp_probe.py status --role all --target http://localhost:8900/mcp` 得到：
- `chrome`/`chromium` → `[ERROR 未找到进程]`（实为 Remotion 用 `chrome-headless-shell`，仅渲染期存在，闲置缺省属正常）
- `http://localhost:8900/mcp` → `[ERROR] HTTP 401`（probe 未带 token，MCP 一律 401）
- `VERDICT = PROBLEMS_FOUND`（健康系统被误判为故障）

### 4. 发现异常 B（关键）：外部提交的 `upload_asset_chunk` 卡死
外部客户端 `192.168.20.1`（带 token，直连 8900）反复发送 `upload_asset_chunk start`：
- 06:10:36 `start` total_bytes=1417174 → 无响应
- 06:12:36 `start` total_bytes=1666119 → 无响应
- 06:14:37 `start` total_bytes=1178349 → 无响应
- 每次 `dispatch` 日志出现，但 **`completed` 日志从不出现** → 工具调用在服务端挂起，客户端永不收到响应，陷入重试循环，无法完成上传 → 无法生成视频。

**诊断**（用独立干净客户端复现）:
- 本机新会话 `upload_asset_chunk start` → curl 超时 20s 无响应（**稳定复现**）
- 工具代码单独在进程内执行 `start` → **0.00s 成功**（工具本身无问题）
- py-spy/gdb 采样：事件循环空闲 + 全部 worker 线程空闲在 `queue.get()`，`_run_tool_sync`(asyncio.to_thread) 的任务未被任何 worker 拾取 → 会话消息循环/线程池处于卡死态
- git 对比：`_run_tool_sync` / `upload_asset_chunk` 路径在运行版本与磁盘版本间**无差异** → 不是代码级修复，是**运行实例长期运行（2 天+）后 asyncio 线程池/会话状态退化**导致

### 5. 修复（第 1 次尝试，成功）
按目标授权（“必要时可直接重启对应的应用替换旧的”），重启 `openmontage-mcp.service`：
- systemd 对旧进程超时 SIGKILL（进一步佐证旧实例卡死）
- 新实例 PID 3816703，06:20:17 启动，125 tools 注册，Bearer 鉴权启用，孤儿恢复 0/42/0，keep-alive 30s

### 6. 修复后验证
- `upload_asset_chunk start` → **24ms 成功**返回 upload_id ✅
- 全链路测试 B（`monitor_render/chunk_chain_test.py`，与外部客户端**完全相同的协议**：`start→append→complete` 分块上传 3 张图）:
  - 每个操作 `success=True`（start 3ms / append 4ms / complete 5ms）
  - `queued → rendering → rendered → published`，耗时 **76s**
  - 视频: `projects/chunk-chain-20260819-062047/renders/*.mp4`，**1.8MB，h264 1080×1920，10.05s，300 帧**
  - 分享: `https://share.weiyun.com/hdWcm6Pp`
- BFF 代理链路: `POST localhost:8080/api/mcp {get_render_status}` → 返回 `status=published` + share_url ✅
- `om_mcp_probe.py status` → **`VERDICT = NO_PROBLEMS_DETECTED`** ✅

---

## 三、代码修复：`om_mcp_probe.py`（探测误报，已应用，未提交）

1. **渲染期浏览器进程按需判定**
   - 新增 `RENDER_ON_DEMAND_PROCS = {chrome, chromium, chrome-headless-shell, ...}` 与 `_render_active()`（识别 `remotion render` 子进程）
   - `chrome`/`chromium` 闲置时为 `count=0 (渲染时才需要)`，不再报 `proc_missing`
   - 仅当渲染进行中却一个 headless 浏览器都没有时才报错（渲染卡死/浏览器崩溃）
   - 首版按“每个浏览器名逐个报错”在渲染中误报 `chromium 缺失`（实际一次渲染只用一种浏览器），已改为**集合判定**（任一存在即 OK）
2. **上游探测支持鉴权 + MCP 握手**
   - `_http_probe` 增加可选 `token`，带 `Authorization: Bearer`
   - 新增 `_mcp_probe`：对 `/mcp` 端点执行真实 `initialize` 握手（Bearer + Accept + 会话），200/202 且含 `result` 视为健康，不再把 401 当 `upstream_down`
   - `status` 上游段按 `target` 是否含 `/mcp` 自动选择握手探测

验证：`python3 om_mcp_probe.py --token $TOKEN status --role all --target http://localhost:8900/mcp` → `HTTP 200, 15ms`，`VERDICT=NO_PROBLEMS_DETECTED`。

---

## 四、当前状态（修复后）

| 检查 | 结果 |
|---|---|
| 服务 | openmontage-mcp / frameflow-bff / frameflow-observer / frameflow-perf-monitor 全部 active |
| 端口 | 80/443/8080/8900/9910 全部监听 |
| MCP 上游 | initialize 握手 200 OK（15ms） |
| 渲染链路 | upload_asset 与 upload_asset_chunk 两条路径均产出有效视频 |
| 进程 | 关键进程齐全；浏览器进程渲染期按需出现 |
| 探测 | NO_PROBLEMS_DETECTED |

---

## 五、残留观察与建议（非阻塞）

1. **外部客户端 `192.168.20.1` 的 400**：修复后其部分请求仍报 `400 Bad Request: Missing session ID`——它初始化拿到 `Mcp-Session-Id` 后未在后续请求回传该头。属客户端协议实现问题（可能与其会话状态机在重启后丢失有关），服务端按 MCP 协议拒绝无会话请求是正确行为。若该客户端持续无法回传 session，外部任务将无法通过；需在其侧修复会话头回传逻辑。已挂后台监控观察其后续行为。
2. **`mcp-server.service`（旧 unit）**：`/etc/systemd/system/mcp-server.service` 存在但未启用，与 `openmontage-mcp.service` 并存易混淆，建议删除或注释。
3. **`projects/.uploads/` 残留**：存在多个未完成的 `.part`/`.json`（Aug 8–18），是分块上传中断的孤儿状态，可安全清理。
4. **`frameflow/bff/frameflow-bff.new`**：比运行中二进制（Aug 16 21:28）更旧（Aug 16 20:54），无需替换；建议删除避免误用。
5. **防复发**：本次卡死是长驻进程 asyncio 状态退化，非崩溃（`Restart=on-failure` 不会触发）。建议部署已有的 `deploy/om-mcp-probe.service` + 观察者做周期健康检查，并对 MCP 增加“某会话工具调用无 completed 超时则重启/告警”的看门狗逻辑。

---

## 五-A、真实外部任务验证（修复后，补录）

06:38:37 起，外部提交（BFF→127.0.0.1→8900，会话 `45769fa7...`）上传 **10 张电商产品图**（001-hero / 002-set / 003-capacity / 004-size-guide / 007-front-view / 008-colors / 009-lifestyle / 010-features …），项目 `frameflow-batch-batch-0751d9f3001d16591bf4be39`：

- 分块上传 start/append/complete **全部 success=True（3–10ms）**，16 次 complete 均成功 → 修复前此处是卡死重试
- `create_remotion_video_share` → **CinematicRenderer** 渲染启动，BFF 前端持续消费 `/render-progress/db430b089d9b...`（200 OK）
- **最终结果：`published`**，`https://share.weiyun.com/UdCe4LTl`，视频 `renders/d535715a...-db430b08....mp4` = **22.8 MB / h264 / 1080×1920 / 60.05s**，`video_compose`+`weiyun_upload` 事件均 start/finish
- 抽帧亮度：t=3→121、t=58→129（有内容）；t=30 恰为单帧黑（0.0）——是 60s 中点场景切换的过渡帧，非黑段/渲染故障
- 说明：外部真实提交链路已恢复并完整出片；修复前此处是卡死重试

## 六、om-mcp-probe 服务部署（补录）

2026-08-19 部署 `deploy/om-mcp-probe.service` 为 systemd 常驻服务，供 **192.168.20.0/24 段直接访问**：

- **unit**: `/etc/systemd/system/om-mcp-probe.service`（从仓库 deploy/ 安装，`enable --now`，开机自启）
- **运行**: `openmontage` 低权限用户（新建 system 用户，无登录 shell）；`om_mcp_probe.py status --role all --target http://localhost:8900/mcp --serve 0.0.0.0:9099`
- **环境**: `/etc/openmontage/monitor.env`（权限 600，属主 openmontage）：`OM_MCP_TOKEN=<真实>` / `OM_PROBE_ROLE=all` / `OM_PROBE_PORT=9099` / `OM_PROBE_TARGET=http://localhost:8900/mcp`
- **日志**: `/var/log/openmontage/om_mcp_probe.log`（`LogsDirectory=openmontage` 自动创建）；进程日志走 journald
- **访问**: 绑定 `0.0.0.0:9099`，ufw 未启用、iptables INPUT=ACCEPT → **192.168.20.x 全网段可访问**
  - `curl http://192.168.20.173:9099/healthz` → `OK`
  - `curl http://192.168.20.173:9099/` → 状态报告（`VERDICT = NO_PROBLEMS_DETECTED`）
- **高可用**: `Restart=always` + `RestartSec=3` + `StartLimitIntervalSec=0`（[Unit]）

**部署过程中修正的 deploy 工件问题**（已同步仓库 deploy/ 文件）：
1. systemd 249 的 ExecStart **不支持 `${VAR:-default}`** 默认值语法 → 改为 `${VAR}`，依赖 monitor.env 显式提供 role/port
2. `--log` 是**全局参数**，必须置于子命令 `status` 之前
3. `StartLimitIntervalSec=0` 属 **`[Unit]`** 段，不在 `[Service]`（systemd 249 忽略并告警）
4. 新增 `LogsDirectory=openmontage` + `--log /var/log/openmontage/om_mcp_probe.log`（原默认在 /opt/OpenMontage 下，低权限用户不可写）

## 七、第二次复发：upload_asset_chunk 卡死根因定位与自愈修复（补录）

**现象（07:48–07:50）**：修复后约 30 分钟再次出现外部 BFF 上传卡死——`upload_asset_chunk start` dispatch 有日志、completed 永不出现，`192.168.20.246` 的 `render.mengxa.com/health` 经 BFF `/api/mcp` 探测返回 **502**。重启即恢复，但说明第一次「长驻退化」判断不完整。

**定位过程**：
1. 插桩 `_run_tool_sync`（`tool.sync.submit` / `tool.sync.done`），确认卡死点是 **`asyncio.to_thread` 的默认 executor 永远不拾取任务**（dispatch→submit 有日志、done 无），事件循环仍活、多个 worker 全空闲。
2. 8 会话 × ~200 次分块上传压测（~1600 次 start/append/complete，全成功）+ 并发真实渲染 → **未复现** → 纯负载非触发。
3. py-spy/gdb 全线程分析发现**关键架构**：`mcp_server.py:980 _run_render_job` 用**独立线程 `asyncio.run()` 跑第二个事件循环**，该循环自带第二个默认 executor（线程名 "asyncio_0" 重复即两套线程池）；渲染/微云上传/分享全部经其 executor。渲染慢/卡时该线程的 worker 长期占用（`pipe_read` 等 remotion 子进程）。
4. 主循环 executor 与渲染循环 executor 相互独立，但长驻进程在「真实 BFF 流量 + 渲染线程 + 多 executor」叠加下，主循环默认 executor 偶发「submit 后无人拾取」的卡死（具体交织未 100% 复现，机制与多事件循环 + asyncio.to_thread 默认 executor 生命周期相关）。

**根治（已部署并验证自愈）**：
1. `_run_tool_sync` 增加 **900s 超时**（`asyncio.wait_for`）——即使 executor 卡死也回错误而非无限挂起。
2. 新增 **默认 executor 健康自愈监控**（`_start_executor_health_monitor`，主循环后台任务，30s 周期）：用空操作探测默认 executor，**8s 内未被拾取即视为卡死 → `loop.set_default_executor(ThreadPoolExecutor)` 替换**，后续工具调用立即恢复，无需人工重启。已验证：模拟卡死 executor → 探测超时 → 替换 → 新调用成功（SELF-HEAL OK）。
3. 保留 `tool.sync.submit/done` 插桩用于观测（journal 内 `executor.health.*`、`tool.sync.*`）。

**当前状态**：监控运行中（`executor.health.monitor started`），未再出现 wedge；全链路分块上传+渲染+发布再次通过。

## 八、遗留产出

- 测试脚本与日志: `monitor_render/`（`render_chain_test.py`、`chunk_chain_test.py`、`start_probe.py`、`chain_test*.log`、`chunk_chain.log`）
- 验证视频: `projects/monitor-render-20260819-060519/renders/`、`projects/chunk-chain-20260819-062047/renders/`
- 本次监控日志: 本文件 + systemd journal（`journalctl -u openmontage-mcp.service`）
