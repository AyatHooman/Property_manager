@echo off
REM ─── Tailscale Serve — TAILNET-ONLY HTTPS proxy (NOT public funnel) ────────
REM
REM This proxies http://localhost:5005 to https://<this-pc>.<tailnet>.ts.net
REM but only devices signed into YOUR tailnet can reach it. Nothing is exposed
REM to the public internet — no Funnel, no LAN bind, no port-forward.
REM
REM One-time setup:
REM   1. Install Tailscale on this PC: https://tailscale.com/download
REM   2. Sign in (free personal plan).
REM   3. In a browser go to https://login.tailscale.com/admin/dns
REM      and enable "MagicDNS" if not already on.
REM   4. Make sure Funnel is OFF for this device:
REM      https://login.tailscale.com/admin/settings/funnel
REM   5. Run this script once (regular cmd, no admin needed).
REM
REM After step 5, Tailscale Serve runs as a background service. You DON'T need
REM this .bat after the first setup — just run start-server.bat and the URL
REM keeps working from any device signed into your tailnet.

title Tailscale Serve setup (tailnet-only)
cd /d "%~dp0"

where tailscale >nul 2>&1
if errorlevel 1 (
    echo Tailscale not installed. Get it from https://tailscale.com/download
    pause
    exit /b 1
)

echo Resetting any existing Funnel / Serve config (so we start clean)...
"C:\Program Files\Tailscale\tailscale.exe" funnel reset
"C:\Program Files\Tailscale\tailscale.exe" serve reset

echo Configuring Tailscale Serve (tailnet-only) for http://localhost:5005 ...
"C:\Program Files\Tailscale\tailscale.exe" serve --bg --https=443 http://localhost:5005

echo.
echo ============================================================
echo   Tailnet-only HTTPS URL is now active.
echo   Find your URL with: tailscale status --json
echo   (look for DNSName for this device, e.g.
echo    https://your-pc.tailXXXX.ts.net )
echo.
echo   Only devices signed into YOUR tailnet can reach it.
echo   Funnel (public internet) is OFF.
echo ============================================================
echo.
"C:\Program Files\Tailscale\tailscale.exe" serve status
pause
