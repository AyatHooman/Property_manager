@echo off
title Property Manager Server 5007
cd /d "%~dp0"
if exist "%~dp0.secrets.bat" call "%~dp0.secrets.bat"
set HOST=127.0.0.1
set PORT=5007
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
) else (
    where py >nul 2>&1 && (set "PY=py") || set "PY=python"
)
echo Starting Property Manager on http://%HOST%:%PORT%
"%PY%" -m src.web
pause
