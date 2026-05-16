@echo off
REM ─── Tailscale Funnel — permanent public URL, no client VPN needed ─────────
REM
REM One-time setup:
REM   1. Install Tailscale on this PC: https://tailscale.com/download
REM   2. Sign in (free personal plan).
REM   3. In a browser go to https://login.tailscale.com/admin/dns
REM      → enable "MagicDNS" if not already on
REM      → note your tailnet name (e.g. "tail1234.ts.net")
REM   4. Enable Funnel for this device:
REM      https://login.tailscale.com/admin/settings/funnel
REM      → tick the checkbox next to this PC's hostname
REM   5. (PowerShell, as Admin) Run once:
REM        tailscale serve --bg --https=443 http://localhost:5003
REM        tailscale funnel 443 on
REM   6. Find your URL:
REM        tailscale status --json | findstr DNSName
REM      It looks like:  https://YOUR-PC-NAME.tail1234.ts.net
REM
REM After step 5, Funnel runs as a background service. You DON'T need this .bat
REM after the first setup — just run start-server.bat and the URL keeps working.
REM Use this script only to (re)apply the Funnel rules if needed.

title Tailscale Funnel setup
cd /d "%~dp0"

where tailscale >nul 2>&1
if errorlevel 1 (
    echo Tailscale not installed. Get it from https://tailscale.com/download
    pause
    exit /b 1
)

echo Configuring Tailscale Funnel for http://localhost:5003 ...
"C:\Program Files\Tailscale\tailscale.exe" serve reset
"C:\Program Files\Tailscale\tailscale.exe" funnel reset
"C:\Program Files\Tailscale\tailscale.exe" funnel --bg --yes 5003
echo.
echo ============================================================
echo   Public URL:  https://l-bhpv5y2.taila40a46.ts.net
echo   Auth token: 6143
echo ============================================================
echo.
"C:\Program Files\Tailscale\tailscale.exe" funnel status
pause
