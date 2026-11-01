@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title EXE bigfish UI

set "EXE=%~dp0..\dist\BigFish\BigFish.exe"

if not exist "%EXE%" (
    echo [ERROR] BigFish.exe not found
    pause
    exit /b 1
)

echo Starting BigFish.exe as administrator...
echo The GUI window will open. Use the power button to start/stop.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%EXE%' -Verb RunAs"

