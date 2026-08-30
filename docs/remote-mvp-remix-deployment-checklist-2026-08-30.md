# 远端素材替换 MVP 部署清单（2026-08-30）

目标：让测试环境 `http://192.168.20.173:8900/mcp` 与本机当前代码保持一致，并可验证“上传产品图 → 固定区域覆盖 → 输出新视频”的链路。

## 当前验证结论

- `upload_asset`：已验证成功，测试项目：`mvp-smoke-20260830`。
- `video_compose(operation=overlay)`：已验证成功，生成：`projects/mvp-smoke-20260830/logo_overlay.mp4`。
- `read_session_asset_image`：远端当前返回 `tool is not registered`，说明运行中的 MCP 服务尚未加载最新代码。
- 原始视频的自动拆解、字幕/音频保留、整镜头替换和高级跟踪未在本次 smoke test 中宣称完成；先完成下列部署同步。

## 远端需要处理的事项

### 1. 同步并重启服务

将远端工作树同步到本分支最新提交（当前本机基线为 `35c3a14`，包含 `acd3ee5` 的 `read_session_asset_image` 代码），然后重启 MCP 服务。不要只同步单个文件，避免工具注册表和实现版本不一致。

重启后确认服务加载的是该工作树，而不是旧的全局安装目录或旧进程。

### 2. 确认 MCP 工具注册

通过已认证会话调用：

```text
get_tool_info("read_session_asset_image")
```

随后对一个刚上传的 PNG 调用 `read_session_asset_image`，响应必须包含 MCP 原生 `image` content（不是“tool is not registered”，也不是仅文本错误）。

### 3. 修正上传脚本的地址配置

`scripts/mcp_upload.py` 的文档声称支持 `OPENMONTAGE_MCP_URL`，但当前 `mcp_call()` 仍硬编码 `http://localhost:8900/mcp`。请改为读取环境变量，默认值仍为 localhost，例如：

```python
MCP_URL = os.environ.get("OPENMONTAGE_MCP_URL", "http://localhost:8900/mcp")
```

所有上传调用使用 `MCP_URL`。修复后用远端地址运行脚本，不应再连接本机 8900 端口。

### 4. 覆盖渲染输出目录

`video_compose(operation=overlay)` 不会自动创建任意新输出目录。调用方应先创建 `projects/<project>/outputs`，或服务端在渲染前安全地创建父目录，否则会出现 ffmpeg `No such file or directory`。建议统一使用项目内已有的 `renders/` 或 `outputs/` 目录，并在工具入口做 `mkdir(parents=True, exist_ok=True)`。

## 验收用例

使用独立测试项目，不使用正式用户素材：

1. 上传一张 PNG 产品图，确认返回 `success=true`、`asset.relative_path` 和 `asset.id`。
2. 读取该 `relative_path`，确认返回原生图片内容。
3. 使用 `assets/signal-from-tomorrow-demo.mp4` 做一次固定区域覆盖，参数示例：左上角 `(20,20)`，持续前 2 秒。
4. 确认返回 `success=true` 和输出 artifact；用 `ffprobe` 检查视频可读、视频流存在、音频流仍存在。
5. 检查服务日志中没有 traceback，并记录测试项目、输入素材、覆盖区域、输出文件和时间戳，作为替换审计。

## 当前范围边界

本阶段支持：整镜头替换、固定区域产品图覆盖、保留原节奏/字幕/音乐（在编辑决策明确保留时）。

暂不支持：需要逐帧目标跟踪、遮挡关系处理、复杂透视/光照一致性的高级替换。不要把固定 overlay 测试结果表述为高级跟踪已完成。

