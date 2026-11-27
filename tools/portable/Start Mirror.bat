@echo off
rem iPhone USB Mirror - portable start (no install needed)
title iPhone USB Mirror (log window - you can minimize it)
cd /d "%~dp0app"
"%~dp0python\python.exe" -u mirror.py
if errorlevel 1 (
  echo.
  echo The mirror stopped with an error. Take a photo of this window if you need help.
  pause
)
