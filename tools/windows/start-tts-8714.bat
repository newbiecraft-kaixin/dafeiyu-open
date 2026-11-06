@echo off
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "COSYVOICE_REPO_DIR=%~dp0CosyVoice"
set "COSYVOICE_MODEL_DIR=%~dp0cosyvoice_models\CosyVoice2-0.5B"
rem Use pythonw.exe so there is no console window; closing a window used to kill the TTS service.
set "PYW=%~dp0cosyvoice_env\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%~dp0cosyvoice_env\Scripts\python.exe"
rem Absolute script path: scheduled task working directory may differ from this folder.
"%PYW%" "%~dp0cosyvoice_tts_server.py" >> "%~dp0logs\cosyvoice.log" 2>&1
