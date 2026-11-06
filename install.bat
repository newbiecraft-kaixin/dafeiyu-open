@echo off
chcp 65001 >nul
title dafeiyu installer
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows\setup.ps1" %*
echo.
pause
