@echo off
REM Runs paper mode forever, restarting if it ever exits. Logs to kbot.log.
REM Usage: double-click, or run  scripts\run_forever.bat  from the project folder.
cd /d "%~dp0.."
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
:loop
echo ==== starting kbot %date% %time% >> kbot.log
python -m kbot run >> kbot.log 2>&1
echo ==== kbot exited; restarting in 10s >> kbot.log
timeout /t 10 /nobreak >nul
goto loop
