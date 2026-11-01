@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title bigfish x wechat

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

if not exist "%PY%" (
    echo [ERROR] venv not found: %PY%
    pause
    exit /b 1
)
if not exist "%ROOT%\config.json" (
    echo [ERROR] config.json not found.
    echo Copy config.example.json to config.json and edit auto_reply.chats
    pause
    exit /b 1
)

echo ============================================================
echo   bigfish x wechat
echo ============================================================
echo.
echo   Dry run first? (generate but do not send)
echo   Press D for dry run, any other key for live.
echo.
choice /c DL /n /m "Dry(D) or Live(L)? "

if errorlevel 2 goto LIVE
if errorlevel 1 goto DRY

:DRY
echo.
echo === DRY RUN ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\bigfish\app.py','--dry' -Verb RunAs -Wait"
goto END

:LIVE
echo.
echo === LIVE ===
echo Every incoming message in the watched chats gets an auto reply.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"Start-Process -FilePath '%PY%' -ArgumentList '-X','utf8','%ROOT%\bigfish\app.py' -Verb RunAs -Wait"
goto END

:END
echo.
pause
