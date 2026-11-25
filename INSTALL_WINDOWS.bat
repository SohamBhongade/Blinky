@echo off
setlocal
title Blinky - Windows installer
cd /d "%~dp0"

echo.
echo  ==================================================
echo    BLINKY - one-time setup for Windows
echo    (needs internet; takes about 5 minutes)
echo  ==================================================
echo.

rem ---------- 1. Find Python 3.12 (or install it) ----------
set "PYEXE="
for /f "delims=" %%i in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"

if not defined PYEXE (
    echo [1/4] Python 3.12 not found - downloading it from python.org ...
    curl -L --fail -o "%TEMP%\python-3.12.10-amd64.exe" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    if errorlevel 1 goto :fail_python
    echo       Installing Python 3.12 - a small window will show the progress ...
    "%TEMP%\python-3.12.10-amd64.exe" /passive InstallAllUsers=0 PrependPath=0 Include_launcher=1 Include_tcltk=1 Include_pip=1 Include_test=0
)
if not defined PYEXE set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" goto :fail_python
echo [1/4] Using Python: %PYEXE%

rem ---------- 2. Make Blinky's own private Python folder (.venv) ----------
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating the .venv folder ...
    "%PYEXE%" -m venv .venv
    if errorlevel 1 goto :fail_modules
) else (
    echo [2/4] .venv folder already there - reusing it
)

rem ---------- 3. Install every Python module Blinky uses ----------
echo [3/4] Installing Python modules (mediapipe, opencv, numpy, pillow, pyserial, pyttsx3) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail_modules

rem ---------- 4. Face model file ----------
if not exist "face_landmarker.task" (
    echo [4/4] Downloading the face model file ...
    curl -L --fail -o "face_landmarker.task" "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
) else (
    echo [4/4] face_landmarker.task found
)

echo.
echo Checking everything loads ...
".venv\Scripts\python.exe" -c "import tkinter, cv2, mediapipe, numpy, serial, PIL; print('  OK  mediapipe', mediapipe.__version__, '| opencv', cv2.__version__, '| numpy', numpy.__version__)"
if errorlevel 1 goto :fail_modules

echo.
echo  ==================================================
echo    DONE!  Double-click  RUN_BLINKY_WINDOWS.bat  to start Blinky.
echo  ==================================================
echo.
pause
exit /b 0

:fail_python
echo.
echo  PROBLEM: could not get Python 3.12.
echo  Install it by hand from https://www.python.org/downloads/release/python-31210/
echo  ("Windows installer 64-bit"), then run this file again.
pause
exit /b 1

:fail_modules
echo.
echo  PROBLEM: installing the Python modules failed (see the red text above).
echo  Check the internet connection and run this file again.
pause
exit /b 1
