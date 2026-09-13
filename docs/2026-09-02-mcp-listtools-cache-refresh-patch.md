# MCP server.listTools 缓存刷新诊断补丁 — 2026-09-02

## TL;DR

- **动机**：`openmontage-mcp.service` 在 2026-08-31 22:30:25 死掉，root cause 是 `tools/analysis/frame_sampler.py` 编辑后，hot-reload 导致工具注册失败；MCP 日志里只能看到一句低信息量的 `WARNING Tool 'frame_sampler' not listed, no validation will be performed`，无法判断是缓存刷新失败还是工具真的没注册。
- **修复**：在 vendored MCP 库 `.venv/lib/python3.10/site-packages/mcp/server/lowlevel/server.py` 的 `Server._get_cached_tool_definition` 上加 try/except + 富化 warning。详见 `patches/mcp-server-listtools-cache-refresh-2026-09-02.patch`。
- **持久性**：`pip install --upgrade mcp` 会覆盖 patch。`make install` / `make setup` 末尾应自动重打（TODO）。

## 时间线（来自 journalctl）

| 时间 | 事件 |
|---|---|
| 2026-08-31 18:48:56 | systemd 启动 `openmontage-mcp.service`（PID 3317991） |
| 2026-08-31 22:28:35 | `tools/analysis/frame_sampler.py` mtime（被编辑） |
| 2026-08-31 22:29:18 | root 登录 pts/1（距 MCP 死亡 67s） |
| 2026-08-31 22:29:48 | MCP server 3 次警告 `Tool 'frame_sampler' not listed, no...` |
| 2026-08-31 22:30:25 | MCP server 收到 SIGTERM，干净退出；systemd `Deactivated successfully` |
| 2026-08-31 22:30:25 | systemd 日志**无** `Stopping...` 行 → SIGTERM 是外部发送的，未走 systemd stop |
| 2026-08-31 22:38:35 | commit `6faedd9 fix(frame_sampler): workspace-contract guard for output_dir` |
| 2026-08-31 22:51:38 | 有人手动 `python3 mcp_server.py &` 重启 MCP（PID 4111946），绕过 systemd |
| ... | systemd 一直 inactive 到 2026-09-02 |
| 2026-09-02 22:11 | `.env` 文件 mtime（用户报告有更新） |
| 2026-09-02 22:48 | 本会话首次诊断，发现主分支落后 upstream；fast-forward 到 `cb0c5c5` |
| 2026-09-02 22:49:07 | `systemctl start openmontage-mcp.service` — 改用 systemd |
| 2026-09-02 22:56:36 | 修改 `.venv/.../server.py:_get_cached_tool_definition` |
| 2026-09-02 22:58:47 | `systemctl restart openmontage-mcp.service` — 加载 patch（PID 2715038 → 2749024） |

## 死亡原因调查（已排除）

- ❌ OOM killer — `dmesg` 8/31 整天无 OOM 事件
- ❌ Python exception/traceback — 关闭前日志无 error/exception
- ❌ `systemctl stop` — journal 无 `Stopping...` 行，说明 PID 3317991 的 SIGTERM 不是 systemd 发的
- ❌ 资源耗尽 — CPU 累计 8min 23s（正常运行水位）

**最可能解释**：某用户/agent 编辑 `frame_sampler.py` 后触发 MCP hot-reload，新版本注册失败（可能是 import 异常或 `@register_tool` decorator 失效），3 次 warning 之后通过 `kill 3317991` 直接清理，但**忘了用 `systemctl restart` 拉起**（手动 `python3 mcp_server.py &` 绕过了 systemd）。

## Patch 内容

`patches/mcp-server-listtools-cache-refresh-2026-09-02.patch` — `Server._get_cached_tool_definition` 方法的 unified diff。

两个改动：

1. **包住缓存刷新**：cache miss 时调 `request_handlers[ListToolsRequest]` 之前的 `await ...` 加 try/except，捕获任何异常并 `logger.exception(...)`，记录完整 traceback。
2. **富化 warning**：原 `Tool 'X' not listed, no validation will be performed` 改为 `Tool 'X' not listed, no validation will be performed (cache_size=N, list_tools_handler_present=True/False)`，从单一日志能看出是缓存真有那么多还是 handler 没注册。

## 重启日志（2026-09-02 22:58:47 — systemd）

```
$ systemctl restart openmontage-mcp.service
$ journalctl -u openmontage-mcp.service --no-pager -n 15 | tail
Sep 02 22:58:49 xt python3[2749024]: [22:58:49] INFO Streamable HTTP keep-alive timeout: 30s
Sep 02 22:58:49 xt python3[2749024]: INFO:     Started server process [2749024]
Sep 02 22:58:49 xt python3[2749024]: INFO:     Waiting for application startup.
Sep 02 22:58:49 xt python3[2749024]: [22:58:49] INFO executor.health.monitor started ...
Sep 02 22:58:49 xt python3[2749024]: INFO:     Application startup complete.
Sep 02 22:58:49 xt python3[2749024]: INFO:     Uvicorn running on socket ('::', 8900, 0, 0)

$ curl -X POST http://127.0.0.1:8900/mcp -H "Authorization: Bearer $MCP_API_TOKEN" ...
HTTP 200

$ .venv/bin/python3 -c "from mcp.server.lowlevel.server import Server; import inspect; \
  src = inspect.getsource(Server._get_cached_tool_definition); \
  print('LOCAL PATCH' in src, 'cache_size=%d' in src, 'try:' in src)"
True True True
```

## 重新应用 patch（在 `pip install --upgrade mcp` 之后）

```bash
cd /opt/OpenMontage_Voicebox

# patch 文件头是 'a/.venv/.../server.py' / 'b/.venv/.../server.py'，
# 在仓库根用 -p1 去掉首段 'a/' / 'b/'，从 .venv/... 起匹配。
patch -p1 < patches/mcp-server-listtools-cache-refresh-2026-09-02.patch

# 验证 patch 已生效（含 LOCAL PATCH 注释 + try/except + cache_size 富化）：
.venv/bin/python3 -c "
from mcp.server.lowlevel.server import Server
import inspect
src = inspect.getsource(Server._get_cached_tool_definition)
assert 'LOCAL PATCH' in src, 'patch 标记缺失'
assert 'cache_size=%d' in src, 'cache_size 富化缺失'
assert 'logger.exception' in src, 'try/except 缺失'
print('patch OK')
"

# 重启 MCP 让新代码生效：
systemctl restart openmontage-mcp.service
```

## 建议后续

1. **`scripts/apply_mcp_patches.sh`**：把上面 `patch + verify + systemctl restart` 包成脚本，挂到 `make install` / `make setup` 末尾自动跑（防止下次升级 mcp 后丢失 patch）。
2. **upstream PR**：MCP 库的 `Server._get_cached_tool_definition` 缓存刷新失败被无声吞掉是真 bug。建议提 PR 至少加 try/except + logger.exception。
3. **`tools/analysis/frame_sampler.py` 的 hot-reload 入口**：如果有 `importlib.reload(...)` 之类的代码，失败时应显式 log "re-register threw"，不要依赖 MCP 库的兜底日志。