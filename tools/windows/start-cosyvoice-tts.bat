@echo off
chcp 65001 >nul
title CosyVoice TTS Server (dafeiyu, 127.0.0.1:8714)
cd /d "%~dp0" || exit /b 1

if not exist "cosyvoice_env\Scripts\python.exe" (
    echo [!] cosyvoice_env 不存在，先跑一次安装： py setup_cosyvoice.py
    pause
    exit /b 1
)

set COSYVOICE_REPO_DIR=%~dp0CosyVoice
"%~dp0cosyvoice_env\Scripts\python.exe" cosyvoice_tts_server.py
pause
