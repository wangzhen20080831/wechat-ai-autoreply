@echo off
rem KEEP THIS FILE PURE ASCII. Chinese messages: type "tools\msg\*.txt"
rem (why: see the note at the top of run.bat)
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   DeepSeek x WeChat - one-time setup
echo ============================================================
echo.

set "PYCMD="
for %%V in (3.12 3.11 3.10 3.9) do (
    if not defined PYCMD (
        py -%%V -c "import sys;assert sys.maxsize>2**32" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -%%V"
    )
)
if not defined PYCMD (
    python -c "import sys;assert sys.maxsize>2**32" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    type "tools\msg\need_python.txt"
    pause
    exit /b 1
)

echo [1/4] interpreter: %PYCMD%
%PYCMD% -c "import sys;print('      Python', sys.version)"

if not exist ".venv\Scripts\python.exe" (
    echo [2/4] creating virtualenv .venv ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] failed to create .venv
        pause
        exit /b 1
    )
) else (
    echo [2/4] .venv already exists, skipped
)

echo [3/4] installing dependencies (about 100MB on first run) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    type "tools\msg\pip_failed.txt"
    echo.
    pause
    exit /b 1
)

echo [4/4] preparing config files ...
if not exist "config.json" copy "config.example.json" "config.json" >nul
if not exist ".env" copy ".env.example" ".env" >nul
if not exist "data" mkdir "data"

echo.
echo ============================================================
type "tools\msg\setup_done.txt"
echo ============================================================
echo.
notepad .env
pause
