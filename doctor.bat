@echo off
rem KEEP THIS FILE PURE ASCII. Chinese messages: type "tools\msg\*.txt"
rem (why: see the note at the top of run.bat)
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    type "tools\msg\no_env.txt"
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -X utf8 doctor.py %*
echo.
pause
