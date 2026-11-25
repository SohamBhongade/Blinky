"""
BlinkBuddy - control your room with eye blinks.
Technovate hackathon - theme: "The Last Human Decision"
Tagline: "Every blink is a decision."

How the program works, in one loop (tick) that runs about 30 times a second:
  1. Read a webcam frame.
  2. MediaPipe Face Mesh finds 468 points on the face; we take 6 around each eye.
  3. Turn those points into one number per frame: the Eye Aspect Ratio (EAR).
     Open eye = high EAR, closed eye = low EAR.
  4. The BlinkDetector turns the EAR stream into events:
       BLINK = short close (< 0.5 s)       -> ignored, except for counting rapid blinks
       HOLD  = eyes closed for 3 s         -> select / confirm
       RAPID = 5 short blinks within 3 s   -> instant SOS
  5. The app's state machine (CALIBRATE -> SCAN -> CONFIRM, and SOS) reacts to events.
  6. Redraw the screen.

Everything runs offline: MediaPipe's face model is inside the pip package.
"""

import math
import os
import statistics
import time
import tkinter as tk
from collections import deque
from datetime import datetime

import cv2
import mediapipe as mp
import numpy as np
import serial                      # pyserial
from PIL import Image, ImageTk     # Pillow, to show camera frames in Tkinter

# =====================================================================
# SETTINGS - tune these on the day
# =====================================================================
SERIAL_PORT = "COM5"     # HC-05 *Outgoing* COM port (see "how to run")
BAUD_RATE = 9600         # must match the HC-05 and the Arduino sketch
CAMERA_INDEX = 0         # 0 = built-in webcam, 1 = first USB webcam

SCAN_INTERVAL = 3.0          # seconds the highlight stays on each tile
SELECT_HOLD = 3.0            # seconds of closed eyes to select / confirm
CONFIRM_WINDOW = 6.0         # seconds the patient has to confirm
BLINK_MAX = 0.5              # closes shorter than this are normal blinks
RAPID_BLINK_COUNT = 5        # this many quick blinks ...
RAPID_BLINK_WINDOW = 3.0     # ... within this many seconds = SOS

CALIBRATION_SECONDS = 3.0    # how long we measure the open-eye EAR at start
EAR_THRESHOLD = None         # None = set automatically by calibration.
                             # Put a number (e.g. 0.20) here to force it.
EAR_CLOSED_RATIO = 0.70      # auto threshold = open-eye EAR x this ratio
EAR_HYSTERESIS = 0.02        # eyes only count as open again above threshold + this
EAR_FALLBACK = 0.20          # used if calibration is skipped with Space

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blinkbuddy_log.txt")
LOOP_MS = 15                 # main loop delay (the camera read sets the real pace)

# =====================================================================
# MENU TILES - to add a tile, add a line here and a branch in perform_action()
# =====================================================================
TILES = [
    {"id": "SOS",   "name": "SOS",        "color": "#ff2a2a", "text_on_fill": "white"},
    {"id": "NURSE", "name": "CALL NURSE", "color": "#ffb300", "text_on_fill": "black"},
    {"id": "LIGHT", "name": "LIGHT",      "color": "#fff1c9", "text_on_fill": "black"},
    {"id": "FAN",   "name": "FAN",        "color": "#22c7d8", "text_on_fill": "black"},
    # {"id": "MEDICINE", "name": "MEDICINE", "color": "#b388ff", "text_on_fill": "black"},
]

# Colours and font for the projector: dark background, bright text
BG = "#0b0d12"
PANEL = "#171b24"
TEXT = "#f5f5f5"
MUTED = "#9aa3b2"
GOOD = "#3ddc84"
BAD = "#ff5252"
FONT = "Segoe UI"

# Face Mesh landmark numbers for the 6 eye points, in EAR order:
# p1 = one corner, p2 & p3 = upper lid, p4 = other corner, p5 & p6 = lower lid
LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]


