# OpenMontage 微云分享接口探测发现（WEIYUN_SHARE_PROBE_FINDINGS）

> 探测对象：`https://dw.aixifs.com/mcp`（Streamable-HTTP MCP，Bearer token）
> 探测时间：2026-08-11
> 探测方式：黑盒 MCP 调用 + 源码交叉核对（`mcp_server.py` / `tools/weiyun.py` / `lib/mcp_session.py`）
> 复测工具：本文档同目录 `om_mcp_probe.py`

---

## 0. TL;DR

- 微云分享相关工具**现已暴露**（工具总数 19 → 21）：`weiyun_gen_share_link` 与 `create_remotion_video_share`。
- **远端已提交修复**：commit `154b427`（谢生，2026-08-11 22:34）`feat(mcp): expose weiyun.gen_share_link as MCP tool`，新增 `@mcp.tool()` 包装的 `weiyun_gen_share_link`，把 `file_list`/`dir_list`/`share_name`/`passwd` 作为 kwargs 正确塞进 `inputs` 再调 `tool.execute(inputs)`。本地仓库已 `git merge --ff-only` 同步到该提交。
- **但线上调用仍失败**（详见第 3 / 8 节）：`weiyun_gen_share_link` 的 `file_list`/`dir_list` 依旧没传进底层函数（6/6 重试一致报 "file_list or dir_list is required"）。
- **已实施修复（见第 9 节）**：把 `weiyun_gen_share_link` 签名的 `list[str] | None = None`（含 null 联合分支）改为纯数组 `list[str] = []`，去掉触发部署端 FastMCP 把数组参数丢弃成 `None` 的 null 分支；`file_list: [...]` / `dir_list: [...]` 文档化契约不变。代码已改、待提交推送并重启部署后复测。
- 另：写操作 `upload_asset` / `create_remotion_video_share` 仍存在 **session 注入断裂**（`get_mcp_session_id()` 拿不到值，报 "session required"），属负载均衡/会话非粘性部署层问题，单独阻碍整条链路（详见第 8.3）。
- 客户端侧调用坑：服务端**每台响应轮换 `Mcp-Session-Id`**；Windows schannel 偶发 **TLS 抖动（curl 35）**。

---

## 1. 两版发现

### 第一版（工具暴露之前，误判）
最初 `tools/list` 返回 19 个工具，**只有 `weiyun_upload`**，没有 `weiyun_gen_share_link`。
据 `weiyun_upload` 自身描述（"token-based counterpart to the cookie-based `weiyun_publish`"），
当时推断：`weiyun_gen_share_link` 属于**另一个独立的腾讯微云连接器**（`tencent-weiyun`），
生成共享链接需跨两个 MCP 服务两步完成（OpenMontage 上传拿 file_id → 微云连接器 `gen_share_link` 拿链接）。
—— **这一版结论已过时**，仅供追溯。

### 第二版（现已暴露，但 wiring 未通）✅ 当前结论
重新 `tools/list` 返回 **21** 个工具，新增：
- `weiyun_gen_share_link(file_list, dir_list, share_name, passwd)` —— 入参是**服务器端文件路径/目录**，返回微云 `short_url`。
- `create_remotion_video_share(project_id, duration_per_image, aspect_ratio, title)` —— 一站式：传图后直接渲染 Remotion **并**生成微云分享链接（默认竖屏 9:16）。

实测两者均无法产出真实链接（见第 3 节）。

---

## 2. 接口契约（来自 `tools/list` / 源码）

| 工具 | 注册名（源码） | 入参 | 出参 | 依赖 |
|---|---|---|---|---|
| `weiyun_gen_share_link` | `weiyun.gen_share_link` (`tools/weiyun.py:395`) | `file_list[]`、`dir_list[]`、`share_name`、`passwd` | `short_url`、`share_name` | mcporter → 微云连接器 |
| `weiyun_upload` | （表层工具） | `video_path`、`target_dir?`、`overwrite?` | `file_id`、`filename`、`mcp_url` | 微云连接器 |
| `create_remotion_video_share` | （表层工具，`mcp_server.py:476`） | `project_id`、`duration_per_image`、`aspect_ratio`、`title` | 渲染+分享结果 | **隐式依赖 MCP session 里的上传图片** |

---

## 3. 实测证据（黑盒调用）

