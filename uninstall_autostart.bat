@echo off
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ORCHESTRA control bot.cmd" 2>nul
schtasks /Delete /F /TN "ORCHESTRA control bot" 2>nul
echo Done.
pause
