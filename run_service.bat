@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m orchestra.cli serve
pause
