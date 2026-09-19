@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Optional OpenAI Image API Setup
echo 이 기능은 선택 사항이며 별도 API 사용료가 발생할 수 있습니다.

py -3.14 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.14 openai_image_api.py --save-key
) else (
  python openai_image_api.py --save-key
)

echo.
pause
