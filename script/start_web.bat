@echo off
REM 启动 Web 服务（含定时任务）
REM 用法: script\start_web.bat [端口]
cd /d %~dp0\..
set PY=.venv\Scripts\python.exe
if not exist %PY% set PY=python
echo [start_web] 启动 http://0.0.0.0:%2  ...
if "%~1"=="" (
    %PY% -m src.web.app
) else (
    %PY% -m src.web.app --port %~1
)
pause
