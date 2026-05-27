@echo off
REM ============================================================
REM cleanup_p2.bat — finishes the P2 hygiene sweep.
REM
REM Why this exists: the sandboxed shell I run from can git-mv
REM SOMETIMES but its filesystem permissions are flaky — it
REM started doing the test_*.py moves, hit a stale .git/index.lock
REM it couldn't unlink, and corrupted partway through. So we let
REM your local Windows shell, which has full permissions, finish
REM the job in one shot.
REM
REM What this script does, in order:
REM   1. Clears the stale .git/index.lock if present.
REM   2. Verifies the git index still works (git status).
REM   3. Moves the remaining 6 test_*.py to archive/initial_dev_tests/
REM      (2 are already there from the partial sandbox run).
REM   4. Moves all 11 prototype_*.html to overlays/_prototypes/.
REM   5. Moves capture_frames.py + spotify_auth.py to archive/superseded/.
REM   6. Deletes the untracked junk:
REM        scan*.log (6 files)
REM        test.png / test.psd / test_preview.png
REM        test_layers/
REM        CURRENT_TASK.md (already gitignored)
REM      (Note: twitch_logo.png used to live at the repo root and was
REM      explicitly preserved here; it now lives at assets/twitch_logo.png
REM      where tools/youtube_live_badge.py reads it from.)
REM
REM Run from the repo root by double-clicking, or from cmd:
REM     cd C:\Users\james\HatmasBot
REM     cleanup_p2.bat
REM
REM Pauses at the end so you can read the summary. Nothing in
REM here is destructive to tracked files except the git mv ops
REM (which preserve history). Untracked deletes are real deletes.
REM ============================================================

setlocal enabledelayedexpansion
pushd "%~dp0"

echo.
echo =====================================================
echo   HATMASBOT P2 HYGIENE SWEEP
echo =====================================================
echo.

REM ---- Step 1: clear stale index.lock ----
if exist ".git\index.lock" (
    echo [1/6] Clearing stale .git\index.lock ...
    del /f /q ".git\index.lock"
    if exist ".git\index.lock" (
        echo       FAILED to delete .git\index.lock — abort.
        echo       Close any open git GUI / VS Code source-control panel
        echo       and re-run.
        goto :end
    )
    echo       Cleared.
) else (
    echo [1/6] No stale lock — good.
)
echo.

REM ---- Step 2: verify git is healthy ----
echo [2/6] Verifying git index ...
git status --short >nul 2>&1
if errorlevel 1 (
    echo       git status failed. Index may need repair.
    echo       Try:    git read-tree HEAD
    echo       Then re-run this script.
    goto :end
)
echo       git is healthy.
echo.

REM ---- Step 3: finish moving test_*.py ----
echo [3/6] Moving remaining test_*.py to archive\initial_dev_tests\ ...
if not exist "archive\initial_dev_tests" mkdir "archive\initial_dev_tests"
for %%F in (test_godrequest.py test_nowplaying.py test_obs_align.py test_title.py test_tracker.py test_tracker_teams.py) do (
    if exist "%%F" (
        git mv "%%F" "archive\initial_dev_tests\%%F"
        if errorlevel 1 (
            echo       FAILED to git mv %%F
        ) else (
            echo       moved %%F
        )
    )
)
echo.

REM ---- Step 4: move prototype overlays ----
echo [4/6] Moving prototype_*.html to overlays\_prototypes\ ...
if not exist "overlays\_prototypes" mkdir "overlays\_prototypes"
for %%F in (overlays\prototype_*.html) do (
    if exist "%%F" (
        for %%G in (%%~nxF) do git mv "overlays\%%G" "overlays\_prototypes\%%G"
    )
)
echo.

REM ---- Step 5: move superseded root scripts ----
echo [5/6] Moving superseded root scripts to archive\superseded\ ...
if not exist "archive\superseded" mkdir "archive\superseded"
for %%F in (capture_frames.py spotify_auth.py) do (
    if exist "%%F" (
        git mv "%%F" "archive\superseded\%%F"
        if errorlevel 1 (
            echo       FAILED to git mv %%F
        ) else (
            echo       moved %%F
        )
    )
)
echo.

REM ---- Step 6: delete untracked junk ----
echo [6/6] Deleting untracked junk at repo root ...
for %%F in (scan.log scan_cuda.log scan_enroll.log scan_no_refined.log scan_optimized.log scan_post_enroll.log) do (
    if exist "%%F" (
        del /f /q "%%F"
        echo       deleted %%F
    )
)
REM Note: twitch_logo.png lives at assets/twitch_logo.png (not the
REM repo root) so it is naturally excluded from this root-level sweep.
REM tools/youtube_live_badge.py reads it as the LIVE badge glyph
REM (with a fallback to a programmatic glyph if missing — works
REM either way, but the file produces a nicer badge).
for %%F in (test.png test.psd test_preview.png) do (
    if exist "%%F" (
        del /f /q "%%F"
        echo       deleted %%F
    )
)
if exist "test_layers" (
    rmdir /s /q "test_layers"
    echo       deleted test_layers\
)
if exist "CURRENT_TASK.md" (
    del /f /q "CURRENT_TASK.md"
    echo       deleted CURRENT_TASK.md
)
echo.

REM ---- Summary ----
echo =====================================================
echo   DONE.  Run "git status" to see the staged moves.
echo =====================================================
echo.
echo   Suggested commit:
echo     git add -A
echo     git commit -m "P2 hygiene: archive root test scripts, prototype overlays, superseded scripts; delete junk"
echo.

:end
popd
endlocal
pause
