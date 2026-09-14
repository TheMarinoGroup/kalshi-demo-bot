@echo off
setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"
if not exist data mkdir data
set HUD_URL=http://127.0.0.1:8080/api/health
set HUD_TITLE=kalshi-paper-hud
set LOG=data\hud-watchdog.log
echo Watchdog: desk http://127.0.0.1:8080
echo If HUD is not healthy on :8080, restarts python -m kalshi_pbot hud
echo Ctrl+C stops the watchdog (the HUD window may keep running).
echo %date% %time% watchdog start>> "%LOG%"
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://127.0.0.1:8080/"

:loop
call :healthy
if errorlevel 1 (
  echo %date% %time% hud unhealthy - restarting>> "%LOG%"
  echo %date% %time% HUD not healthy on :8080 - restarting
  taskkill /FI "WINDOWTITLE eq %HUD_TITLE%*" /T /F >nul 2>&1
  timeout /t 2 /nobreak >nul
  start "%HUD_TITLE%" cmd /c "cd /d "%~dp0" && python -m kalshi_pbot hud"
)
timeout /t 15 /nobreak >nul
goto loop

:healthy
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%HUD_URL%' -TimeoutSec 3; if ($r.StatusCode -ne 200) { exit 1 }; if ($r.Content -notmatch 'ok') { exit 1 }; exit 0 } catch { exit 1 }"
exit /b %ERRORLEVEL%
