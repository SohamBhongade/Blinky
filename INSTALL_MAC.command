#!/bin/bash
# BLINKY - one-time setup for a Mac (MacBook, Apple chip or Intel).
# Run it:  open Terminal, type  bash  and a space, drag this file into the window, press Enter.

cd "$(dirname "$0")" || exit 1
set -o pipefail

echo
echo " =================================================="
echo "   BLINKY - one-time setup for Mac"
echo "   (needs internet; takes about 5 minutes)"
echo " =================================================="
echo

# Files downloaded from GitHub get a "quarantine" flag - remove it so the scripts can run.
xattr -dr com.apple.quarantine . 2>/dev/null
chmod +x RUN_BLINKY_MAC.command INSTALL_MAC.command 2>/dev/null

fail() { echo; echo " PROBLEM: $1"; echo; read -r -p " Press Enter to close..." _; exit 1; }

# ---------- 1. Find Python 3.12 with a working tkinter (or install it from python.org) ----------
PY=""
for c in "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12" "$(command -v python3.12 2>/dev/null)"; do
    if [ -n "$c" ] && [ -x "$c" ] && "$c" -c "import tkinter, sys; assert tkinter.TkVersion >= 8.6" 2>/dev/null; then
        PY="$c"; break
    fi
done

if [ -z "$PY" ]; then
    echo "[1/4] Python 3.12 not found - downloading it from python.org ..."
    PKG="/tmp/python-3.12.10-macos11.pkg"
    curl -L --fail -o "$PKG" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg" \
        || fail "could not download Python. Install it from https://www.python.org/downloads/release/python-31210/ (macOS installer), then run this again."
    echo "      Installing Python 3.12 - type your Mac password when asked (nothing shows while you type) ..."
    sudo installer -pkg "$PKG" -target / || fail "Python install was cancelled or failed."
    PY="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
fi
[ -x "$PY" ] || fail "Python 3.12 is still missing."
echo "[1/4] Using Python: $PY"

# ---------- 2. Make Blinky's own private Python folder (.venv) ----------
if [ ! -x ".venv/bin/python" ]; then
    echo "[2/4] Creating the .venv folder ..."
    "$PY" -m venv .venv || fail "could not create the .venv folder."
else
    echo "[2/4] .venv folder already there - reusing it"
fi

# ---------- 3. Install every Python module Blinky uses ----------
echo "[3/4] Installing Python modules (mediapipe, opencv, numpy, pillow, pyserial) ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt certifi \
    || fail "installing the Python modules failed (see the text above). Check the internet and run this again."

# ---------- 4. Face model file ----------
if [ ! -f "face_landmarker.task" ]; then
    echo "[4/4] Downloading the face model file ..."
    curl -L --fail -o face_landmarker.task \
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
else
    echo "[4/4] face_landmarker.task found"
fi

echo
echo "Checking everything loads ..."
.venv/bin/python -c "import tkinter, cv2, mediapipe, numpy, serial, PIL; print('  OK  mediapipe', mediapipe.__version__, '| opencv', cv2.__version__, '| numpy', numpy.__version__)" \
    || fail "a module did not load (see the text above)."

echo
echo " =================================================="
echo "   DONE!  Start Blinky with  RUN_BLINKY_MAC.command"
echo "   (first time: Mac asks to let Terminal use the camera - click Allow)"
echo " =================================================="
echo
read -r -p " Press Enter to close..." _
