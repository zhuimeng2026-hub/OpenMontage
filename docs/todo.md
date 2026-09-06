# TODO

## video_downloader 工作区隔离 + 命名冲突防护

- [x] ~~`output_dir` 路径校验~~ — 已升级到 ProjectWorkspace 模式（见下方），不再需要手写校验。
- [x] ~~`outtmpl` URL hash 防冲突~~ — `reference_video_<sha1(url)[:8]>.%(ext)s` 已落地。
- [x] **复用 upload_asset 的 user-id 自动注入模式（2026-09-05 完成）**：
  - 删 `_validate_output_dir`（ProjectWorkspace.resolve() 接管）
  - 删 `output_dir` / `userid` 必填
  - 新增 `project_id`（默认 `"references"`，匹配 `reference_input` 语义）— 同样不允许下划线开头（`sanitize_project_id` 拒绝）
  - `_resolve_workspace` 走 `ProjectWorkspace.for_current_principal(project_id)` 优先（MCP 路径，userid 从 session ContextVar 取，来自 WeChat → X-VClaw-User-Id）
  - 非 MCP 路径（测试/脚本）显式传 `userid`，落到 `ProjectWorkspace.for_principal(Principal(...), project_id)`
  - 两种路径都过 `workspace.resolve(output_dir)`，symlink/路径穿越在那一层被拒
  - ToolResult 新增 `workspace_root` 字段回显路径，caller 不用再算
  - 测试改用 `patch.object(paths, 'PROJECTS_DIR', td)` 把真实 `for_principal` 引导到 tempdir，不再 mock 整个 workspace
- [x] **回归测试** `tests/tools/test_video_analyzer_workspace.py` 10 个 test 全过：`tests/tools/test_video_analyzer_workspace.py` covers 默认 project_id=userid 透传、output_dir 默认在 workspace 下、显式 output_dir 越界拒绝、子路径接受、VideoDownloader/Transcriber/FrameSampler 都收到 userid+project_id+相对路径。
- [x] **video_analyzer 级联修复（2026-09-05）**：
  - `video_analyzer.py:165-169` 入口的 `output_dir` 默认从 `Path("projects/_analysis") / f"analysis_<ts>"`（不在任何 user namespace）改成 `workspace.root / f"analysis_{int(time.time())}"`
  - 给 input_schema 加 `project_id`（默认 `"references"`，匹配 video_downloader）
  - 5 处 `.execute()` 调子工具的 call site（`VideoDownloader` × 2、`Transcriber`、`FrameSampler` × 2）都显式传 `userid` + `project_id` + 相对路径（`output_dir.relative_to(workspace.root)`）
  - video_analyzer 自身走 `ProjectWorkspace.for_current_principal(project_id)` 优先；非 MCP 路径走 `Principal(...).for_principal(...)` 兜底
  - 修了 `tests/tools/test_scene_detect_long_video.py::test_video_analyzer_retains_and_reports_degraded_scenes` 以适配新的 workspace 契约
- [x] **C 类顺手做掉（2026-09-05）**：
  - `video_compose.py`：input_schema 加 `project_id`（默认 `"renders"`）+ `userid`（optional fallback）；加 `_resolve_workspace()` 静态方法；`execute()` 入口 stash `_resolved_userid` / `_resolved_project_id`；render 路径 `hf_inputs` 加 `userid` + `project_id`；remotion_bilingual_overlay 路径 `SubtitleGen().execute({...})` 加同样两键
  - `auto_reframe.py`：input_schema 加 `project_id`（默认 `"reframes"`）+ `userid`；`execute()` 入口 inline workspace resolution（同 video_analyzer 模式）；`FaceTracker().execute({...})` 加 `userid` + `project_id`
  - `asset_upload_chunk.py:29` 检查确认只 import 常量 + class，没调 `.execute()`，不需改
  - `video_compose.py:434` 的 `HyperFramesCompose()._runtime_check()` 不调 `.execute()`，不需改
  - 测试 `tests/tools/test_video_compose_auto_reframe_workspace.py`（8 个 test）：workspace 解析 3 + execute stash 1 + HF/SG inputs 契约 2 + AutoReframe no-principal 1 + FaceTracker cascade 1
- [ ] **未触动（设计留口）**：
  - `video_compose.py:1425-1475` 的 `workspace_path` 当前是相对路径 `output_path.parent.parent / "hyperframes"`。这次没改它，因为 HyperFramesCompose 还没加 workspace 校验；将来若 HF 改用 ProjectWorkspace，这块需要升级为 `ws.root / "hyperframes"` 之类绝对路径
  - `video_compose.py:2095` 的 SubtitleGen tmpdir 是有意保留的"ephemeral"中间产物；本次只传 userid 让未来 pattern 一致，不动 tmpdir 位置

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