def font(px, bold=False):
    """Tk font sized in pixels (negative size = pixels), so text scales with the window."""
    return (FONT, -max(8, int(px)), "bold" if bold else "normal")


def eye_aspect_ratio(p):
    """EAR = (two vertical eye openings) / (2 x eye width).
    About 0.3 when open, drops towards 0.1 when closed."""
    vertical = math.dist(p[1], p[5]) + math.dist(p[2], p[4])
    horizontal = math.dist(p[0], p[3])
    return vertical / (2.0 * horizontal) if horizontal > 0 else 0.0


# =====================================================================
# ARDUINO LINK - sends single characters over Bluetooth (HC-05 = COM port)
# =====================================================================
class ArduinoLink:
    def __init__(self, port, baud):
        self.port = port
        self.ser = None
        print(f"Connecting to Arduino on {port} ...")
        try:
            self.ser = serial.Serial(port, baud, timeout=0, write_timeout=1)
            print("Arduino connected.")
        except (serial.SerialException, OSError) as e:
            print(f"Could not open {port} ({e}). Running in DEMO MODE.")

    @property
    def connected(self):
        return self.ser is not None

    def send(self, char):
        if self.ser:
            try:
                self.ser.reset_input_buffer()   # we never read; throw away anything the board sent
                self.ser.write(char.encode("ascii"))
                print(f"[Arduino] sent '{char}'")
                return
            except (serial.SerialException, OSError) as e:
                print(f"[Arduino] send failed ({e}). Switching to DEMO MODE.")
                self.close()
        print(f"[DEMO] would send '{char}'")

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None


# =====================================================================
# BLINK DETECTOR - turns the stream of EAR values into events
# =====================================================================
class BlinkDetector:
    def __init__(self):
        self.threshold = EAR_THRESHOLD or EAR_FALLBACK
        self.closed = False          # are the eyes closed right now?
        self.close_start = 0.0       # when they closed
        self.hold_fired = False      # HOLD already sent for this close?
        self.blink_times = deque()   # times of recent short blinks

    def update(self, ear, now):
        """Feed one EAR reading. Returns a list of events: 'BLINK', 'RAPID', 'HOLD'."""
        events = []
        if not self.closed and ear < self.threshold:
            # eyes just closed
            self.closed = True
            self.close_start = now
            self.hold_fired = False
        elif self.closed and ear > self.threshold + EAR_HYSTERESIS:
            # eyes just opened - how long were they shut?
            self.closed = False
            if now - self.close_start < BLINK_MAX:
                events.append("BLINK")
                self.blink_times.append(now)
                # forget blinks older than the rapid-blink window
                while self.blink_times and now - self.blink_times[0] > RAPID_BLINK_WINDOW:
                    self.blink_times.popleft()
                if len(self.blink_times) >= RAPID_BLINK_COUNT:
                    self.blink_times.clear()
                    events.append("RAPID")
        # long close: fire HOLD once per close
        if self.closed and not self.hold_fired and now - self.close_start >= SELECT_HOLD:
            self.hold_fired = True
            events.append("HOLD")
        return events

    def hold_progress(self, now):
        """0.0 to 1.0 - how far through a 3-second hold we are (for the progress bar)."""
        if not self.closed or self.hold_fired:
            return 0.0
        return min(1.0, (now - self.close_start) / SELECT_HOLD)

    def reset(self):
        """Called when the face is lost, so a half-finished close never counts."""
        self.closed = False
        self.hold_fired = False


