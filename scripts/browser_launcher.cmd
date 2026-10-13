@echo off
setlocal
cd /d "%~dp0.."

rem Browser shortcut entry: start the project if needed, then let start.bat
rem reuse the dedicated Edge window and open the dashboard tab.
start "" /min "%~dp0..\start.bat"
exit /b 0