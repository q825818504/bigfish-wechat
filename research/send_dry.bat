@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','\"%~dp0test_send_uia.py\"','--dry' -Verb RunAs -Wait"

echo.
if exist "%~dp0_send_test.txt" ( type "%~dp0_send_test.txt" )
pause
