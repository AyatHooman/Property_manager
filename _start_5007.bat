@echo off
title Property Manager Server 5007
cd /d "%~dp0"
if exist "%~dp0.secrets.bat" call "%~dp0.secrets.bat"
set HOST=127.0.0.1
set PORT=5007
echo Starting Property Manager on http://%HOST%:%PORT%
C:\Users\z5194283\.conda\envs\geo_env\python.exe -m src.web
pause
