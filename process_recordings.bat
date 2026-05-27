@echo off
REM ============================================================================
REM process_recordings.bat
REM
REM Stream Deck-friendly wrapper around tools\process_recordings.py.
REM
REM Drag this file's path into a Stream Deck "System: Open" button (or
REM "Multimedia: Run") and you'll get one-click end-of-stream cleanup:
REM
REM   1. Scans every .mp4 sitting in HatmasBot\recordings\
REM   2. Writes its sibling .events.json (kills + deaths)
REM   3. Sorts the .mp4 + .events.json into recordings\<God>\,
REM      recordings\mixed\, or recordings\unknown\ depending on which
REM      god(s) appeared in the recording.
REM
REM Output is captured to data\process_recordings.log so you can review
REM what happened after the fact even if the console window flashed past.
REM ============================================================================

REM Always operate from the script's own folder (the HatmasBot repo root),
REM regardless of where Stream Deck launches us from.  %~dp0 expands to
REM the directory this .bat lives in, with a trailing backslash.
pushd "%~dp0"

REM Make sure the log directory exists (data\ already does in a real
REM install, but this keeps a fresh clone happy too).
if not exist "data" mkdir "data"

REM Timestamp prefix so successive runs in the same log are easy to skim.
echo. >> "data\process_recordings.log"
echo ============================================================ >> "data\process_recordings.log"
echo Run started: %DATE% %TIME% >> "data\process_recordings.log"
echo ============================================================ >> "data\process_recordings.log"

REM Run the orchestrator.  Tee-style: we want the user to see live
REM progress in the console AND have everything captured in the log.
REM PowerShell's Tee-Object handles both.  Stderr is redirected to
REM stdout so error lines also land in the log.
REM
REM If you'd rather run silently (no visible console window), change the
REM Stream Deck button to launch  pythonw.exe "<path>\tools\process_recordings.py"
REM directly and skip this .bat.
powershell -NoProfile -Command ^
    "& { python tools\process_recordings.py 2>&1 | Tee-Object -FilePath 'data\process_recordings.log' -Append }"

set EXITCODE=%ERRORLEVEL%

echo. >> "data\process_recordings.log"
echo Run ended:   %DATE% %TIME%   (exit code %EXITCODE%) >> "data\process_recordings.log"

popd

REM Brief pause so the summary stays on screen if the user is watching.
REM Stream Deck launches don't typically interact with this — it just
REM closes after the timeout — but it's nice when running from Explorer.
timeout /t 5 /nobreak > nul

exit /b %EXITCODE%
