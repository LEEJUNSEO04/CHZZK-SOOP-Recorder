@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CHZZK SOOP Recorder
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PATH=%~dp0tools;%PATH%"

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [오류] FFmpeg를 찾지 못했습니다. FFmpeg를 설치하고 PATH에 추가하세요.
  pause
  exit /b 1
)

py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.14 -u chzzk_all_in_one.py
) else (
  python -u chzzk_all_in_one.py
)

echo.
echo 프로그램이 종료되었습니다. 위의 오류 내용을 확인하세요.
pause
