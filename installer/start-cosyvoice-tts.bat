@echo off
chcp 65001 >nul
title CosyVoice TTS Server (FlatFish, 127.0.0.1:8714)
setlocal
set "ROOT=%~dp0..\"
REM Python deps live in the default Python env; old cosyvoice_env is only a fallback.
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY if exist "%ROOT%audio\cosyvoice_env\Scripts\python.exe" set "PY=%ROOT%audio\cosyvoice_env\Scripts\python.exe"
if not defined PY if exist "%ROOT%tools\cosyvoice_env\Scripts\python.exe" set "PY=%ROOT%tools\cosyvoice_env\Scripts\python.exe"

if not defined PY (
    echo [!] Python not found. Install Python 3.10+ and run install.bat option 7.
    pause
    exit /b 1
)

set "COSYVOICE_REPO_DIR=%ROOT%audio\CosyVoice"
if not exist "%COSYVOICE_REPO_DIR%" set "COSYVOICE_REPO_DIR=%ROOT%CosyVoice"
if not exist "%COSYVOICE_REPO_DIR%" set "COSYVOICE_REPO_DIR=%ROOT%tools\CosyVoice"
set "COSYVOICE_MODEL_DIR=%ROOT%audio\cosyvoice_models\CosyVoice2-0.5B"
if not exist "%COSYVOICE_MODEL_DIR%" set "COSYVOICE_MODEL_DIR=%ROOT%cosyvoice_models\CosyVoice2-0.5B"
if not exist "%COSYVOICE_MODEL_DIR%" set "COSYVOICE_MODEL_DIR=%ROOT%tools\cosyvoice_models\CosyVoice2-0.5B"

"%PY%" "%ROOT%tools\cosyvoice_tts_server.py"
pause
