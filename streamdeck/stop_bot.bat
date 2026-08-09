@echo off
REM ============================================================
REM Stream Deck: stop HatmasBot (supervisor + bot).
REM
REM Kills the supervisor first so it cannot relaunch, then the
REM bot process. Hard kill - safe because all state writes are
REM atomic and economy.db is SQLite WAL. For a fully graceful
REM stop, type "quit" in the bot console instead.
REM ============================================================

setlocal
pushd "%~dp0.."

echo.
echo ====================================
echo         HATMASBOT STOP
echo ====================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File tools\bot_ctl.ps1 -Action stop

echo.
echo Pause for 6 seconds so you can read the result...
timeout /t 6 >nul

popd
endlocal
