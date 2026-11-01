@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title EXE selftest

set "EXE=%~dp0..\dist\BigFish\BigFish.exe"

if not exist "%EXE%" (
    echo [ERROR] BigFish.exe not found
    pause
    exit /b 1
)

echo Running BigFish.exe --selftest as administrator...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"$p = Start-Process -FilePath '%EXE%' -ArgumentList '--selftest' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"

echo.
echo Exit code: %errorlevel%
pause