# =====================================================================
# THE APP
# =====================================================================
class BlinkBuddyApp:
    def __init__(self, root):
        self.root = root
        root.title("BlinkBuddy")
        root.configure(bg=BG)
        root.geometry("1280x720")
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # --- hardware ---
        self.arduino = ArduinoLink(SERIAL_PORT, BAUD_RATE)
        self.arduino.send("Z")       # start with fan, lamp, LEDs and buzzer all off, matching the screen
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY   # DSHOW opens faster on Windows
        self.cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        self.camera_ok = self.cap.isOpened()
        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=False,
            min_detection_confidence=0.5, min_tracking_confidence=0.5)
        self.detector = BlinkDetector()

        # --- state ---
        self.mode = "CALIBRATE"      # CALIBRATE | SCAN | CONFIRM | SOS
        self.calibrated = False
        self.calib_samples = []
        self.calib_time = 0.0
        self.highlight = 0           # index of the highlighted tile
        self.scan_elapsed = 0.0      # time spent on the current tile (pauses while eyes closed)
        self.pending_tile = None     # tile waiting for confirmation
        self.confirm_start = 0.0
        self.light_on = False
        self.fan_on = False
        self.nurse_called = False

        # --- camera / face data ---
        self.frame = None
        self.eye_points = []
        self.face_visible = False
        self.ear = 0.0
        self.preview_photo = None    # keep a reference or Tkinter drops the image

        self.log = []                # in-memory event log: (time, text)
        self.last_action = "-"
        self.last_tick = time.monotonic()

        root.bind("<Key>", self.on_key)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log_event("App started" + ("" if self.arduino.connected else " (demo mode, Arduino not connected)"))
        if not self.camera_ok:
            self.log_event("Camera not found - keyboard only")
        self.tick()

    # -----------------------------------------------------------------
    # MAIN LOOP
    # -----------------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        dt = now - self.last_tick
        self.last_tick = now

        self.read_camera()
        if self.face_visible:
            events = self.detector.update(self.ear, now)
        else:
            self.detector.reset()
            events = []
        self.update_state(now, dt, events)
        self.draw(now)
        self.after_id = self.root.after(LOOP_MS, self.tick)

    def read_camera(self):
        """Grab a frame, run Face Mesh, compute EAR from both eyes."""
        ok, frame = self.cap.read() if self.camera_ok else (False, None)
        if not ok:
            self.frame, self.eye_points, self.face_visible = None, [], False
            return
        frame = cv2.flip(frame, 1)            # mirror image, feels natural
        h, w = frame.shape[:2]
        results = self.face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self.frame = frame
        if not results.multi_face_landmarks:
            self.eye_points, self.face_visible = [], False
            return
        lm = results.multi_face_landmarks[0].landmark
        # landmarks are 0..1, convert to pixels so width and height use the same units
        left = [(lm[i].x * w, lm[i].y * h) for i in LEFT_EYE]
        right = [(lm[i].x * w, lm[i].y * h) for i in RIGHT_EYE]
        self.ear = (eye_aspect_ratio(left) + eye_aspect_ratio(right)) / 2.0
        self.eye_points = [left, right]
        self.face_visible = True

    # -----------------------------------------------------------------
    # STATE MACHINE
    # -----------------------------------------------------------------
    def update_state(self, now, dt, events):
        if self.mode == "CALIBRATE":
            self.update_calibration(dt)
            return
        if self.mode == "SOS":
            return                                    # only R (reset) leaves SOS

        if "RAPID" in events:                         # works from any screen, no confirmation
            self.perform_action("SOS", "rapid blinks")
            return
        if "HOLD" in events:
            self.on_long_close(now)

        if self.mode == "SCAN":
            # move on only while the face is visible and the eyes are open
            if self.face_visible and not self.detector.closed:
                self.scan_elapsed += dt
                if self.scan_elapsed >= SCAN_INTERVAL:
                    self.scan_elapsed = 0.0
                    self.highlight = (self.highlight + 1) % len(TILES)

        elif self.mode == "CONFIRM":
            hold_in_progress = self.detector.hold_progress(now) > 0
            # time out, but don't cut off a confirm-hold that has already started
            if now - self.confirm_start >= CONFIRM_WINDOW and not hold_in_progress:
                self.log_event(f"Cancelled: {self.pending_tile['name']} (not confirmed)")
                self.back_to_scan()

    def on_long_close(self, now):
        """A 3-second eye close, or Space on the keyboard."""
        if self.mode == "SCAN":
            self.pending_tile = TILES[self.highlight]
            self.confirm_start = now
            self.mode = "CONFIRM"
            self.log_event(f"Selected: {self.pending_tile['name']} - waiting for confirmation")
        elif self.mode == "CONFIRM":
            tile = self.pending_tile
            self.back_to_scan()
            self.perform_action(tile["id"], "confirmed")

    def back_to_scan(self):
        self.mode = "SCAN"
        self.pending_tile = None
        self.scan_elapsed = 0.0

    def start_calibration(self):
        self.mode = "CALIBRATE"
        self.calib_samples = []
        self.calib_time = 0.0

    def update_calibration(self, dt):
        # only count time while a face is visible, so calibration waits for the patient
        if self.face_visible:
            self.calib_samples.append(self.ear)
            self.calib_time += dt
        if self.calib_time >= CALIBRATION_SECONDS and self.calib_samples:
            open_ear = statistics.median(self.calib_samples)   # median ignores the odd blink
            threshold = EAR_THRESHOLD or open_ear * EAR_CLOSED_RATIO
            self.finish_calibration(threshold, f"open-eye EAR {open_ear:.3f}")

    def finish_calibration(self, threshold, note):
        self.detector.threshold = threshold
        self.log_event(f"Calibrated: {note}, closed threshold {threshold:.3f}")
        self.back_to_scan()
        self.highlight = 0
        if not self.calibrated:
            self.calibrated = True
            self.perform_action("READY")

    # -----------------------------------------------------------------
    # ACTIONS - the ONE place where things actually happen.
    # Every action (blink, keyboard, later a nurse phone page) comes through here.
    # -----------------------------------------------------------------
    def perform_action(self, action, source=""):
        if action == "SOS":
            self.mode = "SOS"
            self.pending_tile = None
            self.arduino.send("S")
            text = "SOS ALERT"
        elif action == "NURSE":
            self.nurse_called = True
            self.arduino.send("N")
            text = "Nurse called"
        elif action == "LIGHT":
            self.light_on = not self.light_on
            self.arduino.send("L" if self.light_on else "l")
            text = f"Light {'ON' if self.light_on else 'OFF'}"
        elif action == "FAN":
            self.fan_on = not self.fan_on
            self.arduino.send("F" if self.fan_on else "f")
            text = f"Fan {'ON' if self.fan_on else 'OFF'}"
        elif action == "RESET":
            self.nurse_called = False
            self.detector.blink_times.clear()
            if self.mode == "SOS":
                if self.calibrated:
                    self.back_to_scan()
                else:
                    self.start_calibration()
            self.arduino.send("X")
            text = "SOS / nurse call cleared"
        elif action == "READY":
            self.arduino.send("G")
            text = "App ready"
        # elif action == "MEDICINE":
        #     self.arduino.send("M")
        #     text = "Medicine requested"
        else:
            text = f"Unknown action: {action}"
        self.log_event(text + (f" ({source})" if source else ""))
        # Later: notify_nurse_phone(action, text) would go here.

    def log_event(self, text):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log.append((stamp, text))
        self.last_action = f"{text}  @ {stamp[11:]}"
        print(f"[{stamp}] {text}")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {text}\n")
        except OSError:
            pass

    # -----------------------------------------------------------------
    # KEYBOARD BACKUP
    # -----------------------------------------------------------------
    def on_key(self, event):
        key = event.keysym.lower()
        if key == "escape":
            self.quit()
        elif key == "space":
            if self.mode == "CALIBRATE":
                self.finish_calibration(EAR_THRESHOLD or EAR_FALLBACK, "skipped with Space")
            elif self.mode in ("SCAN", "CONFIRM"):
                self.on_long_close(time.monotonic())
        elif key == "s" and self.mode != "SOS":
            self.perform_action("SOS", "keyboard")
        elif key == "r":
            self.perform_action("RESET", "keyboard")
        elif key == "c" and self.mode != "SOS":
            self.start_calibration()
        elif key == "up":
            self.detector.threshold += 0.01
        elif key == "down":
            self.detector.threshold = max(0.05, self.detector.threshold - 0.01)
        elif key == "f11":
            self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    # -----------------------------------------------------------------
    # DRAWING - the whole screen is redrawn on one canvas every frame
    # -----------------------------------------------------------------
    def tile_label(self, tile):
        if tile["id"] == "LIGHT":
            return f"LIGHT\n{'ON' if self.light_on else 'OFF'}"
        if tile["id"] == "FAN":
            return f"FAN\n{'ON' if self.fan_on else 'OFF'}"
        if tile["id"] == "NURSE" and self.nurse_called:
            return "NURSE\nCALLED"
        return tile["name"]

    def confirm_text(self, tile):
        if tile["id"] == "LIGHT":
            return f"TURN LIGHT {'OFF' if self.light_on else 'ON'}"
        if tile["id"] == "FAN":
            return f"TURN FAN {'OFF' if self.fan_on else 'ON'}"
        if tile["id"] == "NURSE":
            return "CALL THE NURSE"
        if tile["id"] == "SOS":
            return "SEND SOS"
        return tile["name"]

    def draw(self, now):
        c = self.canvas
        c.delete("all")
        W = max(c.winfo_width(), 800)
        H = max(c.winfo_height(), 500)

        if self.mode == "SOS":
            self.draw_sos(W, H, now)
            return

        pad = int(H * 0.02)
        header_h = int(H * 0.13)
        status_h = int(H * 0.07)
        side_w = int(W * 0.26)

        # title bar
        c.create_text(pad * 2, header_h * 0.42, text="BlinkBuddy", anchor="w",
                      fill=TEXT, font=font(header_h * 0.5, True))
        c.create_text(pad * 2, header_h * 0.84, text="Every blink is a decision.", anchor="w",
                      fill=MUTED, font=font(header_h * 0.2))

        area = (pad, header_h, W - side_w - pad, H - status_h - pad)
        if self.mode == "CALIBRATE":
            self.draw_calibration(area, H)
        elif self.mode == "CONFIRM":
            self.draw_confirm(area, now)
        else:
            self.draw_tiles(area, now, pad, H)

        if not self.face_visible:
            msg = "Face not visible" if self.camera_ok else "Camera not found - use keyboard"
            x0, y0, x1, y1 = area
            cy = (y0 + y1) / 2
            c.create_rectangle(x0, cy - H * 0.07, x1, cy + H * 0.07, fill="#000000", outline=BAD, width=4)
            c.create_text((x0 + x1) / 2, cy, text=msg, fill=BAD, font=font(H * 0.07, True))

        self.draw_sidebar(W - side_w, pad, side_w - pad, H)
        self.draw_status(W, H, status_h, pad)

    def draw_tiles(self, area, now, gap, H):
        c = self.canvas
        x0, y0, x1, y1 = area
        n = len(TILES)
        cols = math.ceil(math.sqrt(n))          # 4 tiles -> 2x2, 5-6 tiles -> 3x2
        rows = math.ceil(n / cols)
        tw = (x1 - x0 - gap * (cols - 1)) / cols
        th = (y1 - y0 - gap * (rows - 1)) / rows
        label_px = min(th * 0.2, tw * 0.11)

        for i, tile in enumerate(TILES):
            r, col = divmod(i, cols)
            tx0 = x0 + col * (tw + gap)
            ty0 = y0 + r * (th + gap)
            tx1, ty1 = tx0 + tw, ty0 + th

            if i == self.highlight:
                grow = gap * 0.45                   # highlighted tile is slightly larger
                tx0, ty0, tx1, ty1 = tx0 - grow, ty0 - grow, tx1 + grow, ty1 + grow
                fg = tile["text_on_fill"]
                c.create_rectangle(tx0, ty0, tx1, ty1, fill=tile["color"],
                                   outline="#ffffff", width=max(6, int(H * 0.012)))
                # progress bar while the eyes are held closed
                progress = self.detector.hold_progress(now) if self.face_visible else 0.0
                if progress > 0:
                    m = (tx1 - tx0) * 0.08
                    bh = (ty1 - ty0) * 0.09
                    bx0, by1 = tx0 + m, ty1 - m * 0.6
                    bx1, by0 = tx1 - m, by1 - bh
                    c.create_rectangle(bx0, by0, bx1, by1, outline=fg, width=3)
                    c.create_rectangle(bx0, by0, bx0 + (bx1 - bx0) * progress, by1, fill=fg, outline="")
                    c.create_text((tx0 + tx1) / 2, by0 - bh * 0.6, text="Keep eyes closed...",
                                  fill=fg, font=font(label_px * 0.4, True))
            else:
                fg = tile["color"]
                c.create_rectangle(tx0, ty0, tx1, ty1, fill=PANEL, outline=tile["color"], width=3)

            c.create_text((tx0 + tx1) / 2, (ty0 + ty1) / 2 - th * 0.05, text=self.tile_label(tile),
                          fill=fg, font=font(label_px, True), justify="center")

    def draw_confirm(self, area, now):
        c = self.canvas
        x0, y0, x1, y1 = area
        h = y1 - y0
        cx = (x0 + x1) / 2
        tile = self.pending_tile
        remaining = max(0.0, CONFIRM_WINDOW - (now - self.confirm_start))

        c.create_rectangle(x0, y0, x1, y1, fill=PANEL, outline=tile["color"], width=8)
        c.create_text(cx, y0 + h * 0.14, text="Are you sure?", fill=TEXT, font=font(h * 0.11, True))
        c.create_text(cx, y0 + h * 0.31, text=self.confirm_text(tile), fill=tile["color"],
                      font=font(h * 0.12, True))
        c.create_text(cx, y0 + h * 0.48, text=f"Close your eyes for {SELECT_HOLD:g} s to confirm",
                      fill=TEXT, font=font(h * 0.055))
        c.create_text(cx, y0 + h * 0.66, text=f"{math.ceil(remaining)}", fill=MUTED,
                      font=font(h * 0.16, True))

        if self.detector.closed and self.detector.hold_fired:
            c.create_text(cx, y0 + h * 0.86, text="Open your eyes first, then close them to confirm",
                          fill=MUTED, font=font(h * 0.04))
        else:
            progress = self.detector.hold_progress(now) if self.face_visible else 0.0
            bw = (x1 - x0) * 0.6
            bx0, by0 = cx - bw / 2, y0 + h * 0.82
            c.create_rectangle(bx0, by0, bx0 + bw, by0 + h * 0.07, outline=tile["color"], width=3)
            if progress > 0:
                c.create_rectangle(bx0, by0, bx0 + bw * progress, by0 + h * 0.07,
                                   fill=tile["color"], outline="")

    def draw_calibration(self, area, H):
        c = self.canvas
        x0, y0, x1, y1 = area
        h = y1 - y0
        cx = (x0 + x1) / 2
        c.create_rectangle(x0, y0, x1, y1, fill=PANEL, outline=MUTED, width=3)
        c.create_text(cx, y0 + h * 0.22, text="Calibrating...", fill=TEXT, font=font(h * 0.11, True))
        c.create_text(cx, y0 + h * 0.42, text="Look at the camera, eyes open", fill=TEXT,
                      font=font(h * 0.07))
        bw = (x1 - x0) * 0.6
        bx0, by0 = cx - bw / 2, y0 + h * 0.58
        progress = min(1.0, self.calib_time / CALIBRATION_SECONDS)
        c.create_rectangle(bx0, by0, bx0 + bw, by0 + h * 0.07, outline=GOOD, width=3)
        c.create_rectangle(bx0, by0, bx0 + bw * progress, by0 + h * 0.07, fill=GOOD, outline="")
        c.create_text(cx, y0 + h * 0.82, text="Space = skip and use the default threshold",
                      fill=MUTED, font=font(h * 0.04))

    def draw_sos(self, W, H, now):
        c = self.canvas
        flash = int(now * 2.5) % 2 == 0           # flips about 2.5 times a second
        c.create_rectangle(0, 0, W, H, fill="#ff0000" if flash else "#4a0000", outline="")
        c.create_text(W / 2, H * 0.36, text="SOS", fill="white", font=font(H * 0.3, True))
        c.create_text(W / 2, H * 0.62, text="HELP NEEDED", fill="white", font=font(H * 0.1, True))
        c.create_text(W / 2, H * 0.85, text="Press R to reset", fill="white", font=font(H * 0.045))

    def draw_sidebar(self, x, y, w, H):
        c = self.canvas
        ph = int(w * 3 / 4)
        if self.frame is not None:
            img = self.frame.copy()
            colour = (60, 60, 255) if self.detector.closed else (80, 220, 60)   # BGR: red / green
            for eye in self.eye_points:
                pts = np.array(eye, dtype=np.int32)
                cv2.polylines(img, [pts], True, colour, 2)
                for px, py in pts:
                    cv2.circle(img, (int(px), int(py)), 3, colour, -1)
            img = cv2.cvtColor(cv2.resize(img, (w, ph)), cv2.COLOR_BGR2RGB)
            self.preview_photo = ImageTk.PhotoImage(Image.fromarray(img))
            c.create_image(x, y, image=self.preview_photo, anchor="nw")
        else:
            c.create_rectangle(x, y, x + w, y + ph, fill=PANEL, outline=MUTED)
            c.create_text(x + w / 2, y + ph / 2, text="No camera", fill=MUTED, font=font(H * 0.03))

        ty = y + ph + H * 0.03
        eyes = "CLOSED" if self.detector.closed else "OPEN"
        c.create_text(x, ty, anchor="w", fill=TEXT, font=font(H * 0.028, True),
                      text=f"EAR {self.ear:.3f}   eyes {eyes}" if self.face_visible else "EAR  -")
        c.create_text(x, ty + H * 0.04, anchor="w", fill=MUTED, font=font(H * 0.024),
                      text=f"closed below {self.detector.threshold:.3f}  (Up/Down to adjust)")
        keys = ("Space  select / confirm\nS  SOS      R  reset\n"
                "C  recalibrate\nF11  fullscreen   Esc  quit")
        c.create_text(x, ty + H * 0.09, anchor="nw", fill=MUTED, font=font(H * 0.024), text=keys)

    def draw_status(self, W, H, status_h, pad):
        c = self.canvas
        y0 = H - status_h
        cy = y0 + status_h / 2
        c.create_rectangle(0, y0, W, H, fill=PANEL, outline="")
        fs = font(status_h * 0.36)
        c.create_text(pad, cy, anchor="w", fill=TEXT, font=fs, text=f"Last: {self.last_action}")
        states = f"Light {'ON' if self.light_on else 'OFF'}   Fan {'ON' if self.fan_on else 'OFF'}"
        item = c.create_text(W - pad, cy, anchor="e", fill=TEXT, font=fs, text=states)
        left_edge = c.bbox(item)[0]
        if self.arduino.connected:
            text, colour = f"Arduino: connected ({self.arduino.port})", GOOD
        else:
            text, colour = "Arduino: not connected (demo)", BAD
        c.create_text(left_edge - pad * 2, cy, anchor="e", fill=colour, font=font(status_h * 0.36, True),
                      text=text)

    # -----------------------------------------------------------------
    def quit(self):
        self.log_event("App closed")
        self.root.after_cancel(self.after_id)
        self.cap.release()
        self.face_mesh.close()
        self.arduino.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    BlinkBuddyApp(root)
    root.mainloop()
