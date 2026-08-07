# MCP Server: rsync_upload_artifact 与 export_bundle 诊断报告

**日期**: 2026-08-08  
**现象**: 远程 `http://lanes.ymxt.top:8900/mcp` 暴露了 108 个工具，  
包括 `rsync_upload_artifact` 和 `export_bundle`，但本地 `mcp_server.py` 缺少对应的 MCP tool 包装。

---

## 问题概述

本地 `mcp_server.py`（git main 分支，commit `39dbf84`）共注册 **15 个 MCP 工具**，  
远程服务器实际暴露 **108 个工具**，差异如下：

### 远程有，本地没有（需补充的 MCP 包装）

| 工具名 | 能力组 | 模块路径 | 状态 |
|--------|--------|----------|------|
| `rsync_upload_artifact` | `artifact_delivery` / `publish` | `tools.rsync_upload` | 本地已有 `tools/rsync_upload.py`，缺 MCP 包装 |
| `export_bundle` | `publish` | `tools.publishers.export_bundle` | 本地已有 `tools/publishers/export_bundle.py`，缺 MCP 包装 |

> 其余 106 个差异工具（`edge_tts`、`flux_image` 等）均为 registry 自动发现的底层工具，  
> 通过 `execute_tool` 统一调用，**无需单独 MCP 包装**。

### 本地有，远程没有

无。本地 15 个 MCP 工具远程全部覆盖。

---

## 根因分析

远程服务器上的 `mcp_server.py` 是**手工修改版本**，未提交到 git。  
本地 main 分支只包含核心 MCP 包装（15 个），`rsync_upload_artifact` 和 `export_bundle`  
的 MCP 层是在远程手动添加的，但本地仓库未同步。

---

## 需要做的事

### 1. 在 `mcp_server.py` 中添加 `rsync_upload_artifact` 的 MCP 包装

参考 `s3_upload` 的包装模式（`mcp_server.py:344-398`），在 `write_checkpoint` 之后添加：

```python
@mcp.tool()
def rsync_upload_artifact(
    source_path: str,
    remote_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload a rendered artifact to a public server via SSH/rsync.

    Configuration is read from .env (RSYNC_* variables).
    """
    tool = registry.get("rsync_upload_artifact")
    if tool is None:
        return {"success": False, "error": "rsync_upload_artifact tool is not registered"}
    result = tool.execute({
        "source_path": source_path,
        "remote_name": remote_name,
        "dry_run": dry_run,
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}
```

### 2. 在 `mcp_server.py` 中添加 `export_bundle` 的 MCP 包装

```python
@mcp.tool()
def export_bundle(
    video_path: str,
    project_name: Optional[str] = None,
    chapters: Optional[list[dict]] = None,
    metadata: Optional[dict] = None,
) -> dict[str, Any]:
    """Package a rendered video with metadata into a self-contained export bundle.

    Writes a schema-valid publish_log with status='exported'.
    """
    tool = registry.get("export_bundle")
    if tool is None:
        return {"success": False, "error": "export_bundle tool is not registered"}
    result = tool.execute({
        "video_path": video_path,
        "project_name": project_name,
        "chapters": chapters,
        "metadata": metadata,
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}
```

### 3. 将修改提交并推送到远程服务器

```bash
git add mcp_server.py
git commit -m "feat(mcp): add rsync_upload_artifact and export_bundle MCP wrappers"
git push origin main
```

远程服务器 `pull` 后重启 MCP 服务即可。

---

## 验证方法

部署后执行：

```bash
curl -s -X POST http://lanes.ymxt.top:8900/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_tool_info","arguments":{"tool_name":"rsync_upload_artifact"}}}'
```

应返回完整的 input_schema 和 output_schema，而非 "not found"。

---

## 附：远程工具清单（108 个）

完整列表已由诊断脚本获取，保存于 `docs/mcp-remote-tool-list.json`（未纳入版本控制）。
