@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Recorder Python Dependencies

py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.14 -m pip install -r requirements.txt
) else (
  python -m pip install -r requirements.txt
)

echo.
echo 설치 결과를 확인한 뒤 창을 닫으세요.
pause
