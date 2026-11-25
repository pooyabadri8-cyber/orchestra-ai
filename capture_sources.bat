@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo ORCHESTRA is not installed in this folder yet: run setup.bat first.
  echo If you used an older copy, first copy .env, data and assets_store from it into this folder.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\capture_sources.py
pause
