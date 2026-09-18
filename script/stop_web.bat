@echo off
REM 停止 Web 服务（默认端口 8000）
REM 用法: script\stop_web.bat [端口]
setlocal
set PORT=%~1
if "%PORT%"=="" set PORT=8000
echo [stop_web] 查找占用端口 %PORT% 的进程 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    echo [stop_web] 终止 PID %%a
    taskkill /F /PID %%a
)
echo [stop_web] 完成
endlocal
