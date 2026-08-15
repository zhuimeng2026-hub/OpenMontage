@echo off
setlocal
set "NG=C:\Users\Admin\OpenMontage\nginx"
set "SITE=%~1"
if "%SITE%"=="" set "SITE=render.mengxa.com.conf"

if exist "%NG%\sites-available\%SITE%" (
    if not exist "%NG%\sites-enabled\%SITE%" (
        copy "%NG%\sites-available\%SITE%" "%NG%\sites-enabled\%SITE%" >nul && echo Enabled: %SITE%
    ) else (
        echo Already enabled: %SITE%
    )
    REM nginx 以 SYSTEM 服务运行，用户会话无法 `nginx -s reload`，改为重启服务进程
    taskkill /F /IM nginx.exe >nul 2>&1
    echo nginx 已重启（SCM 用新配置拉起）
) else (
    echo Not found in sites-available: %SITE%
)
