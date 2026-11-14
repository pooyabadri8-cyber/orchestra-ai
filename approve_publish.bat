@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\approve_latest.py
pause
