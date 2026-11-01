@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

if not exist "%PY%" (
    echo [ERROR] venv not found
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\tools\live_send_test.py' -Verb RunAs -Wait"

echo.
if exist "%~dp0_live_result.txt" ( type "%~dp0_live_result.txt" )
pause
