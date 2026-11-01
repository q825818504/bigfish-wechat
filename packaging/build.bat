@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."
title bigfish build

set "PY=%~dp0..\.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "OUTNAME=BigFish"

echo ============================================================
echo   bigfish build
echo ============================================================
echo.

if not exist "%PY%" (
    echo [ERROR] venv not found: %PY%
    pause
    exit /b 1
)

echo [1/4] clean ...
if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build"
if exist "%ROOT%\dist" rmdir /s /q "%ROOT%\dist"

echo [2/4] pyinstaller ...
"%PY%" -m PyInstaller --noconfirm --clean ^
  --distpath "%ROOT%\dist" ^
  --workpath "%ROOT%\build" ^
  "%ROOT%\packaging\bigfish.spec"
if errorlevel 1 (
    echo [ERROR] pyinstaller failed
    pause
    exit /b 1
)

echo.
echo [3/4] copy external resources ...
set "OUT=%ROOT%\dist\%OUTNAME%"
if not exist "%OUT%" (
    echo [ERROR] dist folder not found: %OUT%
    dir /b "%ROOT%\dist"
    pause
    exit /b 1
)

if not exist "%OUT%\ui" mkdir "%OUT%\ui"
for %%F in (index.html style.css app.js qwebchannel.js icon.png icon.ico) do (
    if exist "%ROOT%\ui\%%F" copy /y "%ROOT%\ui\%%F" "%OUT%\ui\" >nul
)

if not exist "%OUT%\bridge" mkdir "%OUT%\bridge"
for %%F in ("%ROOT%\bridge\*.mjs") do copy /y "%%F" "%OUT%\bridge\" >nul

if exist "%ROOT%\config.example.json" copy /y "%ROOT%\config.example.json" "%OUT%\" >nul
if exist "%ROOT%\README.md" copy /y "%ROOT%\README.md" "%OUT%\" >nul

echo [4/4] done
echo.
echo   output: %OUT%
echo.
dir /b "%OUT%"
echo.
echo next (optional): compile installer with Inno Setup
echo   iscc packaging\installer.iss
echo.
pause
