@echo off
REM ============================================================
REM  start_factorio.bat - launch Factorio with RCON enabled
REM  (hatmas-events mod bridge). Stream Deck friendly.
REM
REM  Reads the RCON password from core/config_local.py so there
REM  is exactly one place the password lives. RCON binds to
REM  127.0.0.1 only - nothing is exposed to the network.
REM
REM  After launch: Multiplayer -> Host saved game -> pick your
REM  streaming save. RCON goes live when the multiplayer game
REM  starts hosting, NOT in single player.
REM ============================================================
pushd "%~dp0"

REM -- password from config (single source of truth) --
for /f "delims=" %%p in ('python -c "import sys; sys.path.insert(0, '.'); from core import config; print(config.FACTORIO_RCON_PASSWORD)"') do set RCONPW=%%p
if "%RCONPW%"=="" (
  echo [start_factorio] FACTORIO_RCON_PASSWORD is empty in core/config_local.py
  pause
  exit /b 1
)

REM -- find factorio.exe (add your path here if neither matches) --
set FACTORIO_EXE=
if exist "C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"
if exist "C:\Program Files\Factorio\bin\x64\factorio.exe" set "FACTORIO_EXE=C:\Program Files\Factorio\bin\x64\factorio.exe"
if "%FACTORIO_EXE%"=="" (
  echo [start_factorio] factorio.exe not found in the usual places.
  echo Edit start_factorio.bat and set FACTORIO_EXE to your install path.
  pause
  exit /b 1
)

echo [start_factorio] Launching with RCON on 127.0.0.1:27015
start "" "%FACTORIO_EXE%" --rcon-bind 127.0.0.1:27015 --rcon-password "%RCONPW%"
popd
