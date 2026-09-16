@echo off
REM ------------------------------------------------------------
REM rescan_all.bat
REM
REM Full rebuild of every recording's .events.json and the Ask the VOD
REM index. Written for the night after the 2026-09-15 move of the
REM library to D:\Recordings (tools/migrate_recordings.py deleted every
REM sidecar and every hatmaster row of the index on purpose).
REM
REM   1. process_recordings.py --reprocess-all
REM        walks every .mp4 under D:\Recordings (skips channels/), files
REM        loose recordings into <God>/, mixed/, unknown/, rewrites every
REM        sidecar in place. GPU-bound, ~13x realtime (~3.5 h for 43 h).
REM   2. vod_index.py index
REM        transcribes + indexes everything the sorter filed (~10x
REM        realtime, ~4.5 h). Root-level loose files are skipped, so
REM        step 1 has to finish first.
REM
REM Output is appended to data\rescan_all.log. Safe to re-run: both
REM steps skip work that is already current.
REM ------------------------------------------------------------

pushd "%~dp0.."

echo. >> "data\rescan_all.log"
echo ============================================================ >> "data\rescan_all.log"
echo Rescan started: %DATE% %TIME% >> "data\rescan_all.log"
echo ============================================================ >> "data\rescan_all.log"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "& { python tools\process_recordings.py --reprocess-all 2>&1 | Tee-Object -FilePath 'data\rescan_all.log' -Append }"
echo sorter exit code %ERRORLEVEL% >> "data\rescan_all.log"

echo. >> "data\rescan_all.log"
echo --- vod_index (Ask the VOD) --- >> "data\rescan_all.log"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "& { python tools\vod_index.py index 2>&1 | Tee-Object -FilePath 'data\rescan_all.log' -Append }"
echo vod_index exit code %ERRORLEVEL% >> "data\rescan_all.log"

echo Rescan ended:   %DATE% %TIME% >> "data\rescan_all.log"
popd
timeout /t 10
