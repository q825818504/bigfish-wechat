@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title watcher debug

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

del /q "%ROOT%\state\watermark.json" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\tools\test_watcher_debug.py','filehelper','45' -Verb RunAs -Wait"

echo.
pause
