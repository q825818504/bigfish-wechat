@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title watcher test

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\tools\test_watcher.py','filehelper','40' -Verb RunAs -Wait"

echo.
pause
