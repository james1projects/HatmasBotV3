@echo off
REM ============================================================
REM Stream Deck: restart (or start) HatmasBot.
REM
REM If the bot is running under the supervisor, this kills the
REM bot process and lets the supervisor relaunch it (~5s). If
REM nothing is running, it opens a new console via run_bot.bat.
REM
REM Drop onto a Stream Deck "System: Open" button. Safe to press
REM whether the bot is up, down, or wedged.
REM ============================================================

setlocal
pushd "%~dp0.."

echo.
echo ====================================
echo        HATMASBOT RESTART
echo ====================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File tools\bot_ctl.ps1 -Action restart

echo.
echo Pause for 6 seconds so you can read the result...
timeout /t 6 >nul

popd
endlocal
