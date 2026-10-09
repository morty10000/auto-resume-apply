@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0data" mkdir "%~dp0data"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
title Auto Resume Bot - Server

rem ===== locate Python (portable runtime first, then system) =====
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" set "PY=python"

rem ===== sanity: Python must be runnable =====
"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python runtime is not available.
    echo         Portable package: please re-extract the zip completely
    echo         ^(make sure runtime\python.exe exists^).
    echo         Source install: install Python 3.10+ first.
    echo.
    pause
    exit /b 1
)

rem ===== sanity: folder must be writable =====
echo.>"%~dp0data\.write_test" 2>nul
if not exist "%~dp0data\.write_test" (
    echo.
    echo [ERROR] Cannot write to this folder ^(no permission^).
    echo         Please move the project folder to Desktop or Documents and retry.
    echo         ^(Do not run it from C:\Program Files or a read-only drive^)
    echo.
    pause
    exit /b 1
)
del "%~dp0data\.write_test" >nul 2>&1

rem ===== hint: another instance may be running =====
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 echo [INFO] Port 8000 is in use - if the UI does not open, another copy may already be running.

rem ===== launch the UI opener in background (no window) =====
if exist "%~dp0runtime\pythonw.exe" (
    start "" "%~dp0runtime\pythonw.exe" -X utf8 "%~dp0scripts\open_ui.py"
) else (
    start "" /min powershell -NoProfile -WindowStyle Hidden -Command "& '%PY%' -X utf8 '%~dp0scripts\open_ui.py'"
)

rem ===== run the server in this window (keep it open) =====
"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 >> "%~dp0data\server_console.log" 2>&1
echo.
echo Server exited. If unexpected, check: data\server_console.log  /  data\startup.log
pause
