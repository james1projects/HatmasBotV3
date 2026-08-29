@echo off
REM ============================================================
REM Legacy entry point. The old console-prompt thumbnail flow has
REM been replaced by the Thumbnail Studio; this just forwards to
REM thumbnail_studio.bat so existing Stream Deck buttons pointing
REM here keep working. CLI flags pass straight through.
REM
REM The prompt-driven CLI is still available directly:
REM   python tools\build_thumbnail.py --help
REM ============================================================

call "%~dp0thumbnail_studio.bat" %*
exit /b %ERRORLEVEL%
