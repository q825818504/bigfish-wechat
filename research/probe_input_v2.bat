@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','\"%~dp0probe_input_v2.py\"' -Verb RunAs -Wait"

echo.
if exist "%~dp0_input_probe2.txt" ( type "%~dp0_input_probe2.txt" )
pause
