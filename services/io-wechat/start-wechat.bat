@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0..\..\astrbot-win\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" wechat_service.py
