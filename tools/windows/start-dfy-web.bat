@echo off
cd /d "%~dp0astrbot-win\AstrBot-4.29.3\data\stickers"
set "PY=%~dp0cosyvoice_env\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Dafeiyu console:
echo   storage  http://127.0.0.1:8712/storage
echo   access   http://127.0.0.1:8712/access
echo Close this window to stop the server.
"%PY%" tools_source_server.py >> "%~dp0logs\dfy-web.log" 2>&1
pause
