@echo off
rem ===========================================================================
rem  KEEP THIS FILE 100%% PURE ASCII - never put Chinese text in a .bat.
rem  cmd.exe parses .bat line by line using the console code page (GBK on a
rem  Chinese Windows). UTF-8 Chinese inside gets cut apart: lines break in the
rem  middle of English words and even "if ( ... )" blocks fall apart, so the
rem  script silently does nothing. (Seen for real: the bot never started.)
rem  For Chinese messages use:  type "tools\msg\*.txt"   (UTF-8 files)
rem ===========================================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    type "tools\msg\no_env.txt"
    pause
    exit /b 1
)

if not exist "config.json" copy "config.example.json" "config.json" >nul
if not exist "data" mkdir "data"

title DeepSeek x WeChat - auto reply
".venv\Scripts\python.exe" -X utf8 main.py %*

echo.
type "tools\msg\stopped.txt"
pause
