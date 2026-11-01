@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title bigfish uia live

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

echo ============================================================
echo   bigfish x wechat - UIA LIVE (will really send replies)
echo ============================================================
echo.
echo   Enter the chat name to watch.
echo   WARNING: every incoming message will get an auto reply.
echo   Start with filehelper to test safely.
echo.

set "TARGET="
set /p "TARGET=Chat name: "

if "%TARGET%"=="" (
    echo.
    echo No input. Aborting.
    pause
    exit /b 1
)

echo.
echo Target: %TARGET%
echo Starting live mode. Close the new window or press Ctrl+C to stop.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\bigfish\run_uia.py','--who','%TARGET%' -Verb RunAs -Wait"

echo.
echo Done.
pause