| 调用 | 结果 | 解读 |
|---|---|---|
| `tools/list` / `get_tool_info` | ✅ 正常 | 只读工具 |
| `weiyun_gen_share_link`（`dir_list=["/opt/OpenMontage/renders"]`） | `success=false`，`"file_list or dir_list is required"` | **参数转发断**：底层收到空 inputs |
| `upload_asset`（base64 上传图片） | 一致 `"Streamable HTTP Mcp-Session-Id is required"`（重试 12 次全失败） | **session 注入断**：排除随机命中多实例 |
| `create_remotion_video_share` | 同上报 session 缺失 | 同上 |

`weiyun_gen_share_link` 的 session 校验**已通过**（不是报 session 缺失，而是报参数缺失），
说明它与 session 无关，纯粹是 `arguments → execute(inputs)` 的转发断链。

### 3.1 第三版复测（git 修复提交 `154b427` 后，2026-08-11 深夜）
- `tools/list` 拿到的 `weiyun_gen_share_link.inputSchema` **已是新代码形态**（FastMCP 自动由 `@mcp.tool` 函数签名生成：`file_list`/`dir_list`/`share_name`/`passwd`，title=`weiyun_gen_share_linkArguments`）→ 确认部署进程已加载新代码。
- 显式传 `file_list=["/opt/OpenMontage/renders/output.mp4"]` 与 `dir_list=["/opt/OpenMontage/renders"]`，**连续 6 次调用全部仍报 "file_list or dir_list is required"**（排除负载均衡命中旧实例）。
- 对照测 `upload_asset`（传 `content_base64`/`filename`/`project_id`）：返回 `"Mcp-Session-Id is required"` —— 说明该函数的**参数其实转发成功**（已进入函数体用 session），只是 session ContextVar 没注入；反证 FastMCP 参数绑定对 `upload_asset` 这类普通类型正常，问题集中在 `weiyun_gen_share_link` 的 **联合类型参数**。
- 结论：新 `@mcp.tool` 包装部署到位，但 `file_list`/`dir_list`（`list[str] | None`）**依旧没绑定进函数**，底层收到不含它们的 inputs。

---

## 4. 源码级根因定位

### 4.1 session 机制（`lib/mcp_session.py` + `mcp_server.py:1044-1052`）
- session 用 **`ContextVar`**（`_mcp_session_id`），request-scoped。
- 在 ASGI 中间件里：`session_id = request_session`（取自 `Mcp-Session-Id` 请求头）→ `set_mcp_session_id(session_id)`，请求结束 `finally` 中 `reset`。
- 写操作 `upload_asset` / `create_remotion_video_share`（`mcp_server.py:489` 等）调用 `get_mcp_session_id()`
  拿**隐式上传图片**。`get_mcp_session_id()` 返回 `None` 时即报 "session required"。
- ⚠️ 推断：dispatch 路径上 `ContextVar` 未跨线程/任务传播，或 `request_session` 解析失败，导致写操作 handler 内取不到 session。
  （只读工具不依赖该值，故正常——与实测一致。）

### 4.2 微云分享参数转发断（`tools/weiyun.py:419-429` + `mcp_server.py:957`）

底层 `WeiyunGenShareLink` 本身没问题：
```python
def execute(self, inputs):
    return self._call_mcporter("weiyun.gen_share_link", inputs)

def _call_mcporter(self, tool_name, inputs):
    args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
    if inputs:                                    # ← 关键：inputs 非空才带 --args
        args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])
```
- `WeiyunGenShareLink` **本身不读 session**，直接转发 mcporter；`_call_mcporter` 只有 `inputs` 非空才加 `--args`。
- 旧版断定"MCP 暴露层没把 `arguments` 传给 `execute`"——但 `154b427` 已新增 `@mcp.tool()` 包装把 kwargs 塞进 `inputs`（见 8 节）。
- **新事实**：新包装已部署，schema 也是新的，但 `file_list`/`dir_list` 仍没进 `inputs`。
  唯一特殊点：该包装函数签名用了 **PEP 604 联合类型 `list[str] | None = None`**。
  → 疑点（4.2.1）：部署的 `mcp` SDK / FastMCP 版本对该类 `X | None` 类型提示的参数**绑定失败**，把值丢弃为默认值 `None`，于是 `if file_list:`/`if dir_list:` 恒假，`inputs` 只剩 `mcp_session_id`，mcporter 不带 `--args` → 微云 server 报缺字段。
