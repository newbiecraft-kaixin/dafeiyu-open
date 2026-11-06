@echo off
chcp 65001 >nul
title NapCat (dafeiyu, Windows native)
setlocal
set "ROOT=%~dp0..\..\"

set "NC="
if exist "%ROOT%third_party\NapCat\napcat.bat" set "NC=%ROOT%third_party\NapCat"
if not defined NC if exist "%ROOT%third_party\NapCat\bootmain\napcat.bat" set "NC=%ROOT%third_party\NapCat\bootmain"
if not defined NC for /r "%ROOT%third_party\NapCat" %%F in (napcat.bat) do if not defined NC set "NC=%%~dpF"
if not defined NC if exist "%ROOT%tools\windows\napcat-win\napcat.bat" set "NC=%ROOT%tools\windows\napcat-win"

if not defined NC (
    echo [!] NapCat not found. Run install.bat in the repo root first.
    pause
    exit /b 1
)
if "%NC:~-1%"=="\" set "NC=%NC:~0,-1%"
cd /d "%NC%"
call "%NC%\napcat.bat"
pause
