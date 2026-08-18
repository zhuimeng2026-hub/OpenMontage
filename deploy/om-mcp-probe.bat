@echo off
REM ===========================================================================
REM OpenMontage 监控进程托管（Windows 等效方案）
REM ---------------------------------------------------------------------------
REM 背景：systemd 只存在于 Linux。本机开发机是 Windows，无法用 .service 单元。
REM       这里的等价做法是「keep-alive 包装脚本 + 计划任务开机自启」：
REM         1) 本脚本循环拉起 om_mcp_probe，进程退出即记录并自动重启（= systemd Restart=always）
REM         2) 用下面的 schtasks 命令把本脚本注册为开机自启（= systemd WantedBy=multi-user.target）
REM
REM 注册为开机自启（以 SYSTEM 身份、无登录也运行，对应 systemd 常驻）：
REM   schtasks /create /tn "OpenMontageMonitor" /tr "C:\Users\Admin\OpenMontage\deploy\om-mcp-probe.bat" /sc onstart /ru SYSTEM /rl highest
REM 查看 / 删除：
REM   schtasks /query /tn "OpenMontageMonitor"
REM   schtasks /delete /tn "OpenMontageMonitor" /f
REM
REM 若不想用计划任务，也可「始终开机登录」场景下直接把本 .bat 丢进
REM   开始菜单 -> 启动 文件夹，但计划任务更贴近 systemd 的常驻语义。
REM ===========================================================================

SETLOCAL
SET "REPO=C:\Users\Admin\OpenMontage"
SET "PY=%REPO%\.venv\Scripts\python.exe"
SET "LOG=%REPO%\logs\om_mcp_probe_keepalive.log"
SET "PROBE_PORT=9099"
SET "TARGET=http://localhost:8900/mcp"
SET "ROLE=all"

IF NOT EXIST "%REPO%\logs" mkdir "%REPO%\logs"

:loop
REM 清掉沙箱会话变量，避免 genie-safe-delete 钩子 fail-closed 误伤（与 start_mcp_server.sh 一致）
SET "CODEBUDDY_SESSION_ID="
SET "CLAUDE_SESSION_ID="
SET "NODE_OPTIONS="

echo [%date% %time%] starting om_mcp_probe (role=%ROLE% target=%TARGET% serve=0.0.0.0:%PROBE_PORT%) >> "%LOG%"
"%PY%" "%REPO%\om_mcp_probe.py" status --role %ROLE% --target %TARGET% --serve 0.0.0.0:%PROBE_PORT%
SET "RC=%ERRORLEVEL%"
echo [%date% %time%] om_mcp_probe exited rc=%RC%, restart in 3s >> "%LOG%"

REM 退出后稍等再拉起（对应 systemd RestartSec=3）
timeout /t 3 /nobreak >nul
GOTO loop
