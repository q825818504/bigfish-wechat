@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title bigfish uia dry

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

echo ============================================================
echo   bigfish x wechat - UIA dry run (no message will be sent)
echo ============================================================
echo.
echo   Enter the chat name to watch. Use filehelper to test safely.
echo.

set "TARGET="
set /p "TARGET=Chat name: "

if "%TARGET%"=="" (
    echo No input. Aborting.
    pause
    exit /b 1
)

echo.
echo Target: %TARGET%
echo Starting... send a message in WeChat to trigger a reply.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\bigfish\run_uia.py','--who','%TARGET%','--dry','--allow-self' -Verb RunAs -Wait"

echo.
pause
