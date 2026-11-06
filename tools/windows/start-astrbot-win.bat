@echo off
chcp 65001 >nul
title AstrBot (dafeiyu, Windows native)
setlocal
set "ROOT=%~dp0..\..\"
set "PY=%ROOT%astrbot-win\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "AB="
if exist "%ROOT%third_party\AstrBot\main.py" set "AB=%ROOT%third_party\AstrBot"
if not defined AB for /d %%D in ("%ROOT%third_party\AstrBot*") do if exist "%%D\main.py" set "AB=%%D"
if not defined AB for /d %%D in ("%ROOT%astrbot-win\AstrBot*") do if exist "%%D\main.py" set "AB=%%D"

if not defined AB (
    echo [!] AstrBot not found. Run install.bat in the repo root first.
    pause
    exit /b 1
)
cd /d "%AB%"
"%PY%" main.py
pause
