@echo off
REM ============================================================
REM Quick Discord send tester for HatmasBot.
REM
REM Sends a test message to a Discord channel WITHOUT starting
REM the whole bot. Handy for verifying the bot can post to a
REM specific channel (e.g. a private #bot-test channel).
REM
REM Usage (run from a terminal, or drop on a Stream Deck
REM "System: Open" button):
REM
REM   discord_test.bat                       send to the default channel
REM   discord_test.bat --list                list channels + ids the bot sees
REM   discord_test.bat 123456789012345678    send to a specific channel id
REM   discord_test.bat 1234... "hello there" custom message to that channel
REM
REM Whatever you type after the .bat is passed straight through
REM to tools\discord_test.py. The window pauses at the end so you
REM can read the OK/FAIL line.
REM
REM Exit codes: 0 = sent (or list shown), 1 = something failed.
REM ============================================================

setlocal
pushd "%~dp0"

python tools\discord_test.py %*
set "RC=%ERRORLEVEL%"

echo.
echo Press any key to close...
pause >nul

popd
endlocal
exit /b %RC%
