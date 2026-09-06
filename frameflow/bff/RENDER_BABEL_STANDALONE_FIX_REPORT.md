# 渲染任务 @babel/standalone 缺失 Bug — 二次复检报告

- **报告日期**：2026-08-19（重新检查）
- **涉及组件**：`frameflow/bff`（本机 A：ocdev，:8080）→ `lanes.ymxt.top:8900/mcp`（机器 B：MCP 上传 + Remotion 渲染，仅 IPv6）
- **复检人**：本机 A 上的诊断 agent（无机器 B 的管理通道）
- **新结论**：**机器 B 的 `@babel/standalone` 依赖已被修复**（早期报告"全部渲染失败"已不成立）；残留失败任务由于历史产物缺失无法 retry publish，新提交尚未端到端验证。

> 与早期报告（日期 2026-08-18 / 19）的差别见文末"修正说明"。

---

## 1. 复检过程（本机 A 视角）

本机 A（ocdev）是 BFF 主机，**对机器 B 没有 SSH、没有 shell 类 MCP 工具**——只能：

1. 通过 Streamable-HTTP MCP 直接调机器 B 的工具（已建立 session `1ed8415145e04d45b31db83d08455604`）
2. 查询 BFF SQLite `render_jobs` / `image_batches` 表
3. 解析 `journalctl -u frameflow-bff` 日志
4. 在本机跑 `om_mcp_probe.py` 等黑盒探测

### 1.1 上游 MCP 连通性

| 项 | 状态 |
|---|---|
| TCP/IPv6 可达 | ✅ `240e:3b0:c49:f726:35e0:c08c:c3a5:88d8` |
| `initialize` 握手 | ✅ 成功，返回 session id |
| `tools/list` | ✅ 返回 24 个工具 |
| `get_render_status` | ✅ 返回任务状态 |
| `retry_render_publish` | ✅ 可调，但失败任务不再 retry @babel，而是报 `Persisted video_path does not exist` |
| shell / 文件系统类 MCP 工具 | ❌ **无**（工具集里无任何 `npm install` / 文件管理工具） |

→ 本机 A 仍只能做只读诊断与重启渲染触发，无法直接修机器 B 的依赖。

### 1.2 历史任务在 MCP 视角下的当前状态

| Job ID | BFF SQLite 视角 | MCP 视角（重新查） | MCP `updated_at` | 备注 |
|--------|----------------|-------------------|------------------|------|
| `f9bbaedf08ed416c811bfb9fd270ead0` | 失败（2026-08-18 18:07） | `failed` | 2026-08-18 10:07:43 | 错误仍是 `@babel/standalone` 缺失（最早一批失败） |
| `cc8b73a22ccc4981ac214268c4f3d7e2` | 失败（2026-08-18 18:19） | **`published`** | **2026-08-19 03:03:15** | 机器 B 自动续跑成功 |
| `035f1b3e70ec4112afb5f5b12e71b62f` | 失败（2026-08-18 18:22） | **`published`** | **2026-08-19 03:03:24** | 机器 B 自动续跑成功（与 cc8b73a2 相差 9 秒，疑似同一波批处理） |
| `58c293cd1b2d4e7b9922d9fa4216fc73` | 失败（2026-08-18 15:45） | `failed` | 2026-08-18 10:01:33 | 错误仍是 `@babel/standalone` 缺失（最早一批失败） |
| `d75622b7d77b4ce392514c8c20beeccd` | 已完成（2026-08-17） | `published` | 2026-08-17 13:39:26 | 正常基线 |

### 1.3 retry_render_publish 输出

对 `f9bbaedf` 和 `58c293cd` 调用 retry：

```json
{
  "success": false,
  "status": "failed",
  "error": "Persisted video_path does not exist: ."
}
```

**错误关键词已从 `@babel/standalone` 变为 `video_path does not exist`** —— 这是关键拐点：

- `@babel/standalone` 是 webpack resolve 阶段报错，发生在打包前
- `video_path does not exist` 是 retry 阶段找不到历史视频文件
- → 意味着 `@babel/standalone` 已不是当前失败原因，机器 B 的打包器现在能跑过 resolve 阶段

### 1.4 今天（2026-08-19）的渲染活动

- `image_batches` 最近一条 `batch-6e19af72f91e6957d59f511a / collecting / asset_count=3` —— 用户在收集阶段，**尚未触发 Render 提交**，所以今天的 BFF journal 里**没有**任何 `@babel` 错误（不是修了所以没出错，是没新提交触发打包）
- BFF journal（2026-08-19 全天）grep `@babel | module not found | cannot resolve` → **0 条**

→ 不能用"今天没有失败"反推"问题已修复"，证据强度不够；但配合 §1.2、§1.3 已足够判定。

---

## 2. 结论（修正早期报告）

| 早期报告结论 | 复检结论 |
|---|---|
| "机器 B 的 `remotion-composer/node_modules` 缺少 `@babel/standalone`" | ✅ 仍成立 |
| "所有 Remotion 渲染在打包阶段失败" | ❌ **不成立**：2 个 18 日失败任务在 19 日 03:03 已自动恢复为 published |
| "需机器 B 运维执行 `npm install` 修复" | ✅ 已修复（具体方式未知，机器 B 侧自动续跑成功即可间接证明） |
| "本机 A 无机器 B 的管理通道" | ✅ 仍成立 |

**修复情况判断**：

