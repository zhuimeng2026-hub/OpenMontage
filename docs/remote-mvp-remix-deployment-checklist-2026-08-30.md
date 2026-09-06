# 远端素材替换 MVP 部署清单（2026-08-30）

目标：让测试环境 `http://192.168.20.173:8900/mcp` 与本机当前代码保持一致，并可验证“上传产品图 → 固定区域覆盖 → 输出新视频”的链路。

## 当前验证结论

- `upload_asset`：✅ 已验证成功，测试项目：`mvp-smoke-20260830`、`verify-fix-20260830`。
- `video_compose(operation=overlay)`：✅ 已验证成功，生成：`projects/mvp-smoke-20260830/logo_overlay.mp4`（30.06s，H.264 + AAC 双流）。
- `read_session_asset_image`：✅ 已验证成功，`get_tool_info` 返回完整 schema，`tools/call` 返回 MCP 原生 `image` content（type=image, mimeType=image/png, has_data=True），不再是“tool is not registered”。
- 原始视频的自动拆解、字幕/音频保留、整镜头替换和高级跟踪未在本次 smoke test 中宣称完成；先完成下列部署同步。

## 远端需要处理的事项

### 1. 同步并重启服务 — ✅

工作树已与 `upstream` 同步至 `22fe69f`（含 `acd3ee5` 的 `read_session_asset_image` 工具与 `b07bbb0` 的部署纪要）。MCP 服务在本地工作树变更后重启于 2026-08-30 11:10:04，新 PID `2592627`，cwd = `/opt/OpenMontage_Voicebox`，加载的正是当前工作树（`tools/asset/__init__.py` 已存在，`registry.discover()` 现在能找到 `tools.asset.read_session_asset_image`）。

### 2. 确认 MCP 工具注册 — ✅

通过新 session `836b4c3cc14144f2934d80f96e944911` + Bearer token 实测：

- `get_tool_info(tool_name="read_session_asset_image")` → 返回完整 schema
  ```json
  {
    "name": "read_session_asset_image",
    "version": "1.0.0",
    "tier": "core",
    "capability": "asset_management",
    "provider": "openmontage",
    "stability": "production",
    "status": "available",
    "module_path": "tools.asset.read_session_asset_image",
    "input_schema": { "required": ["relative_path"], ... }
  }
  ```
- `read_session_asset_image(relative_path="projects/verify-fix-20260830/assets/_sessions/cca8c3d3bb970e12/logo.png")` → `result.content[0]` 是 `type=image`、`mimeType=image/png`、`data` 170388 字节 base64（PNG），`isError=false`。

### 3. 修正上传脚本的地址配置 — ✅

`scripts/mcp_upload.py` 现在读 `OPENMONTAGE_MCP_URL` 环境变量，默认值仍为 `http://localhost:8900/mcp`：

```python
MCP_URL = os.environ.get("OPENMONTAGE_MCP_URL", "http://localhost:8900/mcp")
...
req = urllib.request.Request(MCP_URL, ...)  # 所有 mcp_call() 都走 MCP_URL
```

运行 `OPENMONTAGE_MCP_URL=http://192.168.20.173:8900/mcp python3 scripts/mcp_upload.py --project ...` 即可打到远端，不再连本机 8900。docstring 与实现现在一致。

### 4. 覆盖渲染输出目录 — ✅

`tools/video/video_compose.py:_overlay`（line 2944）在解析完 `output_path`、通过输入校验之后、构造 ffmpeg 命令之前，加了：

```python
# Defensive mkdir: callers don't always pre-create the project's
# `renders/` or `outputs/` tree, and ffmpeg refuses to write into a
# missing parent directory. ``exist_ok=True`` keeps idempotent
# re-renders safe.
output_path.parent.mkdir(parents=True, exist_ok=True)
```

这样在新项目首次跑 overlay 时也会自动创建父目录，不再有 `No such file or directory`。

## 验收用例

