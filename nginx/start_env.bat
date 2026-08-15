@echo off
setlocal
set "NGINX=C:\tools\nginx-1.31.3\nginx.exe"
set "NG=C:\Users\Admin\OpenMontage\nginx"
set "BFF_DIR=C:\Users\Admin\OpenMontage\frameflow\bff"
set "BFF=%BFF_DIR%\frameflow-bff"

REM 原二进制无扩展名，cmd 无法直接运行，复制出一份 .exe
if not exist "%BFF%.exe" copy "%BFF%" "%BFF%.exe" >nul
set "BFF_EXE=%BFF%.exe"

REM 启动 BFF（nginx 已作为 Windows 服务自启，无需在此启动）
start "OpenMontage-BFF" /D "%BFF_DIR%" "%BFF_EXE%"
echo BFF 已启动，监听 8080

REM 启动 MCP 服务（streamable-http，监听 8900，供 BFF 调用图生视频等工具）
set "OM_ROOT=C:\Users\Admin\OpenMontage"
set "PY=python"
tasklist /FI "IMAGENAME eq python.exe" | find /I "python.exe" >nul 2>&1
if errorlevel 1 (
  start "OpenMontage-MCP" /D "%OM_ROOT%" cmd /c "%PY% mcp_server.py ^> %NG%\logs\mcp.out.log 2^>^&1"
  echo MCP 服务已启动，监听 8900
) else (
  echo 检测到 python 进程，MCP 服务可能已在运行；如需重启请先运行 stop_env.bat
)

echo.
echo nginx 由 Windows 服务托管（开机自启），直接访问 https://render.mengxa.com
echo 完整链路：浏览器 -> nginx(443) -> BFF(8080) -> MCP(8900)
echo 若修改了站点配置，请运行 reload_env.bat 使其生效。
echo （浏览器对自签证书会提示不安全，点“继续/高级-继续访问”即可）
pause