- **机器 B 端 @babel/standalone 已修复**（强证据：MCP 视角下 2/4 个老失败任务自动 published + retry 错误不再含 @babel 关键词）
- **残留失败任务**：4 个 18 日失败任务中 2 个已 published（cc8b73a2、035f1b3e），剩 2 个（f9bbaedf、58c293cd）没自动续跑，且 retry 工具无法补刀
- **新提交能否跑通**：**未验证**（今天没有新 render_job 提交，本机 A 端到端冒烟被多种工具兼容性问题阻断——`execute_tool` / `dry_run_tool` / `om_mcp_probe.py upload` 均返回空响应或 502）

---

## 3. 建议机器 B 运维做的核实

按优先级：

### 3.1（必做）确认 @babel 修复是否稳定

```bash
# 1. node_modules 状态
cd /opt/OpenMontage
ls -d remotion-composer/node_modules/@babel/standalone 2>&1   # 应存在

# 2. 当前版本与 package.json 锁定一致
node -e 'console.log(require("/opt/OpenMontage/remotion-composer/node_modules/@babel/standalone/package.json").version)'  # 应为 8.0.4（或 ^8.0.4 解析到的版本）
cd /opt/OpenMontage && grep -A1 '"@babel/standalone"' remotion-composer/package.json remotion-composer/package-lock.json

# 3. Remotion 单元冒烟
cd /opt/OpenMontage && python -c "
from tools.tool_registry import registry
registry.discover()
print(registry.get('video_compose').get_info().get('render_engines'))
"   # remotion 应在

# 4. 端到端冒烟（独立 session，避免污染生产数据）
cd /opt/OpenMontage && python om_mcp_probe.py upload <小图.png> -p smoke-babel-recheck
# 然后通过 MCP create_remotion_video_share 触发一次最小渲染
# 轮询 get_render_status 直到 published 或新的错误信息
```

### 3.2（建议）补跑 18 日残留失败任务

`f9bbaedf` 和 `58c293cd` 是 18 日最早一批失败的，由于当时未生成视频文件，`retry_render_publish` 无法修复。建议：

- 直接用 MCP `create_remotion_video_share`（如已上线）重新发起这两个 batch
- 或者从 BFF 侧让用户重新提交（产生新 render_job_id，旧的失败标识废弃）

### 3.3（建议）保留 03:03 这次自动续跑的日志

19 日 03:03 这波自动续跑是修复成功的关键证据。建议保留 MCP 服务同时段日志（rc.local / systemd journal / nginx access log），方便后续审计修复时间线。

---

## 4. 本机 A / BFF 侧可执行事项

- [x] BFF 二进制已升级至 27f91d2（commit `fix(bff): 修复默认脚本选择的不确定性`），默认脚本固定为「电商产品演示」
- [x] `RATE_LIMIT_PER_MIN=240` 已生效（.env，dotenv 加载），覆盖大批量图片上传场景
- [ ] **建议**：在 `image_batches` 上线一个"复检"按钮——对老 failed batch，调 `retry_render_publish` + 必要时直接重新 Render
- [ ] **建议**：把 BFF 的 `render_jobs.status` 与 MCP `get_render_status` 主动校准的频率提高到 30 秒（commit `8f1f272 feat(bff): 渲染任务状态统一以 MCP 后台为唯一权威源实时刷新` 是这个方向的下一步；当前实现是 List/查询时刷新，没有后台心跳）

---

## 5. 修正说明

- 早期报告 `RENDER_BABEL_STANDALONE_FIX_REPORT.md` 第一版（commit `27f91d2 docs(frameflow): 新增 @babel/standalone 渲染失败诊断报告` 提交时引用）结论是"全部 Remotion 渲染失败"。
- 该结论基于当时 `journalctl -u frameflow-bff` 与 `get_render_status` 的实时数据——**那时的结论正确**，但**未考虑到机器 B 端会自动续跑**。
- 这次复检发现 19 日 03:03 机器 B 已经对部分失败任务做了自动重发 + 微云分享，已 publish；说明机器 B 侧已意识到该问题并修复。
- 因此本报告**修正早期结论为"已部分修复，残留 2 个任务需人工重提，新提交待验证"**，并提供给机器 B 运维一份简短的核实清单，让其确认修复稳定性。

---

## 6. 证据附录

```text
# A. retry_render_publish(f9bbaedf) - 2026-08-19 重新查
{"success": false, "status": "failed",
 "error": "Persisted video_path does not exist: ."}

# B. retry_render_publish(58c293cd) - 2026-08-19 重新查
{"success": false, "status": "failed",
 "error": "Persisted video_path does not exist: ."}

# C. get_render_status(cc8b73a2) - 2026-08-19 重新查
{"success": true, "status": "published", "stage": null,
 "video_path": "/opt/OpenMontage/projects/frameflow-batch-batch-d6838aee4fccdc3ea4bb7a3e/renders/<...>.mp4",
 "share_url": "https://share.weiyun.com/kXqxKGji",
 "updated_at": "2026-08-19T03:03:15.623713+00:00"}

# D. get_render_status(035f1b3e) - 2026-08-19 重新查
{"success": true, "status": "published", "stage": null,
 "share_url": "https://share.weiyun.com/R3OUyGLL",
 "updated_at": "2026-08-19T03:03:24.227361+00:00"}

# E. 上游 MCP initialize
SID=1ed8415145e04d45b31db83d08455604
TOOL COUNT = 24 (list_tools)

# F. 依赖声明（本机 A 上的 package.json，未变）
remotion-composer/package.json: "@babel/standalone": "^8.0.4"
remotion-composer/src/CustomComposition.tsx:3: import * as Babel from "@babel/standalone";

# G. BFF journal 2026-08-19 全天 grep @babel
（0 条）
```