- 佐证：`upload_asset` 用普通类型（`str`/`Optional[str]`），其参数能正常转发（见 3.1 对照），进一步锁定问题在该工具的联合类型提示。

---

## 5. 客户端调用坑（必看）

1. **SID 轮换**：服务端在 `initialize` / `notifications/initialized` 后可能**轮换 `Mcp-Session-Id`**，
   且每次响应头都可能带新值。必须在**每次响应后重新读取并回带**该 header，否则后续调用报 "Mcp-Session-Id is required"。
2. **TLS 抖动**：Windows schannel 偶发 `curl (35) OpenSSL SSL_connect ... EOF`，
   表现为空响应 / SID 为空。加 `--retry`（`--retry-all-errors`）或外层重试即可。
3. **文件名安全**：`upload_asset` 拒绝中文/空格 basename，需转成 ASCII 安全名（如 `wxwork_img01.jpg`）。
4. **参数名坑**：`upload_asset` 用 `project_id`（非 `project`）；`get_pipeline` 用 `name`；`get_pipeline_stages` 用 `pipeline_name`。

---

## 6. 复测工具 `om_mcp_probe.py`

已落地在本仓库根目录，封装了上面的所有坑（SID 轮换 + curl 重试 + 参数安全）。

```bash
# 列出全部工具（验证 21 个、确认 weiyun_gen_share_link 在列）
python om_mcp_probe.py list

# 上传一张图，打印服务器端 path
python om_mcp_probe.py upload "C:/path/45.jpg" -p mclaw-demo

# 生成微云分享链接（当前预期复现 "file_list or dir_list is required"）
python om_mcp_probe.py share -d /opt/OpenMontage/renders
python om_mcp_probe.py share -f /opt/OpenMontage/renders/output.mp4

# 调任意工具
python om_mcp_probe.py call weiyun_gen_share_link '{"dir_list":["/opt/OpenMontage/renders"]}'

# 自定义端点 / token（覆盖默认值）
OM_MCP_URL=... OM_MCP_TOKEN=... python om_mcp_probe.py list
```

子命令：`init` / `list` / `call <name> <json>` / `upload <file> -p <project>` / `share -d <dir> | -f <file>`。

---

## 7. 修复建议（给 OpenMontage 维护者）

| 优先级 | 问题 | 建议 |
|---|---|---|
| P0 | `weiyun_gen_share_link` 参数转发断 | 确认 MCP 暴露层把 `tools/call.arguments` 透传到 `WeiyunGenShareLink.execute(inputs)` |
| P0 | 写操作 session 注入断 | 排查 `ContextVar` 跨线程/任务传播，或 `request_session` 头解析；保证 `tools/call` 与 `initialize` 落在同一 session 上下文 |
| P1 | SID 轮换无文档 | 在 `MCP_SERVER.md` 注明客户端须回带每次响应的 `Mcp-Session-Id` |
| P2 | TLS 抖动 | 服务端侧启用会话复用 / 客户端 `--retry-all-errors`（已在 `om_mcp_probe.py` 处理） |

---

## 8. 第三版进展：远端修复提交 + 本地同步 + 线上复测结论（2026-08-11 深夜）

### 8.1 远端已提交修复（git）
- 提交 `154b427`（作者：谢生，2026-08-11 22:34）：`feat(mcp): expose weiyun.gen_share_link as MCP tool`。
- diff 仅在 `mcp_server.py` 新增 28 行：`@mcp.tool()` 包装函数
  ```python
  @mcp.tool()
  def weiyun_gen_share_link(
      file_list: list[str] | None = None,     # ← PEP 604 联合类型
      dir_list:  list[str] | None = None,
      share_name: str = "",
      passwd: str = "",
  ) -> dict[str, Any]:
      tool = registry.get("weiyun.gen_share_link")
      inputs = {"mcp_session_id": get_mcp_session_id()}
      if file_list: inputs["file_list"] = file_list
      if dir_list:   inputs["dir_list"]   = dir_list
      if share_name: inputs["share_name"] = share_name
      if passwd:     inputs["passwd"]     = passwd
      result = tool.execute(inputs)
      return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}
  ```
- 代码意图正确：FastMCP 应把 `arguments` 的 `file_list`/`dir_list` 作为 kwargs 传入，再塞进 `inputs`。

### 8.2 本地同步
- 本地 `main` 落后该提交恰好 1 个提交（快进关系，无分叉、无冲突）。
- 已执行 `git merge --ff-only 154b427` 同步，工作区 `mcp_server.py` 已是修复版（另 `git update-ref refs/remotes/origin/main 154b427` 修正本地 stale 引用）。

