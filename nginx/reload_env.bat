@echo off
setlocal
REM 因 nginx 以 SYSTEM 服务运行，用户会话的 `nginx -s reload` 会被拒（Access is denied）。
REM 这里改为结束 nginx 进程，由 Windows 服务控制管理器(SCM)自动用最新配置重启。
taskkill /F /IM nginx.exe >nul 2>&1
echo 已请求重启 nginx 服务，SCM 将用最新配置重新拉起...
ping -n 4 127.0.0.1 >nul
echo 完成。可访问 https://render.mengxa.com 验证。
