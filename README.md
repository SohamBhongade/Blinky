# Blinky — control your room with a blink

Technovate 2026–27 · Mission 05 · *The Last Human Decision*

Blinky watches the patient's eyes through a webcam. A menu (SOS · Call Nurse · Light · Fan) scans on screen. Close your eyes for **2 s** to pick the highlighted option, then close them for **2.5 s** to confirm. Blinking fast 5 times sends an **SOS**. The laptop sends the command to the Arduino UNO Q over the HC-05 Bluetooth module and sends an alert to the caregiver's phone through the ntfy app.

---

## 1. Download

On this GitHub page click the green **Code** button, then **Download ZIP**, and unzip it somewhere easy, such as the Desktop.
Keep every file in the same folder, especially `blinky.py` and `face_landmarker.task`.

## 2. Install (one time only, needs internet, about 5 min)

### Windows laptop
1. Double-click **`INSTALL_WINDOWS.bat`**.
   If a blue "Windows protected your PC" box appears, click **More info**, then **Run anyway**.
2. Wait for **DONE!** It installs Python 3.12 if it's missing, then every Python module Blinky needs.

### MacBook
1. Open **Terminal** (press Cmd + Space, type *Terminal*, press Enter).
2. Type `bash ` (with a space after it), drag **`INSTALL_MAC.command`** into the Terminal window, and press **Enter**.
3. If it asks for your Mac password, type it and press Enter. Nothing shows on screen while you type; that's normal.
4. Wait for **DONE!**

## 3. Start Blinky

| Windows | Mac |
|---|---|
| Double-click **`RUN_BLINKY_WINDOWS.bat`** | In Terminal: `bash `, drag in **`RUN_BLINKY_MAC.command`**, Enter. After the install you can usually just double-click it. |

**Mac, first time:** a box asks to let Terminal use the **camera**. Click **Allow**. If you clicked "Don't Allow", go to System Settings → Privacy & Security → Camera and turn **Terminal** on.

## 4. Connect the Arduino (Bluetooth)

1. Switch on the Blinky box (battery rocker switch).
2. Pair **HC-05** in the laptop's Bluetooth settings. The PIN is usually **1234** (or 0000).
3. Start Blinky. It finds the HC-05 by itself:
   - **Windows:** it tries `COM5` first, then every other Bluetooth COM port.
   - **Mac:** it looks for `/dev/cu.HC-05`.
4. If the Arduino isn't found, Blinky still runs in demo mode. Check the HC-05 is paired, and close the Arduino IDE Serial Monitor.

The Arduino code is already on the board. To upload it again, open `blinky_v2/blinky_v2.ino` in the Arduino IDE (no extra libraries needed). **Unplug the HC-05 TX/RX wires (D0/D1) while uploading.**

## 5. Phone alerts (ntfy)

Install the **ntfy** app on the phone, tap **+**, enter the topic name written in `blinky.py` (`NTFY_TOPIC`), and tap **Subscribe**. The laptop and the phone both need internet (for example the phone's hotspot).

## Keys

Space = select (backup if blinks don't work) · V = voice on/off · B = beep on/off · ↑/↓ = blink sensitivity · F11 = full screen

## What gets installed

Python 3.12, plus these modules (exact versions in `requirements.txt`): mediapipe, opencv-contrib-python, numpy, pillow, pyserial, and pyttsx3 (Windows only).
Everything goes into a `.venv` folder inside this project, so it doesn't touch any other Python on the computer.
