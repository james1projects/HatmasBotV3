@echo off
REM ============================================================================
REM vod_sync.bat
REM
REM Stream Deck wrapper around tools\vod_channels.py sync --all:
REM for every registered Twitch channel, list its new archive VODs,
REM download the newest few to D:\Recordings\channels\<login>\, run the
REM offline detector/sorter with that channel's profile, and index the
REM transcripts into vod_index.db (local-only rows).
REM
REM Output goes to data\vod_sync.log. Downloads resume if interrupted.
REM ============================================================================

pushd "%~dp0.."
if not exist "data" mkdir "data"

echo. >> "data\vod_sync.log"
echo ============================================================ >> "data\vod_sync.log"
echo Run started: %DATE% %TIME% >> "data\vod_sync.log"
echo ============================================================ >> "data\vod_sync.log"

powershell -NoProfile -Command ^
    "& { python tools\vod_channels.py sync --all 2>&1 | Tee-Object -FilePath 'data\vod_sync.log' -Append }"

set EXITCODE=%ERRORLEVEL%

echo. >> "data\vod_sync.log"
echo Run ended:   %DATE% %TIME%   (exit code %EXITCODE%) >> "data\vod_sync.log"

popd
timeout /t 5 /nobreak > nul
exit /b %EXITCODE%