使用独立测试项目，不适用正式用户素材：

1. 上传一张 PNG 产品图，确认返回 `success=true`、`asset.relative_path` 和 `asset.id`。
   ✅ 实测：上传 `assets/social_preview.png` 到 `verify-fix-20260830`，响应 `success=true`、`asset.id=verify-fix-20260830-b4ed8de2be8d`、`asset.relative_path=projects/verify-fix-20260830/assets/_sessions/cca8c3d3bb970e12/logo.png`、`asset.mime_type=image/png`、`asset.bytes=127790`、`asset.sha256=b4ed8de2...`。
2. 读取该 `relative_path`，确认返回原生图片内容。
   ✅ 实测：`read_session_asset_image(relative_path=...)` 返回 `content[0]` 为 `type=image` / `mimeType=image/png` / `data=170388B`，详见 §2。
3. 使用 `assets/signal-from-tomorrow-demo.mp4` 做一次固定区域覆盖，参数示例：左上角 `(20,20)`，持续前 2 秒。
   ✅ 实测：已生成 `projects/mvp-smoke-20260830/logo_overlay.mp4`，参数 `x=20, y=20, scale=0.25, start=0, duration=2`，服务日志记录 `success=True duration=9.62s / 9.79s`（10:54:46 / 10:55:09）。
4. 确认返回 `success=true` 和输出 artifact；用 `ffprobe` 检查视频可读、视频流存在、音频流仍存在。
   ✅ `ffprobe -v error -show_streams projects/mvp-smoke-20260830/logo_overlay.mp4` 结果：
   - format_name = mov,mp4,m4a,3gp,3g2,mj2（可读）
   - video_streams = 1，codec_name = h264，1920×1080，30fps
   - audio_streams = 1，codec_name = aac，sample_rate = 48000Hz，stereo
   - duration = 30.06s
   与源视频流数一致。
5. 检查服务日志中没有 traceback，并记录测试项目、输入素材、覆盖区域、输出文件和时间戳，作为替换审计。
   ✅ `logs/mcp_server.log` 最近 30 行无 traceback；记录了项目 `mvp-smoke-20260830`、输入 `assets/signal-from-tomorrow-demo.mp4`、覆盖参数 `x=20 y=20 scale=0.25 start_time=0 duration=2`、输出 `projects/mvp-smoke-20260830/logo_overlay.mp4`、时间戳 10:54:46 / 10:55:09。

## 当前范围边界

本阶段支持：整镜头替换、固定区域产品图覆盖、保留原节奏/字幕/音乐（在编辑决策明确保留时）。

暂不支持：需要逐帧目标跟踪、遮挡关系处理、复杂透视/光照一致性的高级替换。不要把固定 overlay 测试结果表述为高级跟踪已完成。

## 修复 commit

见紧邻本文件的前一次提交（`git log --oneline docs/remote-mvp-remix-deployment-checklist-2026-08-30.md` 的第一个结果），由本机基线 `22fe69f` 演进而来，含三处代码修复：

- `tools/asset/__init__.py`（新增空文件）— 让 `pkgutil.iter_modules(tools)` 发现 `tools.asset` 子包，从而使 `tool_registry.discover()` 注册 `ReadSessionAsset` 与 `ReadSessionAssetImage` 两个 BaseTool。修复前 `tools/list` 看得到 MCP 包装器，但 `registry.get(...)` 返回 `None`，任何依赖 BaseTool 的调用都失败。
- `scripts/mcp_upload.py` — 新增模块级常量 `MCP_URL = os.environ.get("OPENMONTAGE_MCP_URL", "http://localhost:8900/mcp")`，`mcp_call()` 的 `Request` URL 从硬编码字符串改为 `MCP_URL`。
- `tools/video/video_compose.py:_overlay` — 在输入校验通过后、`cmd` 构造前加 `output_path.parent.mkdir(parents=True, exist_ok=True)`，对应清单 §4 与验收 #3 在新项目上的可重复性。