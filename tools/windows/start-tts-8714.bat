@echo off
setlocal
set "TOOLS=%~dp0..\"
if not exist "%TOOLS%logs" mkdir "%TOOLS%logs"
set "COSYVOICE_REPO_DIR=%TOOLS%CosyVoice"
set "COSYVOICE_MODEL_DIR=%TOOLS%cosyvoice_models\CosyVoice2-0.5B"
rem Use pythonw.exe so there is no console window; closing a window used to kill the TTS service.
set "PYW=%TOOLS%cosyvoice_env\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%TOOLS%cosyvoice_env\Scripts\python.exe"
rem Absolute script path: scheduled task working directory may differ from this folder.
"%PYW%" "%TOOLS%cosyvoice_tts_server.py" >> "%TOOLS%logs\cosyvoice.log" 2>&1
