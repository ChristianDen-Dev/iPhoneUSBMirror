@echo off
rem iPhone USB Mirror - first-time setup on a new PC.
rem 1) removes the "downloaded from the internet" mark Windows puts on files
rem    from Google Drive (it can block the program), 2) checks the PC.
title iPhone USB Mirror - first-time setup
cd /d "%~dp0"
echo Preparing files (removing the internet-download block)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File | Unblock-File" >nul 2>&1
"%~dp0python\python.exe" -u "%~dp0app\setup_check.py"
