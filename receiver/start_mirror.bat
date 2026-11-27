@echo off
rem iPhone USB Mirror - double-click to start.
rem Uses the project's Python environment (receiver\.venv). Close this
rem black window or the mirror window to stop.
title iPhone USB Mirror (log)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment is missing: receiver\.venv
  echo See README.md - "Set up the receiver".
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u mirror.py %*
if errorlevel 1 pause
