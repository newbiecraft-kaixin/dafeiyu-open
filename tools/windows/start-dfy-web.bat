@echo off
set "ROOT=%~dp0..\..\"
set "AB="
if exist "%ROOT%third_party\AstrBot\main.py" set "AB=%ROOT%third_party\AstrBot"
if not defined AB for /d %%D in ("%ROOT%third_party\AstrBot*") do if exist "%%D\main.py" set "AB=%%D"
if not defined AB for /d %%D in ("%ROOT%astrbot-win\AstrBot*") do if exist "%%D\main.py" set "AB=%%D"
if not defined AB (
    echo [!] AstrBot not found. Run install.bat in the repo root first.
    pause
    exit /b 1
)
cd /d "%AB%\data\stickers"
set "PY=%ROOT%cosyvoice_env\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Dafeiyu console:
echo   storage  http://127.0.0.1:8712/storage
echo   access   http://127.0.0.1:8712/access
echo Close this window to stop the server.
"%PY%" tools_source_server.py >> "%ROOT%logs\dfy-web.log" 2>&1
pause
