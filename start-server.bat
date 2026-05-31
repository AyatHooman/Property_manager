@echo off
REM ─── Property Manager — Flask server launcher ──────────────────────────────
title Property Manager Server
cd /d "%~dp0"

REM Load the shared access token from the local (gitignored) secrets file.
REM Copy .secrets.bat.example to .secrets.bat and set your real token there.
if exist "%~dp0.secrets.bat" call "%~dp0.secrets.bat"

REM Bind to localhost only. Remote access is via Tailscale Serve (tailnet-only),
REM which proxies localhost — so we never expose the port on 0.0.0.0 / the LAN.
set HOST=127.0.0.1
set PORT=5005

echo Starting Property Manager on http://%HOST%:%PORT%
echo.

C:\Users\z5194283\.conda\envs\geo_env\python.exe -m src.web
pause
