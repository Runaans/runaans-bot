@echo off
REM Keeps the bot running on a Windows host. Restarts it if it exits, but
REM gives up after MAX_RESTARTS so a broken .env doesn't spin forever.
REM Register in Task Scheduler with "At log on" + "Restart if the task fails".

setlocal
cd /d "%~dp0"

set MAX_RESTARTS=10
set COUNT=0

:loop
echo [%date% %time%] Starting bot...
python main.py

set /a COUNT+=1
if %COUNT% GEQ %MAX_RESTARTS% (
    echo [%date% %time%] Exited %COUNT% times, giving up. Check logs\bot.log
    exit /b 1
)

echo [%date% %time%] Bot exited. Restart %COUNT%/%MAX_RESTARTS% in 15s...
timeout /t 15 /nobreak >nul
goto loop
