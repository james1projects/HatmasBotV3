@echo off
REM ============================================================================
REM vod_search.bat  (Stream Deck: VOD SEARCH)
REM
REM Opens the Ask the VOD search page. Same server logic as vod_review.bat
REM (bot's public server if the bot is up, otherwise the standalone dev
REM host on localhost:8078).
REM ============================================================================
call "%~dp0vod_review.bat" /vod