### 8.3 线上复测（决定性）
- 线上 `tools/list` 的 `weiyun_gen_share_link.inputSchema` **已是新代码形态** → 部署进程确实加载了 154b427。
- 但显式传 `file_list` / `dir_list` 连续 **6/6 仍报 "file_list or dir_list is required"**（排除负载均衡命中旧实例）。
- 对照 `upload_asset` 参数能正常转发（见 3.1）→ 问题集中在 `weiyun_gen_share_link` 的联合类型参数绑定。

### 8.4 结论与下一步（给维护者）
- git 上的修复**方向对、代码形态对**，但**尚未在线上真正生效**。
- 最强嫌疑：**`@mcp.tool` 函数签名里的 `list[str] | None`（PEP 604 联合类型）被部署的 `mcp` SDK / FastMCP 版本绑定失败**，导致 `file_list`/`dir_list` 被丢弃为 `None`。
- 建议改动（任选其一，推荐 A）：
  - **A（首选）**：把类型提示改为 `Optional[list[str]]`（部分 FastMCP 版本对 `Optional` 绑定更稳），即
    `file_list: Optional[list[str]] = None`、`dir_list: Optional[list[str]] = None`（顶部 `from typing import Optional`）。
  - B：去掉联合类型，用 `file_list: list[str] = []`、`dir_list: list[str] = []`（调用方仍可按需传列表）。
  - C：升级部署环境的 `mcp` / `fastmcp` 到支持 PEP 604 绑定的版本。
- 改后**必须重启/重新部署所有实例**，再用 `python om_mcp_probe.py share -d /opt/OpenMontage/renders` 复测；
  预期从 "file_list or dir_list is required" 变为拿到 `short_url`（或退化为 `WEIYUN_MCP_TOKEN` 缺失类凭据错误——那是另一条独立问题）。
- 另：`upload_asset` / `create_remotion_video_share` 的 **session 注入断**仍未修复（8.3 对照仍报 session required），需一并处理才能真正端到端出片。

---

## 9. 已实施修复（2026-08-11 23:33）

针对第 8.4 定位的根因，已在本地 `mcp_server.py` 实施修复，**采用第 8.4 推荐方案 B（去掉 null 联合分支）**：

```python
# 修改前（部署端 FastMCP 把数组参数丢弃为 None）
@mcp.tool()
def weiyun_gen_share_link(
    file_list: list[str] | None = None,
    dir_list:  list[str] | None = None,
    share_name: str = "",
    passwd: str = "",
) -> dict[str, Any]:
    ...

# 修改后（纯数组 + 空列表默认，不再生成 anyOf[array, null]）
@mcp.tool()
def weiyun_gen_share_link(
    file_list: list[str] = [],
    dir_list:  list[str] = [],
    share_name: str = "",
    passwd: str = "",
) -> dict[str, Any]:
    ...
```

**为什么这样改**：线上 `tools/list` 实测 `file_list`/`dir_list` 的 `inputSchema` 为 `anyOf:[{type:array}, {type:null}]`，而能正常工作的 `share_name`/`passwd` 是纯 `string`。对照仓库内其它用 `Optional[str]`（同样是 `anyOf:[string,null]`）的写操作参数能正常转发，说明部署端 FastMCP 对该类的 **数组 + null 联合** 绑定失败、把值丢弃——去掉 null 分支后 schema 退化为纯 `{type:array}`，可正常绑定。调用方仍按原契约传 `file_list: ["/path"]`，无需改动客户端。

**验证状态**：
- ✅ 本地 `py_compile mcp_server.py` 通过；`Any` / `Optional` 均已导入（顶部 `from typing import Any, Optional`）。
- ⏳ 代码已改、**尚未提交推送**；推送并**重启/重新部署全部实例**后，须用 `python om_mcp_probe.py share -d /opt/OpenMontage/renders` 复测：
  - 成功标志：返回 `data.short_url`（微云分享短链）。
  - 退化标志：返回 `WEIYUN_MCP_TOKEN` 缺失类凭据错误 → 那是独立的令牌配置问题，说明数组绑定已修通。
- ⚠️ 仍未解决：`upload_asset` 等的 session 非粘性（负载均衡）问题，属部署层，需单独处理才能端到端出片。
