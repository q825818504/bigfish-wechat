@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title EXE live send

set "EXE=%~dp0..\dist\BigFish\BigFish.exe"

if not exist "%EXE%" (
    echo [ERROR] BigFish.exe not found:
    echo         %EXE%
    echo Build it first: packaging\build.bat
    pause
    exit /b 1
)

echo Running BigFish.exe --live-send as administrator...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"$p = Start-Process -FilePath '%EXE%' -ArgumentList '--live-send' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"

echo.
echo Exit code: %errorlevel%
pause
