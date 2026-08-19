# 渲染任务 @babel/standalone 缺失 Bug 检查 / 处理 / 复检报告

- 报告日期：2026-08-19
- 涉及组件：`frameflow/bff`（本机 A：ocdev，:8080）→ `lanes.ymxt.top:8900/mcp`（机器 B：MCP 上传 + Remotion 渲染，仅 IPv6）
- 结论：**渲染后端（机器 B）的 `remotion-composer/node_modules` 缺少 `@babel/standalone`，导致所有 Remotion 渲染在打包阶段失败；该依赖由 2026-08-18 提交 `13f7dca` 引入，机器 B 未执行 `npm install` 刷新依赖。本机 A 无机器 B 的管理通道（无 SSH / 无 shell 类 MCP 工具），无法从本机直接修复，需机器 B 运维执行一行 `npm install`。**

---

## 1. 异常现象

用户从 Web（`render.mengxa.com`）提交图片 + 自定义脚本，BFF 按环境变量
（`MCP_BASE_URL=http://lanes.ymxt.top:8900/mcp`）把渲染任务转发到上游 MCP（机器 B）。
异常表现为：**渲染任务全部失败（「失败」状态），无成片产出。**

证据：

- BFF 系统日志（journalctl -u frameflow-bff）出现：
  ```
  [bff-mcp] tool_error tool=get_render_status ... error="Remotion render failed for
  renderer_family='animation-first'. Underlying error: Remotion render failed (exit 1):
  Bundling ... Error: Module not found: Error: Can't resolve '@babel/standalone'
  in '/opt/OpenMontage/remotion-composer/src' ..."
  ```
- BFF SQLite `render_jobs` 状态（`frameflow/bff/data/frameflow.db`）：
  - 最近 3 个任务（2026-08-18 18:07 / 18:19 / 18:22）→ 全部「失败」
  - 更早的任务（2026-08-17）有成功有失败（混有上传配额等其它历史问题）
- 直接查询机器 B 上游 MCP（`get_render_status(render_job_id=f9bbaedf...)`）→ 仍为
  `failed`，错误同 @babel/standalone，`updated_at=2026-08-18T10:07:43Z`。

## 2. 系统现状（本机 A，诊断时点）

| 项目 | 状态 |
|------|------|
| `frameflow-bff.service` | active (running)，主进程 940464，:8080 |
| BFF 启动日志 | `[mcp] endpoint=http://lanes.ymxt.top:8900/mcp progress_endpoint=http://lanes.ymxt.top:8900/render-progress` |
| BFF 二进制 | 2026-08-18 17:59 构建，比全部 `*.go` 源文件新（无需重建） |
| `/api/me` | 200（AUTH_REQUIRED=true 生效） |
| nginx `render.mengxa.com` | 代理 `/api/` → `127.0.0.1:8080`，配置正常 |
| 上游 MCP（机器 B） | TCP/IPv6 可达；`initialize`/`tools/list`/`get_render_status` 快速返回 |
| 上游 MCP 渲染 | **`@babel/standalone` 缺失，所有 Remotion 渲染失败** |

BFF 侧的转发本身**工作正常**：上传（upload_asset_chunk）、轮询（get_render_status）
都成功返回，失败发生在机器 B 的 Remotion 打包阶段。

## 3. 根因分析

`remotion-composer/src/CustomComposition.tsx` 第 3 行静态导入：

```ts
import * as Babel from "@babel/standalone";
```

`remotion-composer/package.json` 也声明了依赖：

```json
"@babel/standalone": "^8.0.4"
```

该依赖由 2026-08-18 10:09 提交 `13f7dca`（支持自定义合成脚本「脚本模式」真实渲染）引入。
由于 Remotion 打包器会对**入口引用的整棵静态依赖树**做 webpack resolve，
因此**任何** Remotion 渲染（无论模板还是自定义脚本）都会尝试解析 `@babel/standalone`。

机器 B 的 `remotion-composer/node_modules` 是在该依赖加入**之前**安装的快照，
缺少 `@babel/standalone`（错误栈显示 `@remotion/bundler` 已存在、唯 `@babel/standalone`
缺失），于是所有渲染在 Bundling 阶段即失败。机器 B 没有执行 `npm install` 刷新依赖。

### 时间线

| 时间 | 事件 |
|------|------|
| 2026-08-18 10:09 | 提交 `13f7dca`：package.json 加入 `@babel/standalone`，CustomComposition 静态导入 |
| 2026-08-18 18:07–18:22 | 机器 B 上渲染任务 f9bbaedf / cc8b73a2 / 035f1b3e 全部失败（@babel/standalone） |
| 2026-08-18 21:57 | 提交 `00836bc`：自定义脚本红屏修复（与 @babel 解析无关） |
| 2026-08-19（现在） | 机器 B 状态未变，渲染仍失败 |

## 4. 修复尝试（三次）

### 尝试一：日志定位 + 根因确认（成功，只读）

- 查看 `journalctl -u frameflow-bff`，捕获 `Can't resolve '@babel/standalone'` 错误。
- 用 `om_mcp_probe.py call get_render_status` 直连机器 B，确认真实生产任务失败状态。
- 核查 `package.json` / `CustomComposition.tsx` / `package-lock.json`（锁定版本 8.0.4）。
- 核查 BFF 配置（systemd + .env 均指向 lanes.ymxt.top:8900），确认转发路径正确。

### 尝试二：从本机 A 远程修复机器 B（失败）

