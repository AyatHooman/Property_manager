@echo off
REM ─── Property Manager — HARD restart ───────────────────────────────────────
REM Kills every python.exe + every minimized server cmd window, then relaunches
title Property Manager Restart
cd /d "%~dp0"

echo.
echo ============================================
echo   Property Manager - HARD RESTART
echo ============================================
echo.

REM ── 1. Kill any cmd window whose title is "Property Manager Server" ─────────
echo [1/4] Closing existing server windows...
taskkill /F /FI "WINDOWTITLE eq Property Manager Server*" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Administrator: Property Manager Server*" /T >nul 2>&1

REM ── 2. Brutally kill every python.exe (we own all of them in this workflow)
echo [2/4] Killing all python.exe processes...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM pythonw.exe /T >nul 2>&1

REM Give Windows a moment to release the TCP port
timeout /t 2 /nobreak >nul

REM ── 3. Show current port state for sanity ──────────────────────────────────
echo [3/4] Port state after kill:
netstat -ano | findstr "LISTENING" | findstr ":500"
if errorlevel 1 echo    (no python listeners on 5000-5009 - clean)

REM ── 4. Launch a fresh detached server window ───────────────────────────────
echo.
echo [4/4] Launching fresh server...
start "Property Manager Server" /MIN cmd /k "%~dp0start-server.bat"

REM Poll for up to 20 seconds, checking every second
echo.
echo Waiting for server to bind to port 5002...
set /a tries=0
:waitloop
set /a tries+=1
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":5002.*LISTENING" >nul
if not errorlevel 1 goto serverup
if %tries% geq 60 goto serverdown
set /a mod=%tries% %% 5
if %mod%==0 echo    ... still waiting (%tries%s)
goto waitloop

:serverup
echo.
echo    [OK] Server is LISTENING on port 5002 (took %tries%s)
echo.
echo    Local:  http://127.0.0.1:5002/?token=6143
echo    Public: https://l-bhpv5y2.taila40a46.ts.net/?token=6143
goto done

:serverdown
echo.
echo    [!] WARNING: Server did not bind to :5002 within 60s
echo        Check the minimized "Property Manager Server" window for errors

:done
echo.
echo Done. This window will close in 5 seconds...
timeout /t 5 /nobreak >nul
exit /b 0
