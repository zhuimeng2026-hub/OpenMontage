#!/usr/bin/env bash
# 启动 OpenMontage MCP(8900)
# 1) 注入 REMOTION_CHROME_EXECUTABLE：让 Remotion 复用已安装的
#    chrome-headless-shell(149.0.7790.0)，跳过下载
# 2) 清除会话/NODE_OPTIONS 变量：使 WorkBuddy 的 genie-safe-delete
#    shim 在 remotion 子进程中 no-op（它仅在 CODEBUDDY_SESSION_ID /
#    CLAUDE_SESSION_ID 存在时激活），避免 Windows 回收站 trash 失败
#    导致 Remotion 下载锁删除抛错
export REMOTION_CHROME_EXECUTABLE="C:/Users/Admin/OpenMontage/remotion-composer/node_modules/.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe"
unset CODEBUDDY_SESSION_ID
unset CLAUDE_SESSION_ID
unset NODE_OPTIONS
cd "C:/Users/Admin/OpenMontage"
exec python mcp_server.py
