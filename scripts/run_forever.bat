@echo off
REM Runs paper mode forever, restarting if it ever exits. Logs to kbot.log.
REM Usage: double-click, or run  scripts\run_forever.bat  from the project folder.
cd /d "%~dp0.."
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
echo kbot is running. This window stays quiet on purpose: everything goes to kbot.log.
echo   Leave this window open. To stop the bot: press Ctrl+C here.
echo   To watch it: open another window in this folder and run  python -m kbot dashboard
:loop
echo ==== starting kbot %date% %time% >> kbot.log
python -m kbot run >> kbot.log 2>&1
echo ==== kbot exited; restarting in 10s >> kbot.log
echo %time% bot stopped; restarting in 10s. See kbot.log for details.
timeout /t 10 /nobreak >nul
goto loop
