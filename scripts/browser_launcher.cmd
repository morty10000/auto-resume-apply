@echo off
setlocal
cd /d "%~dp0.."

rem Browser shortcut entry: start the project if needed, then let start.bat
rem reuse the dedicated Edge window and open the dashboard tab.
powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~dp0..\start.bat' -WindowStyle Hidden"
exit /b 0
