@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" roto_app.py %*
