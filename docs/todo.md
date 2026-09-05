# TODO

## video_downloader 平台检测白名单补全

- [ ] `tools/analysis/video_downloader.py::_detect_platform()` 当前只识别 youtube / shorts / instagram / tiktok / vimeo / twitter，其他站（包括 weibo、bilibili、douyin、xiaohongshu、kuaishou）一律落到 `"other_url"`。
- [ ] 实际下载不受影响——yt-dlp 对 `[Weibo]` / `[WeiboVideo]` / B 站 / 抖音的提取器都已可用（已实测 weibo 链接成功 31MB mp4）。
- [ ] 缺失的影响：返回里 `platform` 字段对国内短视频站无意义，下游 agent 无法靠该字段走分支逻辑（例如按平台选不同的 metadata 提取策略或合规标记）。
- [ ] 补全方法：扩 `_detect_platform()` 的 URL 匹配链，覆盖至少 weibo（`weibo.com` / `video.weibo.com`）、bilibili（`bilibili.com` / `b23.tv`）、douyin（`douyin.com` / `v.douyin.com`）、xiaohongshu（`xiaohongshu.com` / `xhslink.com`）、kuaishou（`kuaishou.com` / `v.kuaishou.com`）。
- [ ] 建议同时给 `VideoDownloader` 补一组针对国内站的 `tests/regression/` 测试用例：每个平台 1 个 fixture URL，验证 `execute()` 返回的 `platform` 字段。
- [ ] 回归验证：用 video.weibo.com 的真实链接和 B 站视频各跑一次 `video_downloader`，确认 metadata + 下载产物都正常。

## video_downloader 进度回传到 MCP 客户端

- [ ] 现状：`tools/analysis/video_downloader.py::execute()` 阻塞 `yt_dlp.YoutubeDL(ydl_opts).download([url])`，期间不向 MCP 客户端发任何进度事件，`ydl_opts` 也开了 `quiet=True` 屏蔽 yt-dlp 自身输出。
- [ ] 影响：长视频（数百 MB / 慢网 / 10+ 分钟）下载时客户端 HTTP 连接挂死，看不到百分比、速度、ETA；只有最终 `ToolResult` 一次性返回。
- [ ] 应用已有现成基础设施，不需要重建：`lib/render_progress.py` 的 `publish` / `progress_event` 总线（事件形如 `{"event":"render_progress","phase":...,"percent":...,"ts":...}`）、`mcp_server.py` 已有的 `_progress_callback` 注入（render 管线 `mcp_server.py:2229` 在用）、`BaseTool.run_command(on_output=...)` 流式行回调、SSE 端点对外广播。
- [ ] 工具内改造：给 `video_downloader.py` 的 `ydl_opts` 加 `progress_hooks=[...]`，回调里读 `d["downloaded_bytes"]` / `d["total_bytes"]` / `d["speed"]` / `d["eta"]` / `d["status"]`，通过 `inputs.get("_progress_callback")`（如果存在）转发到 `progress_event(job_id, phase="download", percent=..., message="X.X MB/s", extra={"eta":...})`。
- [ ] MCP 层改造：`mcp_server.py` 当前仅在 render 流水线（`source_ingest` 之外的工具）注入 `_progress_callback`，需要扩展到 `capability="source_ingest"` 的工具（含 `video_downloader`），并为其分配 `job_id` 让客户端能 SSE 订阅。
- [ ] 客户端：现有 SSE 通道已能订阅 `render_progress` 事件，扩展事件 `phase` 枚举增加 `"download"` 即可，前端无需改动传输层。
- [ ] 回归验证：用一个 200 MB+ 的视频跑 `video_downloader`，确认客户端 SSE 流上至少收到 5 个进度事件（<20% / 20-50% / 50-80% / 80-100% / finished），且最终 `video_path` 产物一致；再跑一个失败用例确认错误路径仍正常返回 `ToolResult(success=False, ...)`，不被进度事件干扰。

## MCP 分析产物下载

- [ ] 当用户需要复核或二次编辑参考视频时，支持通过同一 MCP 会话下载远端分析产物到本机 `docs/`。
- [ ] 需要覆盖：`scene_detect.json`、`video_analysis_brief.json`、关键帧图片，以及后续生成的脚本和场景计划。
- [ ] 当前阻塞：远端 `192.168.20.173:8900/mcp` 的工具列表虽显示 `read_session_asset`，实际调用返回 `read_session_asset tool is not registered`。
- [ ] 在服务器端注册并部署 `read_session_asset`（或等价的安全文件读取接口），完成权限、路径白名单和会话隔离验证。
- [ ] 部署完成后，用 B 站测试视频 `BV12oGu6uEX8` 回归验证上传、分解、产物下载全链路。
