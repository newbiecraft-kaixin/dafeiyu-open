@echo off
setlocal
REM Scheduled-task entry: resolve every path relative to the FlatFish root.
set "ROOT=%~dp0..\"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
set "COSYVOICE_REPO_DIR=%ROOT%audio\CosyVoice"
if not exist "%COSYVOICE_REPO_DIR%" set "COSYVOICE_REPO_DIR=%ROOT%CosyVoice"
if not exist "%COSYVOICE_REPO_DIR%" set "COSYVOICE_REPO_DIR=%ROOT%tools\CosyVoice"
set "COSYVOICE_MODEL_DIR=%ROOT%audio\cosyvoice_models\CosyVoice2-0.5B"
if not exist "%COSYVOICE_MODEL_DIR%" set "COSYVOICE_MODEL_DIR=%ROOT%cosyvoice_models\CosyVoice2-0.5B"
if not exist "%COSYVOICE_MODEL_DIR%" set "COSYVOICE_MODEL_DIR=%ROOT%tools\cosyvoice_models\CosyVoice2-0.5B"
set "PYW="
where pythonw >nul 2>&1 && set "PYW=pythonw"
if not defined PYW if exist "%ROOT%audio\cosyvoice_env\Scripts\pythonw.exe" set "PYW=%ROOT%audio\cosyvoice_env\Scripts\pythonw.exe"
if not defined PYW if exist "%ROOT%audio\cosyvoice_env\Scripts\python.exe" set "PYW=%ROOT%audio\cosyvoice_env\Scripts\python.exe"
if not defined PYW if exist "%ROOT%tools\cosyvoice_env\Scripts\pythonw.exe" set "PYW=%ROOT%tools\cosyvoice_env\Scripts\pythonw.exe"
if not defined PYW set "PYW=%ROOT%tools\cosyvoice_env\Scripts\python.exe"
REM Use pythonw.exe so there is no console window; closing a window used to kill the TTS service.
"%PYW%" "%ROOT%tools\cosyvoice_tts_server.py" >> "%ROOT%logs\cosyvoice.log" 2>&1
