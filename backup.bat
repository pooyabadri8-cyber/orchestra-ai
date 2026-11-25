@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo ORCHESTRA is not installed in this folder yet: run setup.bat first.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m orchestra.cli backup
pause
