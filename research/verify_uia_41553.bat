@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','\"%~dp0verify_uia_41553.py\"' -Verb RunAs -Wait"

echo.
if exist "%~dp0_uia_41553.txt" (
    type "%~dp0_uia_41553.txt"
)
pause
