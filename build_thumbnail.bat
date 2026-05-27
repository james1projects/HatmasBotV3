@echo off
REM ============================================================
REM Stream Deck-friendly thumbnail builder.
REM
REM Prompts for the inputs build_thumbnail.py needs, runs it,
REM then pauses briefly so the summary stays readable. Drop the
REM path to this .bat into a Stream Deck "System: Open" or
REM "Multimedia: Run" button for one-press thumbnail creation.
REM
REM Defaults:
REM   preset = 1v1
REM   --vs is asked for when preset = 1v1, 1v2, or 2matches
REM   --vs2 is asked for when preset = 1v2 or 2matches
REM   --god2 is asked for when preset = 2matches or 2gods
REM   --result2 is asked for when preset = 1v2 or 2matches
REM   --skin / --skin2 are always asked for; blank uses the default
REM     card. Drop manual skin art at Custom God Cards/<God>-<Skin>.png
REM     to override the auto-downloaded base art for a specific skin.
REM   blank inputs skip the corresponding flag (so optional
REM   fields like result/kda just don't show on the thumbnail)
REM ============================================================

setlocal enabledelayedexpansion
pushd "%~dp0"

echo.
echo ====================================
echo    HATMAS THUMBNAIL BUILDER
echo ====================================
echo.

REM ---- Preset ----
set "PRESET=1v1"
set /p "PRESET=Preset [1v1, 1v2, 2matches, 2gods, single] (default 1v1): "

REM ---- My god (required) ----
set "GOD="
set /p "GOD=My god (e.g. Ymir, Hou Yi): "
if "!GOD!"=="" (
    echo.
    echo [!] No god provided. Aborting.
    echo.
    timeout /t 3 >nul
    popd
    endlocal
    exit /b 1
)

REM ---- Opposing god(s) and second player god ----
set "VS="
set "VS2="
set "GOD2="
set "RESULT2="
if /i "!PRESET!"=="1v1" (
    set /p "VS=Opposing god (e.g. Loki): "
)
if /i "!PRESET!"=="1v2" (
    set /p "VS=First opponent  (top, e.g. Eset): "
    set /p "VS2=Second opponent (bottom, e.g. Chiron): "
)
if /i "!PRESET!"=="2matches" (
    set /p "VS=Match 1 opponent (top-right, e.g. Baron Samedi): "
    set /p "GOD2=Match 2 my god  (bottom-left, e.g. Baron Samedi): "
    set /p "VS2=Match 2 opponent (bottom-right, e.g. Awilix): "
)
if /i "!PRESET!"=="2gods" (
    set /p "GOD2=Second god you played (right side, e.g. Atlas): "
)

REM ---- Optional skin variants for custom card art ----
REM Drop skin PNGs at "Custom God Cards/<God>-<Skin>.png" before rendering.
REM Leave blank to use the default Custom God Cards/<God>.png or auto-downloaded base.
set "SKIN="
set /p "SKIN=Skin variant for !GOD! (optional, blank = default card): "
set "SKIN2="
if defined GOD2 (
    set /p "SKIN2=Skin variant for !GOD2! (optional, blank = default card): "
)

REM ---- Optional fields ----
set "TEXT="
set /p "TEXT=Headline above VS (optional): "

set "SUBTEXT="
set /p "SUBTEXT=Subtext below VS (optional, 1v1/single/2gods): "

set "RESULT="
if /i "!PRESET!"=="1v2" (
    set /p "RESULT=Result for first opponent  [win / loss / blank]: "
    set /p "RESULT2=Result for second opponent [win / loss / blank]: "
) else if /i "!PRESET!"=="2matches" (
    set /p "RESULT=Match 1 result [win / loss / blank]: "
    set /p "RESULT2=Match 2 result [win / loss / blank]: "
) else (
    set /p "RESULT=Result [win / loss / blank]: "
)

set "KDA="
set /p "KDA=KDA e.g. 12/3/8 (optional): "

REM ---- Optional card flips ----
REM Flip toggles invert whatever the preset already does. Use them when
REM the splash art is facing the wrong way and you want gods looking
REM at each other. Type y to flip, anything else (or blank) to keep.
set "FLIPGOD="
set /p "FLIPGOD=Flip !GOD!'s card? (y / blank): "
set "FLIPVS="
if defined VS (
    set /p "FLIPVS=Flip !VS!'s card? (y / blank): "
)
set "FLIPGOD2="
if defined GOD2 (
    set /p "FLIPGOD2=Flip !GOD2!'s card? (y / blank): "
)
set "FLIPVS2="
if defined VS2 (
    set /p "FLIPVS2=Flip !VS2!'s card? (y / blank): "
)

REM ---- Build the command line ----
set "CMD=python tools\build_thumbnail.py --preset !PRESET! --god "!GOD!""
if defined VS      set "CMD=!CMD! --vs "!VS!""
if defined VS2     set "CMD=!CMD! --vs2 "!VS2!""
if defined GOD2    set "CMD=!CMD! --god2 "!GOD2!""
if defined SKIN    set "CMD=!CMD! --skin "!SKIN!""
if defined SKIN2   set "CMD=!CMD! --skin2 "!SKIN2!""
if defined TEXT    set "CMD=!CMD! --text "!TEXT!""
if defined SUBTEXT set "CMD=!CMD! --subtext "!SUBTEXT!""
if defined RESULT  set "CMD=!CMD! --result !RESULT!"
if defined RESULT2 set "CMD=!CMD! --result2 !RESULT2!"
if defined KDA     set "CMD=!CMD! --kda !KDA!"
if /i "!FLIPGOD!"=="y"  set "CMD=!CMD! --flip-god"
if /i "!FLIPVS!"=="y"   set "CMD=!CMD! --flip-vs"
if /i "!FLIPGOD2!"=="y" set "CMD=!CMD! --flip-god2"
if /i "!FLIPVS2!"=="y"  set "CMD=!CMD! --flip-vs2"

echo.
echo ------------------------------------
echo Running: !CMD!
echo ------------------------------------
echo.

call !CMD!
set "RC=!ERRORLEVEL!"

echo.
echo ------------------------------------
if "!RC!"=="0" (
    echo Done. Thumbnail saved to thumbnails\ and opened in Paint.NET.
) else (
    echo [!] build_thumbnail.py exited with code !RC!.
)
echo ------------------------------------
echo.
timeout /t 5 >nul

popd
endlocal
