@echo off
rem Den-Dev Live - portable start (no install needed)
title Den-Dev Live (log window - you can minimize it)
cd /d "%~dp0app"
if not exist "%~dp0python\python.exe" (
  echo The "python" folder is missing. Extract the WHOLE zip first ^(right-click -^> Extract All^).
  pause
  exit /b 1
)
"%~dp0python\python.exe" -u mirror.py
if errorlevel 1 (
  echo.
  echo The mirror stopped with an error. Run "1 - SETUP (first time).bat" to check this PC,
  echo or take a photo of this window if you need help.
  pause
)
