@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','\"%~dp0wake_uia.py\"' -Verb RunAs -Wait"

echo.
if exist "%~dp0_wake_result.txt" (
    type "%~dp0_wake_result.txt"
)
pause
