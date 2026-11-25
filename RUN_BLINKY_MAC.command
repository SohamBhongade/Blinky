#!/bin/bash
# Start Blinky on a Mac.  (If double-clicking does nothing: open Terminal, type  bash  and a space,
# drag this file into the window, press Enter.)
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
    echo "Blinky is not installed yet. Run INSTALL_MAC.command first."
    read -r -p "Press Enter to close..." _
    exit 1
fi
# python.org's Python needs this for the internet phone alerts (ntfy) to work
export SSL_CERT_FILE="$(.venv/bin/python -c 'import certifi; print(certifi.where())' 2>/dev/null)"
.venv/bin/python blinky.py || read -r -p "Blinky stopped with an error (see above). Press Enter to close..." _
