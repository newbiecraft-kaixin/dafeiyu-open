@echo off
chcp 65001 >nul
cd /d C:\path\to\dafeiyu\services\io-wechat
"C:\path\to\dafeiyu\bots\wxauto-test\venv\Scripts\python.exe" wechat_service.py
