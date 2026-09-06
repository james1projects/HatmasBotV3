@echo off
REM ============================================================================
REM vod_review.bat  (Stream Deck: VOD REVIEW)
REM
REM Opens the Ask the VOD review page (choose which recordings viewers may
REM see) in the default browser. The page is local-only by design.
REM
REM Where it comes from (logic in vod_open.ps1):
REM   1. If the bot is running, its public server on localhost:8070 already
REM      serves /vod/review (loopback always works, toggle or not).
REM   2. Otherwise the standalone dev host (tools\vod_devserver.py on
REM      localhost:8078) is started once and the page opens there. That
REM      process keeps running quietly; close it from Task Manager
REM      (python.exe, vod_devserver) if you want it gone.
REM
REM Pass a path to open a different VOD page, e.g.  vod_review.bat /vod
REM ============================================================================
setlocal
set PAGE=%~1
if "%PAGE%"=="" set PAGE=/vod/review
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vod_open.ps1" -Page "%PAGE%" -RepoRoot "%~dp0.."
endlocal
