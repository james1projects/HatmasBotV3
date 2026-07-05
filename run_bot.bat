@echo off
REM ============================================================
REM HatmasBot always-on launcher.
REM
REM Runs the bot under tools\supervisor.py: a crash is logged to
REM data\crash.log and the bot restarts automatically (5s backoff
REM doubling to 5 min). Typing "quit" in the console or pressing
REM Ctrl+C still shuts everything down cleanly and stays down.
REM
REM Run tools\install_bot_task.ps1 once to make this launch
REM automatically at logon.
REM ============================================================

setlocal
pushd "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py tools\supervisor.py
) else (
    python tools\supervisor.py
)

popd
endlocal