机器 B（`240e:3b0:c49:f726:35e0:c08c:c3a5:88d8`，IPv6-only）可探测的开放端口：
- `8900`（MCP，Streamable-HTTP，Bearer 鉴权）
- `3000`（Next.js 前端，非管理端）
- `22/SSH` → **连接被拒**；其余常见管理端口均未开放。

MCP 工具清单（23 个）中无任何 shell / npm install / 命令执行类工具；
`execute_tool` 只能执行注册的流水线工具（`video_compose` 等），不能写 shell。
`/web/*` 与 `/render-progress` 均为业务接口，无管理能力。
→ **本机 A 不存在机器 B 的文件系统管理通道，无法执行 `npm install`。**

### 尝试三：绕行 / 恢复方案评估（失败）

- 无替代渲染后端（BFF 上游唯一指向 lanes.ymxt.top:8900）。
- 无法在 BFF 侧规避打包器对 `@babel/standalone` 的解析（每次 Remotion 渲染都会打包 CustomComposition）。
- FFmpeg 降级渲染被治理规则禁止（需用户显式批准，且改变交付物性质），不是修复。

## 5. 修复办法（需机器 B 运维执行）

在机器 B（`lanes.ymxt.top`，MCP/Remotion 宿主）执行：

```bash
cd /opt/OpenMontage
git pull --ff-only origin main          # 若代码落后，先对齐（含 13f7dca 及之后提交）
cd remotion-composer
npm install                              # 安装 @babel/standalone@8.0.4 及其它缺失依赖
# 重启 MCP 服务（服务名按机器 B 实际 systemd 单元，例如 openmontage-mcp.service）
sudo systemctl restart <MCP服务名>
sudo systemctl status <MCP服务名> --no-pager -l
```

复检（在机器 B 或通过 MCP）：

```bash
# 1) Remotion 可用性（应报告 remotion available）
cd /opt/OpenMontage && python -c "
from tools.tool_registry import registry; registry.discover()
print(registry.get('video_compose').get_info().get('render_engines'))"

# 2) 在机器 B 执行一次最小渲染冒烟
cd /opt/OpenMontage && python om_mcp_probe.py upload <小图.png> -p smoke-recheck
# 然后调用 create_remotion_video_share -> 轮询 get_render_status，直至 published
```

恢复后，BFF 侧对失败任务的处置方式：
- 用户重新提交即可（BFF 队列会创建新 render_job_id）。
- 如需对历史失败任务复推，可用 `/api/render-queue/:jobId/republish` 或直接让用户重试。

## 6. 关联观察（次要，待机器 B 排查）

上传到机器 B 的延迟**不稳定**：
- BFF 在 2026-08-18 18:01 的 upload_asset_chunk 约 **85–102ms**（正常）；
- 2026-08-18 18:31 同一工具一次 start 操作耗时约 **93s**；
- 本次诊断中 `upload_asset`（整包上传，非分块）对**合法参数**的调用 >90s 无响应，
  甚至偶发连接被重置；而对**非法参数**的调用立即返回校验错误。

说明机器 B 的 MCP 服务对合法上传请求偶发长时间阻塞（疑似线程池/锁/磁盘/负载抖动），
与文档 `frameflow/REMOTE_OBSERVABILITY_HANDOFF.md` 提到的"非回环地址上传卡顿"模式吻合。
建议机器 B 运维在修复 @babel 后，用同一会话连续小文件上传复测；仍卡则采集 MCP
同时段日志定位上传处理函数，不要归因为"性能不足"。

## 7. 本机 A 可执行事项（已完成 / 建议）

- [x] BFF 二进制与配置均为最新，无异常，**无需重启**。
- [x] 监控脚本 `om_mcp_probe.py status` 可用；本机为双机部署中的 BFF 机，
  建议以 `--role bff --target http://lanes.ymxt.top:8900/mcp` 运行（勿用默认 `--role all`，
  否则会把"本机 8900 未监听"误报为故障——8900 在机器 B 上）。
- [ ] 机器 B 修复后，建议在本机用 `om_mcp_probe.py` 对上游做一次端到端冒烟。

## 8. 附录：证据摘录

```text
# BFF journal（2026-08-18 18:07:43，脱敏）
[bff-mcp] tool_error tool=get_render_status ...
error="Remotion render failed for renderer_family='animation-first'. Underlying error:
Remotion render failed (exit 1): Bundling ... Error: Module not found:
Error: Can't resolve '@babel/standalone' in '/opt/OpenMontage/remotion-composer/src' ...
  /opt/OpenMontage/remotion-composer/node_modules/@babel/standalone doesn't exist"

# BFF 启动行（确认转发端点）
2026/08/18 17:59:12 [mcp] endpoint=http://lanes.ymxt.top:8900/mcp progress_endpoint=http://lanes.ymxt.top:8900/render-progress

# 上游 MCP 实时查询（2026-08-19）
get_render_status(f9bbaedf08ed416c811bfb9fd270ead0) => status=failed,
error=Remotion render failed ... Can't resolve '@babel/standalone' ...
updated_at=2026-08-18T10:07:43.286541+00:00

# render_jobs 状态（SQLite，最近行）
失败 | 2026-08-18T18:22 | 035f1b3e
失败 | 2026-08-18T18:19 | cc8b73a2
失败 | 2026-08-18T18:07 | f9bbaedf

# 依赖声明
remotion-composer/package.json: "@babel/standalone": "^8.0.4"
remotion-composer/package-lock.json: node_modules/@babel/standalone: 8.0.4
remotion-composer/src/CustomComposition.tsx:3: import * as Babel from "@babel/standalone";
```
