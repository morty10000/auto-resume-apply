@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0data" mkdir "%~dp0data"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
title Auto Resume Bot - Server
set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" (
    echo [INFO] Portable runtime not found; trying system Python...
    set "PY=python"
)
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "& '%PY%' -X utf8 '%~dp0scripts\open_ui.py'"
"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 >> "%~dp0data\server_console.log" 2>&1
echo.
echo Server exited. Press any key to close.
pause
