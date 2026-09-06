@echo off
REM ============================================================================
REM bingo_start.bat  (Stream Deck: BINGO START)
REM
REM Opens a new Stream Bingo round: viewers can grab cards at
REM hatmaster.tv/bingo, the overlay wakes up, chat gets the announcement.
REM Any round still open is closed without a winner first.
REM Needs the bot running (dashboard on localhost:8069).
REM ============================================================================
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Method Post 'http://localhost:8069/api/bingo/start' -TimeoutSec 15).Content } catch { 'bingo start failed: ' + $_.Exception.Message }"
