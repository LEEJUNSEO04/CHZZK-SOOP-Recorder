@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SOOP Login Test
set "PATH=%~dp0tools;%PATH%"

py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.14 soop_streamlink_login_test.py
) else (
  python soop_streamlink_login_test.py
)

echo.
pause
