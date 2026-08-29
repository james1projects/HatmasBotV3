@echo off
REM ============================================================
REM Stream Deck: SHIP IT — test, commit, push, restart the bot.
REM
REM The anti-rot button. Work is not done until it is committed,
REM pushed, and running in the live bot, so this does all four:
REM
REM   1. Full test suite (tools\run_tests.py). Red = abort, and
REM      nothing is committed, pushed, or restarted.
REM   2. Commit everything (only if the tree is dirty). Message:
REM        ship_it.bat My message here   -> "My message here"
REM        ship_it.bat                   -> "Ship: <date> <time>"
REM   3. Push to origin (GitHub Actions re-verifies remotely).
REM   4. Restart the bot via tools\bot_ctl.ps1 so the change is
REM      actually live, not just on disk.
REM
REM Safe to press with a clean tree: just tests + push + restart.
REM Drop onto a Stream Deck "System: Open" button.
REM ============================================================

setlocal enabledelayedexpansion
pushd "%~dp0.."

set "PY=C:\Users\james\AppData\Local\Programs\Python\Python314\python.exe"

echo.
echo ====================================
echo             SHIP IT
echo ====================================
echo.
echo [1/4] Test suite...
"%PY%" tools\run_tests.py
if errorlevel 1 (
    echo.
    echo [!] TESTS FAILED - nothing committed, pushed, or restarted.
    echo     Fix the failures, then press again.
    pause
    popd
    endlocal
    exit /b 1
)

echo.
echo [2/4] Commit...
set "DIRTY="
for /f "delims=" %%i in ('git status --porcelain') do set "DIRTY=1"
if not defined DIRTY (
    echo Working tree clean - nothing new to commit.
    goto :push
)

echo About to commit and push ALL of this:
echo ------------------------------------
git status --short
echo ------------------------------------
choice /c YN /t 15 /d Y /m "Ship it (auto-yes in 15s)"
if errorlevel 2 (
    echo Aborted - nothing committed.
    pause
    popd
    endlocal
    exit /b 1
)

git add -A
if "%~1"=="" (
    git commit -m "Ship: %DATE% %TIME:~0,5%"
) else (
    git commit -m "%*"
)

:push
echo.
echo [3/4] Push...
git push origin HEAD
if errorlevel 1 (
    echo [!] Push failed ^(offline? auth?^) - the commit is safe locally.
    echo     Continuing to restart anyway; push again later.
)

echo.
echo [4/4] Restart bot...
powershell -NoProfile -ExecutionPolicy Bypass -File tools\bot_ctl.ps1 -Action restart

echo.
echo Shipped. CI verdict: https://github.com/james1projects/HatmasBotV3/actions
echo.
timeout /t 8 >nul

popd
endlocal
