@echo off
REM ─── Property Manager — Flask server launcher ──────────────────────────────
title Property Manager Server
cd /d "%~dp0"

REM Optional: set a shared access token. Anyone using the public URL must enter
REM this. Comment the line out to disable auth (anyone with the link gets in).
set AUTH_TOKEN=6143

REM Bind to all interfaces so Cloudflare Tunnel (or your LAN) can reach it
set HOST=0.0.0.0
set PORT=5003

echo Starting Property Manager on http://%HOST%:%PORT%
echo Auth token: %AUTH_TOKEN%
echo.

C:\Users\z5194283\.conda\envs\geo_env\python.exe -m src.web
pause
