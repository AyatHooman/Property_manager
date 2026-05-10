@echo off
REM Stops Flask + cloudflared on this machine
title Stop Property Manager
echo Stopping Python (Flask) and cloudflared...
taskkill /F /IM python.exe       >nul 2>&1
taskkill /F /IM cloudflared.exe  >nul 2>&1
echo Done.
timeout /t 2 >nul
