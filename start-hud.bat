@echo off
cd /d "%~dp0"
echo Desk: http://127.0.0.1:8080
echo Ctrl+C stops.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8080/"
python -m kalshi_pbot hud
