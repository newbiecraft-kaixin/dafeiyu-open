@echo off
setlocal
REM Script lives in installer\; repo root is one level up. Change this line if you move it.
for %%I in ("%~dp0..") do set "ROOT=%%~fI\"
set "LOGDIR=%ROOT%logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ===== dafeiyu core chain (lite mode) %date% %time% ===== >> "%LOGDIR%\core-run.log"

REM ============================================================
REM dafeiyu CORE-ONLY launcher (lite mode, for low-end machines)
REM Core backend = 4 services only:
REM   hub 23408 / model 23406 / store 23407 / brain 23402
REM Optional (NOT required, start separately when wanted):
REM   pet-relay 23334 (desktop pet side channel)
REM   voice 8714 (CosyVoice TTS)
REM   audio 23366 (voice training + song library)
REM   subtitle 23405 (whisper ASR / Ollama vision)
REM   napcat QQ / astrbot / io-wechat / stickers / dsh
REM ============================================================

REM python: Python is a prerequisite; prefer the system default Python, old venv is only a fallback.
set "PY=python"
if not exist "%ROOT%astrbot-win\venv\Scripts\python.exe" goto :py_ready
where python >nul 2>&1 || set "PY=%ROOT%astrbot-win\venv\Scripts\python.exe"
:py_ready

REM Each service is started inline (no call :label) so this script also works when
REM cmd.exe is invoked with a quoted script path, where label lookup can fail.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 23408 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo [hub] starting port 23408 ...
  echo [core] hub starting >> "%LOGDIR%\core-run.log"
  start "hub" /min /d "%ROOT%services\hub" "%PY%" service.py >> "%LOGDIR%\hub-core.log" 2>&1
  timeout /t 2 /nobreak >nul
) else (
  echo [hub] port 23408 already up, skip
  echo [core] hub skip >> "%LOGDIR%\core-run.log"
)

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 23406 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo [model] starting port 23406 ...
  echo [core] model starting >> "%LOGDIR%\core-run.log"
  start "model" /min /d "%ROOT%services\model" "%PY%" service.py >> "%LOGDIR%\model-core.log" 2>&1
  timeout /t 2 /nobreak >nul
) else (
  echo [model] port 23406 already up, skip
  echo [core] model skip >> "%LOGDIR%\core-run.log"
)

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 23407 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo [store] starting port 23407 ...
  echo [core] store starting >> "%LOGDIR%\core-run.log"
  start "store" /min /d "%ROOT%services\store" "%PY%" service.py >> "%LOGDIR%\store-core.log" 2>&1
  timeout /t 2 /nobreak >nul
) else (
  echo [store] port 23407 already up, skip
  echo [core] store skip >> "%LOGDIR%\core-run.log"
)

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 23402 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo [brain] starting port 23402 ...
  echo [core] brain starting >> "%LOGDIR%\core-run.log"
  start "brain" /min /d "%ROOT%services\brain" "%PY%" service.py >> "%LOGDIR%\brain-core.log" 2>&1
  timeout /t 2 /nobreak >nul
) else (
  echo [brain] port 23402 already up, skip
  echo [core] brain skip >> "%LOGDIR%\core-run.log"
)

echo Core backend ready: hub/model/store/brain.
echo Optional and NOT started: desktop pet, voice TTS, audio (training/songs),
echo whisper vision, QQ/wechat, stickers. Start them with their own scripts when wanted.
echo Core chain done %date% %time% >> "%LOGDIR%\core-run.log"
exit /b 0
