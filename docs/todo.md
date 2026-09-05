# TODO

## MCP 分析产物下载

- [ ] 当用户需要复核或二次编辑参考视频时，支持通过同一 MCP 会话下载远端分析产物到本机 `docs/`。
- [ ] 需要覆盖：`scene_detect.json`、`video_analysis_brief.json`、关键帧图片，以及后续生成的脚本和场景计划。
- [ ] 当前阻塞：远端 `192.168.20.173:8900/mcp` 的工具列表虽显示 `read_session_asset`，实际调用返回 `read_session_asset tool is not registered`。
- [ ] 在服务器端注册并部署 `read_session_asset`（或等价的安全文件读取接口），完成权限、路径白名单和会话隔离验证。
- [ ] 部署完成后，用 B 站测试视频 `BV12oGu6uEX8` 回归验证上传、分解、产物下载全链路。
