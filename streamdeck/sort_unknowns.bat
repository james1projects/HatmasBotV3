@echo off
REM ============================================================
REM Stream Deck: interactive walkthrough of recordings\unknown\.
REM
REM Wraps tools\sort_unknowns.py. For each unidentified clip it
REM suggests the most likely god, asks you to confirm, captures a
REM portrait reference so the matcher learns, and files the clip
REM into the right per-god folder.
REM
REM Run after process_recordings.bat whenever anything landed in
REM recordings\unknown\. Interactive - answer the prompts.
REM ============================================================

setlocal
pushd "%~dp0.."

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py tools\sort_unknowns.py %*
) else (
    python tools\sort_unknowns.py %*
)

echo.
pause

popd
endlocal
