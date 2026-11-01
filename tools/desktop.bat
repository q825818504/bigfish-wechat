@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title bigfish desktop

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

if not exist "%PY%" (
    echo [ERROR] venv not found: %PY%
    pause
    exit /b 1
)

"%PY%" -X utf8 "%ROOT%\bigfish\desktop.py"
pause
