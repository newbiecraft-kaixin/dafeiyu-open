@echo off
chcp 65001 >nul
title CosyVoice TTS Server (dafeiyu, 127.0.0.1:8714)
setlocal
set "TOOLS=%~dp0..\"

if not exist "%TOOLS%cosyvoice_env\Scripts\python.exe" (
    echo [!] cosyvoice_env not found. Run install.bat in the repo root and choose 7.
    pause
    exit /b 1
)

set "COSYVOICE_REPO_DIR=%TOOLS%CosyVoice"
"%TOOLS%cosyvoice_env\Scripts\python.exe" "%TOOLS%cosyvoice_tts_server.py"
pause
