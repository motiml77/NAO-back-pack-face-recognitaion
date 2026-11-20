@echo off
rem Make Remote Printer start automatically (in the background) every time this user logs in to Windows.
cd /d "%~dp0"
python remote_printer.py --install-autostart
if errorlevel 1 goto end
rem Start it now in the background as well
start "" pythonw remote_printer.py
echo.
echo Remote Printer is now running in the background.
:end
pause
