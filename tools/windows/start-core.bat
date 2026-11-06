@echo off
setlocal
REM Script lives in tools\windows; repo root is two levels up. Change this line if you move it.
for %%I in ("%~dp0..\..") do set "ROOT=%%~fI\"
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

REM python: prefer the bundled venv (verified able to run hub), else system python
set "PY=%ROOT%astrbot-win\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

call :ensure_port 23408 hub       "%PY%"  "services\hub"    "service.py"
call :ensure_port 23406 model     "%PY%"  "services\model"  "service.py"
call :ensure_port 23407 store     "%PY%"  "services\store"  "service.py"
call :ensure_port 23402 brain     "%PY%"  "services\brain"  "service.py"

echo Core backend ready: hub/model/store/brain.
echo Optional and NOT started: desktop pet, voice TTS, audio (training/songs),
echo whisper vision, QQ/wechat, stickers. Start them with their own scripts when wanted.
echo Core chain done %date% %time% >> "%LOGDIR%\core-run.log"
exit /b 0

:ensure_port
REM %1=port %2=name %3=interpreter %4=workdir(rel to ROOT) %5=entry args
set "PORT=%~1"
set "SVC=%~2"
set "EXE=%~3"
set "CWD=%ROOT%%~4"
set "ARGS=%~5"
echo [core] check %SVC% port %PORT% >> "%LOGDIR%\core-run.log"
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo [%SVC%] port %PORT% already up, skip
  echo [core] %SVC% skip >> "%LOGDIR%\core-run.log"
  exit /b 0
)
echo [%SVC%] starting port %PORT% ...
echo [core] %SVC% starting >> "%LOGDIR%\core-run.log"
pushd "%CWD%"
REM /s: child cmd strips the outer quotes, inner quotes survive, redirect happens in child
start "%SVC%" /min cmd /s /c ""%EXE%" %ARGS% >> "%LOGDIR%\%SVC%-core.log" 2>&1"
popd
REM give the service a moment to bind the port
timeout /t 2 /nobreak >nul
exit /b 0
