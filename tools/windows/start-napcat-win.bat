@echo off
chcp 65001 >nul
title NapCat (dafeiyu, Windows native)
cd /d "%~dp0napcat-win" || exit /b 1
call "%~dp0napcat-win\napcat.bat"
pause
