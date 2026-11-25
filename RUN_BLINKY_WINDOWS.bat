@echo off
title Blinky
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Blinky is not installed yet. Double-click INSTALL_WINDOWS.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" blinky.py
if errorlevel 1 pause
