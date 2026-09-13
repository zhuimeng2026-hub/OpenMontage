@echo off
setlocal
REM 停止 BFF
taskkill /FI "WINDOWTITLE eq OpenMontage-BFF*" >nul 2>&1
echo BFF 已停止。

REM 停止 MCP 服务（python mcp_server.py）
taskkill /FI "WINDOWTITLE eq OpenMontage-MCP*" >nul 2>&1
tasklist /FI "IMAGENAME eq python.exe" | find /I "python.exe" >nul 2>&1
if not errorlevel 1 (
  echo 仍有 python 进程（mcp_server.py），按命令行匹配结束...
  for /f "tokens=2" %%p in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| find /I "PID:"') do taskkill /PID %%p >nul 2>&1
)
echo MCP 服务已停止。

echo.
echo 注意：nginx 以 Windows 服务运行并开机自启。如需停止 nginx，
echo 请在“服务”控制台（services.msc）停止/禁用 nginx 服务。
echo （本机安全策略禁用了 sc/net，taskkill 结束后服务会被 SCM 自动拉起）
pause
