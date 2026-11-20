@echo off
rem Run Remote Printer in a console window (for testing). Close the window or press Ctrl+C to stop.
cd /d "%~dp0"
python remote_printer.py
pause
