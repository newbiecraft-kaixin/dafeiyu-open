@echo off
chcp 65001 >nul
title AstrBot (dafeiyu, Windows native)
cd /d "%~dp0astrbot-win\AstrBot-4.29.3" || exit /b 1
"%~dp0astrbot-win\venv\Scripts\python.exe" main.py
pause
