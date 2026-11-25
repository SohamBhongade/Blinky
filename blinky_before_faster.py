"""
Blinky - control your room with eye blinks.
Technovate hackathon - theme: "The Last Human Decision"
Tagline: "Every blink is a decision."

How the program works, in one loop (tick) that runs about 30 times a second:
  1. Read a webcam frame.
  2. MediaPipe Face Mesh finds 468 points on the face; we take 6 around each eye.
  3. Turn those points into one number per frame: the Eye Aspect Ratio (EAR).
     Open eye = high EAR, closed eye = low EAR.
  4. The BlinkDetector turns the EAR stream into events:
       BLINK = short close (< 0.5 s)       -> ignored, except for counting rapid blinks
       HOLD  = eyes closed for 2 s         -> select (2.5 s to confirm)
       RAPID = 5 short blinks within 3 s   -> instant SOS
  5. The app's state machine reacts to events:
       CALIBRATE -> SCAN -> CONFIRM -> DONE -> SCAN   (CANCELLED if not confirmed)
       SOS can start from any screen.
  6. Redraw the screen (everything is drawn on one Tkinter canvas).

Everything runs offline: MediaPipe's face model is inside the pip package,
and all icons are drawn with canvas shapes (no image files, no web fonts).
"""

import io
import json
import math
import os
import queue
import hashlib
import statistics
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import wave
from collections import deque
from datetime import datetime

import cv2
import mediapipe as mp
import numpy as np
import serial                      # pyserial
import serial.tools.list_ports     # to find the HC-05 COM port automatically
from PIL import Image, ImageTk     # Pillow, to show camera frames in Tkinter

# Optional extras. If they are missing, the feature is simply switched off.
try:
    import pyttsx3                 # offline text-to-speech (key V)
except Exception:
    pyttsx3 = None
try:
    import winsound                # Windows-only beep (key B)
except ImportError:
    winsound = None

# =====================================================================
# SETTINGS - tune these on the day
# =====================================================================
SERIAL_PORT = "COM5"     # HC-05 *Outgoing* COM port - tried first; if it fails the app
                         # searches the other Bluetooth COM ports and keeps retrying
SERIAL_RETRY_S = 4.0     # seconds between reconnect attempts while not connected
BAUD_RATE = 9600         # must match the HC-05 and the Arduino sketch
CAMERA_INDEX = 0         # 0 = built-in webcam, 1 = first USB webcam

SCAN_INTERVAL = 3.0          # seconds the highlight stays on each tile
SELECT_HOLD = 2.0            # seconds of closed eyes to select a tile
CONFIRM_HOLD = 2.5           # seconds of closed eyes to say YES on "Are you sure?"
CONFIRM_WINDOW = 8.0         # seconds the patient has to confirm (the voice prompt takes ~3 s of this)
BLINK_MAX = 0.5              # closes shorter than this are normal blinks
RAPID_BLINK_COUNT = 5        # this many quick blinks ...
RAPID_BLINK_WINDOW = 3.0     # ... within this many seconds = SOS

CALIBRATION_SECONDS = 3.0    # how long we measure the open-eye EAR at start
EAR_THRESHOLD = None         # None = set automatically by calibration.
                             # Put a number (e.g. 0.20) here to force it.
EAR_CLOSED_RATIO = 0.70      # auto threshold = open-eye EAR x this ratio
EAR_HYSTERESIS = 0.02        # eyes only count as open again above threshold + this
EAR_FALLBACK = 0.20          # used if calibration is skipped with Space

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "blinky_log.txt")
# New mediapipe (0.10.30 and later) needs this model file in the same folder.
# Download once (internet needed only for this):
# https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
FACE_MODEL = os.path.join(HERE, "face_landmarker.task")
LOOP_MS = 15                 # main loop delay (the camera read sets the real pace)

# Sound cues (made in code at start-up, played through the laptop speakers)
SOUND_CUES = True            # tone when "Are you sure?" opens, chime when an action is done
SPEAK_CUES = True            # voice says the choice ("Turn light on. Are you sure?") and the result ("Done. Light is on.")
TONE_VOLUME = 0.35           # beeps: 0.0 to 1.0 (kept softer than the voice)
VOICE_VOLUME = 1.0           # voice: 0.0 to 1.0
VOICE_BOOST = 2.5            # makes the voice louder (1 = as recorded, 3 = very loud)
VOICE_SPEED = 0              # -10 (slow) to 10 (fast); 0 = normal Windows voice speed
VOICE_GAP_MS = 80            # tiny pause between the beep and the voice
HOLD_TICKS = (0.35, 0.6, 0.85)   # while the eyes are held closed: a tick at these points of the hold,
                                 # each one higher, so the patient HEARS the count (their eyes are shut)
KEEP_AUDIO_AWAKE = True      # loop an inaudible sound so cues play with no delay

# Caretaker phone alerts with the free ntfy app (needs internet on laptop AND phone,
# e.g. connect the laptop to the phone's mobile hotspot).
# On the phone: ntfy app -> + -> topic name below -> Subscribe.
NTFY_ENABLED = True
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = "blinky-q88rod"   # keep this secret: anyone with the name can read it
SOS_ALERT_REPEATS = 5        # SOS alerts the phone this many times ...
SOS_ALERT_GAP = 3.0          # ... this many seconds apart (stops early on "On my way" or R)

# Screen timings
DONE_SECONDS = 2.0           # "Light is ON" screen after an action
CANCEL_SECONDS = 1.5         # "Cancelled. Nothing changed." screen
FADE_SECONDS = 0.2           # new highlighted tile fades in over this time
FAN_TURNS_PER_SECOND = 0.5   # fan icon spins 1 turn every 2 s when the fan is ON
SLEEP_AFTER = 20.0           # eyes closed this long = patient is asleep, scanning pauses
WAKE_AFTER = 1.5             # eyes open this long = patient is awake again
SOS_PULSE_HZ = 1.0           # SOS screen pulses per second. Keep at 2 or LOWER
                             # (photosensitive epilepsy safety).

# =====================================================================
# MENU TILES - to add a tile, add a line here and a branch in perform_action()
# "say" is what the voice reads out (key V).
# =====================================================================
TILES = [
    {"id": "SOS",   "name": "SOS",        "color": "#F2553F", "text_on_fill": "#FFFFFF", "say": "S O S"},
    {"id": "NURSE", "name": "CALL NURSE", "color": "#6352DD", "text_on_fill": "#FFFFFF", "say": "Call nurse"},
    {"id": "LIGHT", "name": "LIGHT",      "color": "#FFB400", "text_on_fill": "#151515", "say": "Light"},
    {"id": "FAN",   "name": "FAN",        "color": "#3478F6", "text_on_fill": "#FFFFFF", "say": "Fan"},
    # {"id": "MEDICINE", "name": "MEDICINE", "color": "#19B83A", "text_on_fill": "#FFFFFF", "say": "Medicine"},
]
TILE_BY_ID = {t["id"]: t for t in TILES}

# Colours: light, friendly theme (warm off-white, white cards, black pills, purple accent).
# Only SOS uses strong red.
BG = "#F3F1F0"        # warm off-white background
PANEL = "#FFFFFF"     # white cards / idle tiles
SHADOW = "#E2DEDB"    # soft shadow under cards
TEXT = "#151515"      # main text (near black)
MUTED = "#8C8C93"     # soft grey text
INK = "#151515"       # black pills and buttons
PURPLE = "#6352DD"    # main accent
YELLOW = "#FFB400"
BLUE = "#3478F6"
GREEN = "#19B83A"     # good / connected / done
RED = "#F2553F"       # SOS / problem
FONT = "Segoe UI"
FONT_FALLBACK = "Arial"

# Face Mesh landmark numbers for the 6 eye points, in EAR order:
# p1 = one corner, p2 & p3 = upper lid, p4 = other corner, p5 & p6 = lower lid
LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]


def eye_aspect_ratio(p):
    """EAR = (two vertical eye openings) / (2 x eye width).
    About 0.3 when open, drops towards 0.1 when closed."""
    vertical = math.dist(p[1], p[5]) + math.dist(p[2], p[4])
    horizontal = math.dist(p[0], p[3])
    return vertical / (2.0 * horizontal) if horizontal > 0 else 0.0


# =====================================================================
# SMALL DRAWING HELPERS
# =====================================================================
def mix(c1, c2, t):
    """Blend two '#RRGGBB' colours. t=0 gives c1, t=1 gives c2."""
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def round_rect(c, x0, y0, x1, y1, r, **kw):
    """Rounded rectangle: a smoothed polygon. Doubled points keep the edges straight."""
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0,
           x1, y0 + r, x1, y0 + r, x1, y1 - r, x1, y1 - r, x1, y1,
           x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1, x0, y1,
           x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


# =====================================================================
# ARDUINO LINK - sends single characters over Bluetooth (HC-05 = COM port)
# =====================================================================
class ArduinoLink:
    """Talks to the HC-05. Finds the right COM port by itself and reconnects
    in the background, so the app never gets stuck in demo mode."""

    def __init__(self, port, baud):
        self.preferred = port
        self.baud = baud
        self.port = port
        self.ser = None
        self.lock = threading.Lock()
        self.messages = deque()        # status lines for the app's log (read on the UI thread)
        self.just_connected = False    # app re-sends light/fan state when this flips on
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.searching = True
        self.last_error = ""
        threading.Thread(target=self._keep_connected, daemon=True).start()

    @property
    def connected(self):
        return self.ser is not None

    # ---- finding the port -------------------------------------------------
    def _candidates(self):
        """Configured port first, then every *outgoing* Bluetooth serial port."""
        try:
            ports = list(serial.tools.list_ports.comports())
        except Exception:
            ports = []
        names = [self.preferred]
        for p in ports:
            hwid = (p.hwid or "").upper()
            desc = (p.description or "").lower()
            is_bt = "BTHENUM" in hwid or "bluetooth" in desc
            # Windows makes two COM ports per HC-05. The "Incoming" one has an
            # all-zero address in its hardware id and never reaches the board.
            incoming = "000000000000" in hwid
            if is_bt and not incoming and p.device not in names:
                names.append(p.device)
        return names

    def _try_open(self, name):
        try:
            ser = serial.Serial(name, self.baud, timeout=0, write_timeout=1)
            return ser, None
        except (serial.SerialException, OSError, ValueError) as e:
            return None, e

    def _keep_connected(self):
        while not self._stop.is_set():
            if self.ser is None:
                self.searching = True
                errors = []
                for name in self._candidates():
                    if self._stop.is_set():
                        return
                    ser, err = self._try_open(name)
                    if ser:
                        self.last_error = ""
                        with self.lock:
                            self.ser, self.port = ser, name
                        self.just_connected = True
                        self.messages.append(f"Arduino connected on {name}")
                        print(f"Arduino connected on {name}.")
                        break
                    errors.append(f"{name}: {err}")
                self.searching = False
                if self.ser is None and errors:
                    msg = "Arduino not found - " + " | ".join(errors)
                    if "denied" in msg.lower():
                        msg += "  -> a port is busy: close the Arduino IDE Serial Monitor / other Blinky windows"
                    if msg != self.last_error:          # log it once, not every retry
                        self.last_error = msg
                        self.messages.append(msg)
            self._wake.wait(SERIAL_RETRY_S)
            self._wake.clear()

    def retry_now(self):
        self._wake.set()

    # ---- sending ----------------------------------------------------------
    def send(self, char):
        with self.lock:
            ser = self.ser
            if ser:
                try:
                    ser.reset_input_buffer()   # we never read; throw away anything the board sent
                    ser.write(char.encode("ascii"))
                    print(f"[Arduino] sent '{char}'")
                    return
                except (serial.SerialException, OSError) as e:
                    print(f"[Arduino] send failed ({e}). Reconnecting ...")
                    self.messages.append(f"Arduino link lost on {self.port} - reconnecting")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    self.ser = None
                    self._wake.set()
        print(f"[DEMO] would send '{char}'")

    def close(self):
        self._stop.set()
        self._wake.set()
        with self.lock:
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
        self.hold_seconds = SELECT_HOLD   # how long a HOLD is (the app changes it on the confirm screen)

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
        if self.closed and not self.hold_fired and now - self.close_start >= self.hold_seconds:
            self.hold_fired = True
            events.append("HOLD")
        return events

    def hold_progress(self, now):
        """0.0 to 1.0 - how far through the hold we are (for the progress ring)."""
        if not self.closed or self.hold_fired:
            return 0.0
        return min(1.0, (now - self.close_start) / self.hold_seconds)

    def reset(self):
        """Called when the face is lost, so a half-finished close never counts."""
        self.closed = False
        self.hold_fired = False


# =====================================================================
# FACE FINDER - gets the 468 face points from MediaPipe.
# Works with old mediapipe (mp.solutions) and new mediapipe (Tasks API).
# The face points have the same numbers in both, so the eye lists still work.
# =====================================================================
class FaceFinder:
    def __init__(self):
        self.old = None              # old mediapipe: FaceMesh
        self.new = None              # new mediapipe: FaceLandmarker
        self.error = ""
        self.last_ms = 0
        if hasattr(mp, "solutions"):
            self.old = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=False,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            return
        try:
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision
            with open(FACE_MODEL, "rb") as f:     # read the file ourselves (safer on Windows)
                model = f.read()
            options = vision.FaceLandmarkerOptions(
                base_options=mp_tasks.BaseOptions(model_asset_buffer=model),
                running_mode=vision.RunningMode.VIDEO, num_faces=1,
                min_face_detection_confidence=0.5, min_face_presence_confidence=0.5,
                min_tracking_confidence=0.5)
            self.new = vision.FaceLandmarker.create_from_options(options)
        except FileNotFoundError:
            self.error = "face_landmarker.task is missing"
        except Exception as e:
            self.error = f"face model failed to load ({e})"
        if self.error:
            print(f"FACE MODEL PROBLEM: {self.error}. Put face_landmarker.task next to blinky.py.")

    @property
    def ok(self):
        return self.old is not None or self.new is not None

    def find(self, rgb):
        """Returns the face points (each has .x and .y from 0 to 1), or None if no face."""
        if self.old:
            results = self.old.process(rgb)
            return results.multi_face_landmarks[0].landmark if results.multi_face_landmarks else None
        if self.new:
            ms = max(self.last_ms + 1, int(time.monotonic() * 1000))   # time must always go up
            self.last_ms = ms
            result = self.new.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ms)
            return result.face_landmarks[0] if result.face_landmarks else None
        return None

    def close(self):
        for f in (self.old, self.new):
            if f:
                f.close()


# =====================================================================
# SOUNDS AND VOICE - beeps AND spoken sentences play from ONE queue, one after
# the other, so they can never talk over each other or cut each other off.
#  - Beeps are made with numpy at start-up.
#  - Each sentence is recorded ONCE to a .wav file with Windows' own voice
#    (PowerShell System.Speech; pyttsx3 as a backup), made louder, and kept in memory.
#    The files are cached, so the next start-up is instant.
#  - A beep and its sentence are joined into ONE sound, so the voice starts the
#    moment the beep ends: no overlap, no extra delay.
#  - A silent sound loops in the background, so the speakers never go to sleep
#    (a sleeping sound card is what makes the first beep late).
# =====================================================================
class SoundPlayer:
    RATE = 22050
    TONES = {
        "CONFIRM": [(740, 110), (988, 170)],               # two quick notes going up = a question
        "DONE":    [(784, 110), (1047, 110), (1319, 240)], # three notes going up = success
        "TICK":    [(800, 40)],                            # soft tick when the highlight moves
        "HOLD1":   [(523, 70)],                            # eyes-closed count: three ticks going up...
        "HOLD2":   [(659, 70)],
        "HOLD3":   [(784, 70)],                            # ...then the CONFIRM / DONE sound = "open your eyes"
    }

    def __init__(self):
        self.tones = {}              # name -> samples (numpy floats, -1..1)
        self.voice = {}              # sentence -> samples
        self.voice_state = "preparing" if winsound else "n/a"
        self.silence = None          # path of the silent keep-awake sound
        self.folder = os.path.join(tempfile.gettempdir(), "blinky_sounds")
        self.q = queue.Queue()
        self.busy = False            # True while a sound is playing
        self.on_voice_ready = None   # called once the voice recordings are loaded
        if not winsound:
            return
        try:
            os.makedirs(self.folder, exist_ok=True)
            for name, notes in self.TONES.items():
                self.tones[name] = self.make_tone(notes)
            if KEEP_AUDIO_AWAKE:
                self.silence = os.path.join(self.folder, "keep_awake.wav")
                hush = np.random.uniform(-1, 1, self.RATE) * 2 / 32767   # far too quiet to hear
                with open(self.silence, "wb") as f:
                    f.write(self.to_wav(hush))
        except OSError as e:
            print(f"Could not make sounds ({e}). Sounds are off.")
            self.tones = {}
        threading.Thread(target=self._run, daemon=True).start()
        self.q.put(None)             # start the keep-awake loop

    # ---------- making sounds ----------
    def make_tone(self, notes):
        parts = []
        for freq, ms in notes:
            t = np.arange(int(self.RATE * ms / 1000)) / self.RATE
            tone = np.sin(2 * np.pi * freq * t)
            fade = min(len(t) // 2, int(self.RATE * 0.006))     # 6 ms fade in/out, no clicks
            if fade:
                tone[:fade] *= np.linspace(0, 1, fade)
                tone[-fade:] *= np.linspace(1, 0, fade)
            parts += [tone * TONE_VOLUME, np.zeros(int(self.RATE * 0.025))]
        return np.concatenate(parts)

    def to_wav(self, samples):
        """numpy floats (-1..1) -> .wav file bytes in memory."""
        data = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(data.tobytes())
        return buffer.getvalue()

    # ---------- the voice ----------
    def voice_path(self, text):
        key = hashlib.md5(f"{VOICE_SPEED}|{text}".encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.folder, f"voice_{key}.wav")

    def prepare_voice(self, sentences):
        """Record every sentence once (in the background, the screen keeps running)."""
        if winsound:
            threading.Thread(target=self._prepare_voice, args=(list(dict.fromkeys(sentences)),),
                             daemon=True).start()

    def _prepare_voice(self, sentences):
        def missing():
            return [(t, self.voice_path(t)) for t in sentences
                    if not (os.path.exists(self.voice_path(t)) and os.path.getsize(self.voice_path(t)) > 2000)]
        if missing():
            self._record_powershell(missing())
        if missing() and pyttsx3:
            self._record_pyttsx3(missing())
        for text in sentences:
            try:
                self.voice[text] = self.load_voice(self.voice_path(text))
            except Exception:
                pass
        n = len(self.voice)
        self.voice_state = f"ready ({n}/{len(sentences)})" if n else "FAILED - beeps only"
        print(f"[Voice] {self.voice_state}")
        if n and self.on_voice_ready:
            self.on_voice_ready()

    def _record_powershell(self, items):
        """Windows' built-in voice, no extra installs needed."""
        try:
            listing = os.path.join(self.folder, "sentences.txt")
            script = os.path.join(self.folder, "record.ps1")
            with open(listing, "w", encoding="utf-8") as f:
                f.write("\n".join(f"{path}|{text}" for text, path in items))
            with open(script, "w", encoding="utf-8") as f:
                f.write("Add-Type -AssemblyName System.Speech\n"
                        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                        f"$s.Rate = {int(VOICE_SPEED)}\n"
                        "$s.Volume = 100\n"
                        f"Get-Content -Encoding UTF8 '{listing}' | ForEach-Object {{\n"
                        "  $p = $_.Split('|', 2)\n"
                        "  $s.SetOutputToWaveFile($p[0])\n"
                        "  $s.Speak($p[1])\n"
                        "  $s.SetOutputToNull()\n"
                        "}\n"
                        "$s.Dispose()\n")
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                           capture_output=True, timeout=90,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            print(f"[Voice] PowerShell voice failed ({e})")

    def _record_pyttsx3(self, items):
        try:
            try:
                import comtypes          # Windows voice needs COM started in this thread
                comtypes.CoInitialize()
            except Exception:
                pass
            engine = pyttsx3.init()
            engine.setProperty("rate", 170 + VOICE_SPEED * 10)
            for text, path in items:
                engine.save_to_file(text, path)
            engine.runAndWait()
        except Exception as e:
            print(f"[Voice] pyttsx3 voice failed ({e})")

    def load_voice(self, path):
        """Read a recorded sentence, cut the silence off both ends, and make it loud."""
        with wave.open(path, "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
        if width == 2:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768
        elif width == 1:
            x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
        else:
            raise ValueError("unsupported wav")
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        if rate != self.RATE:            # resample so it joins cleanly with the beeps
            x = np.interp(np.arange(0, len(x), rate / self.RATE), np.arange(len(x)), x)
        loud = np.nonzero(np.abs(x) > 0.02 * np.abs(x).max())[0]
        if len(loud) == 0:
            raise ValueError("silent recording")
        pad = int(self.RATE * 0.02)
        x = x[max(0, loud[0] - pad): loud[-1] + pad]        # no silence at the start = no delay
        x = x / np.abs(x).max()
        x = np.tanh(x * VOICE_BOOST) / np.tanh(VOICE_BOOST)  # louder, without crackling
        fade = int(self.RATE * 0.005)
        x[:fade] *= np.linspace(0, 1, fade)
        x[-fade:] *= np.linspace(1, 0, fade)
        return x * VOICE_VOLUME

    # ---------- playing ----------
    def play(self, tone=None, say=None, only_if_idle=False):
        """Play a beep, a sentence, or a beep followed straight away by a sentence.
        only_if_idle: skip it if something is already playing (used for the hold ticks,
        which are useless if they come late)."""
        if not winsound or (only_if_idle and (self.busy or not self.q.empty())):
            return
        parts = []
        if tone in self.tones:
            parts.append(self.tones[tone])
        if say:
            v = self.voice.get(say)
            if v is not None:
                if parts:
                    parts.append(np.zeros(int(self.RATE * VOICE_GAP_MS / 1000)))
                parts.append(v)
            elif self.voice_state.startswith("ready"):
                print(f"[Voice] no recording for: {say}")
        if not parts:
            return
        while not self.q.empty():        # only the newest sound matters - drop anything still waiting
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put(self.to_wav(np.concatenate(parts)))

    def _run(self):
        while True:
            clip = self.q.get()
            if clip is not None:
                self.busy = True
                try:   # plays from memory; this thread waits until it ends, the screen doesn't
                    winsound.PlaySound(clip, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
                except RuntimeError:
                    pass
                self.busy = False
            if self.silence and self.q.empty():
                try:   # back to the silent loop, to keep the speakers awake
                    winsound.PlaySound(self.silence, winsound.SND_FILENAME | winsound.SND_ASYNC |
                                       winsound.SND_LOOP | winsound.SND_NODEFAULT)
                except RuntimeError:
                    pass

    def stop(self):
        if winsound:
            try:
                winsound.PlaySound(None, 0)
            except RuntimeError:
                pass


# =====================================================================
# CARETAKER PHONE - sends ntfy notifications and listens for the "On my way" button.
# Runs in background threads, so a slow or missing internet never freezes the screen.
# =====================================================================
class PhoneNotifier:
    def __init__(self):
        self.status = "starting" if NTFY_ENABLED else "off"
        self.outbox = queue.Queue()      # notifications waiting to be sent
        self.inbox = queue.Queue()       # "On my way" taps from the phone
        if NTFY_ENABLED:
            threading.Thread(target=self._send_loop, daemon=True).start()
            threading.Thread(target=self._listen_loop, daemon=True).start()

    def send(self, title, message, priority=3, tags="", button=False):
        if NTFY_ENABLED:
            self.outbox.put((title, message, priority, tags, button))

    def _send_loop(self):
        while True:
            title, message, priority, tags, button = self.outbox.get()
            # ntfy's JSON format: title, text and buttons all travel together in one message
            note = {"topic": NTFY_TOPIC, "title": title, "message": message,
                    "priority": priority, "tags": [tags] if tags else []}
            if button:
                # a button on the notification; tapping it posts to our "-ack" topic
                note["actions"] = [{"action": "http", "label": "On my way",
                                    "url": f"{NTFY_SERVER}/{NTFY_TOPIC}-ack",
                                    "method": "POST", "body": "coming", "clear": True}]
            req = urllib.request.Request(NTFY_SERVER, data=json.dumps(note).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=5).close()
                self.status = f"sent {datetime.now():%H:%M:%S}"
                print(f"[Phone] sent: {title}")
            except Exception as e:
                self.status = "NOT sent - no internet?"
                print(f"[Phone] could not send '{title}' ({e})")

    def _listen_loop(self):
        url = f"{NTFY_SERVER}/{NTFY_TOPIC}-ack/json"     # stream of new messages, one JSON per line
        while True:
            try:
                with urllib.request.urlopen(url, timeout=90) as stream:
                    for line in stream:
                        msg = json.loads(line)
                        if msg.get("event") == "message":
                            self.inbox.put(msg.get("message", ""))
            except Exception:
                time.sleep(5)        # background thread, so waiting here is fine; then reconnect


# =====================================================================
# THE APP
# =====================================================================
class BlinkyApp:
    def __init__(self, root):
        self.root = root
        root.title("Blinky")
        root.configure(bg=BG)
        root.geometry("1280x720")
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # --- fonts: Segoe UI if installed, else Arial. Cached by size. ---
        self.font_family = FONT if FONT in tkfont.families(root) else FONT_FALLBACK
        self.fonts = {}
        self.fit_cache = {}

        # --- hardware ---
        self.arduino = ArduinoLink(SERIAL_PORT, BAUD_RATE)
        self.arduino.send("Z")       # start with fan, lamp, LEDs and buzzer all off, matching the screen
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY   # DSHOW opens faster on Windows
        self.cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        self.face_finder = FaceFinder()
        self.camera_ok = self.cap.isOpened() and self.face_finder.ok
        if not self.cap.isOpened():
            self.camera_msg = "Camera not found – use the keyboard (Space to select)"
        else:
            self.camera_msg = "Face model file missing – use the keyboard (Space to select)"
        self.detector = BlinkDetector()
        self.phone = PhoneNotifier()
        self.sounds = SoundPlayer()

        # --- state ---
        self.mode = "CALIBRATE"      # CALIBRATE | SCAN | CONFIRM | DONE | CANCELLED | SOS
        self.calibrated = False
        self.calib_samples = []
        self.calib_time = 0.0
        self.highlight = 0           # index of the highlighted tile
        self.highlight_changed_at = 0.0   # for the fade-in
        self.scan_elapsed = 0.0      # time spent on the current tile (pauses while eyes closed)
        self.pending_tile = None     # tile waiting for confirmation
        self.confirm_start = 0.0
        self.message_until = 0.0     # when the DONE / CANCELLED screen ends
        self.done_action = None      # which action the DONE screen is about
        self.done_text = ""
        self.light_on = False
        self.fan_on = False
        self.nurse_called = False
        self.nurse_coming = False    # set by the nurse (key A now, nurse phone later)
        self.sos_start = 0.0
        self.sleep_start = 0.0       # when sleep mode started
        self.open_since = None       # in sleep mode: when the eyes opened (for waking up)
        self.sos_alerts_sent = 0     # how many SOS alerts went to the phone
        self.next_sos_alert = 0.0
        self.paused = False          # Pause button / P key: blinks choose nothing (SOS still works)
        self.hold_step = 0           # how many hold ticks have played for the current eye close
        self.pause_btn = None        # (x0, y0, x1, y1) of the Pause button, for mouse clicks

        # --- screen options ---
        self.patient_mode = False    # D: hide EAR numbers and key help
        self.voice_on = False        # V: speak tile names
        self.beep_on = False         # B: soft tick when the highlight moves
        self.fan_angle = 0.0         # fan icon rotation

        # --- camera / face data ---
        self.frame = None
        self.eye_points = []
        self.face_visible = False
        self.ear = 0.0
        self.preview_photo = None    # keep a reference or Tkinter drops the image

        self.log = []                # in-memory event log: (time, text)
        self.last_action = "–"
        self.last_tick = time.monotonic()

        root.bind("<Key>", self.on_key)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Motion>", self.on_mouse_move)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log_event("App started")
        if not self.camera_ok:
            self.log_event(self.camera_msg.replace("–", "-"))
        # say the start-up instruction as soon as the voice is ready (if still calibrating)
        self.sounds.on_voice_ready = lambda: self.mode == "CALIBRATE" and self.play_cue(None, self.SAY_CALIBRATE)
        self.sounds.prepare_voice(self.all_sentences())   # record the voice once, in the background
        self.tick()

    # -----------------------------------------------------------------
    # MAIN LOOP
    # -----------------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        dt = now - self.last_tick
        self.last_tick = now

        self.read_camera()
        # confirming takes a little longer than selecting
        self.detector.hold_seconds = CONFIRM_HOLD if self.mode == "CONFIRM" else SELECT_HOLD
        if self.face_visible:
            events = self.detector.update(self.ear, now)
        else:
            self.detector.reset()
            events = []
        self.update_state(now, dt, events)
        self.hold_ticks(now)
        while not self.phone.inbox.empty():           # caretaker tapped "On my way"
            self.phone.inbox.get()
            self.perform_action("NURSE_ACK", "phone")
        self.repeat_sos_alert(now)
        self.check_arduino_link()
        if self.fan_on:   # spin the fan icon
            self.fan_angle = (self.fan_angle + dt * FAN_TURNS_PER_SECOND * 2 * math.pi) % (2 * math.pi)
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
        lm = self.face_finder.find(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self.frame = frame
        if lm is None:
            self.eye_points, self.face_visible = [], False
            return
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
        if self.paused:
            return                                    # paused: only rapid-blink SOS (above) works
        if "HOLD" in events:
            self.on_long_close(now)                   # does nothing on DONE / CANCELLED

        if self.mode == "SCAN":
            # eyes closed for a long time = asleep. Pause so nothing is chosen by accident.
            if self.face_visible and self.detector.closed and now - self.detector.close_start >= SLEEP_AFTER:
                self.perform_action("SLEEP", f"eyes closed {SLEEP_AFTER:g} s")
                return
            # move on only while the face is visible and the eyes are open
            # (with no camera at all, keep moving so the keyboard still works)
            can_see = self.face_visible or not self.camera_ok
            if can_see and not self.detector.closed:
                self.scan_elapsed += dt
                if self.scan_elapsed >= SCAN_INTERVAL:
                    self.scan_elapsed = 0.0
                    self.move_highlight((self.highlight + 1) % len(TILES))

        elif self.mode == "CONFIRM":
            hold_in_progress = self.detector.hold_progress(now) > 0
            # time out, but don't cut off a confirm-hold that has already started
            if now - self.confirm_start >= CONFIRM_WINDOW and not hold_in_progress:
                self.log_event(f"Cancelled: {self.pending_tile['name']} (not confirmed)")
                self.show_message("CANCELLED", CANCEL_SECONDS)
                self.play_cue(None, "Cancelled.")

        elif self.mode in ("DONE", "CANCELLED"):
            if now >= self.message_until:
                self.back_to_scan()

        elif self.mode == "SLEEP":
            # wake up once the eyes stay open for WAKE_AFTER seconds
            # (5 fast blinks still send SOS - that is checked above)
            if self.face_visible and not self.detector.closed:
                if self.open_since is None:
                    self.open_since = now
                elif now - self.open_since >= WAKE_AFTER:
                    self.perform_action("WAKE", "eyes open")
            else:
                self.open_since = None

    def on_long_close(self, now):
        """A 3-second eye close, or Space on the keyboard."""
        if self.mode == "SCAN":
            self.pending_tile = TILES[self.highlight]
            self.confirm_start = now
            self.mode = "CONFIRM"
            self.play_cue("CONFIRM", self.spoken_confirm(self.pending_tile))   # beep, then "Turn fan on. Are you sure?"
            self.log_event(f"Selected: {self.pending_tile['name']} - waiting for confirmation")
        elif self.mode == "CONFIRM":
            tile = self.pending_tile
            self.back_to_scan()
            self.perform_action(tile["id"], "confirmed")

    def hold_ticks(self, now):
        """While the eyes are held closed on a choice, tick 3 times going up, so the patient
        knows it is counting. The CONFIRM / DONE sound that follows means: open your eyes."""
        counting = self.face_visible and self.mode in ("SCAN", "CONFIRM") and not self.paused
        p = self.detector.hold_progress(now) if counting else 0.0
        step = sum(p >= f for f in HOLD_TICKS)
        if step > self.hold_step and SOUND_CUES:
            self.sounds.play(f"HOLD{step}", only_if_idle=True)
        self.hold_step = step

    def back_to_scan(self):
        self.mode = "SCAN"
        self.pending_tile = None
        self.scan_elapsed = 0.0

    def show_message(self, mode, seconds):
        """Show the DONE or CANCELLED screen for a short time, then scan again."""
        self.mode = mode
        self.pending_tile = None
        self.message_until = time.monotonic() + seconds

    def move_highlight(self, index):
        self.highlight = index
        self.highlight_changed_at = time.monotonic()
        if self.voice_on or self.beep_on:
            self.sounds.play("TICK" if self.beep_on else None, TILES[index]["say"] if self.voice_on else None)

    def play_cue(self, kind, say=None):
        """Beep ('CONFIRM' / 'DONE' / None), then the sentence straight after it, as one sound."""
        self.sounds.play(kind if SOUND_CUES else None, say if (SPEAK_CUES or self.voice_on) else None)

    def spoken_confirm(self, tile):
        choice = self.confirm_text(tile).lower().replace("sos", "S O S")
        # "Open your eyes. Turn fan on? Close your eyes again to say yes."
        return f"Open your eyes. {choice[0].upper() + choice[1:]}? Close your eyes again to say yes."

    SAY_CALIBRATE = "Look at the camera, and keep your eyes open."
    SAY_READY = f"Blinky is ready. Close your eyes for {SELECT_HOLD:g} seconds to choose."

    @staticmethod
    def spoken_done(done_text):
        return "Done. " + done_text[0] + done_text[1:].lower() + "."     # "Done. Fan is on."

    def all_sentences(self):
        """Every sentence Blinky can say, so they are all recorded at start-up."""
        out = []
        saved = (self.light_on, self.fan_on)
        for light in (False, True):
            for fan in (False, True):
                self.light_on, self.fan_on = light, fan
                out += [self.spoken_confirm(t) for t in TILES]
        self.light_on, self.fan_on = saved
        out += [self.spoken_done(t) for t in ("Light is ON", "Light is OFF", "Fan is ON", "Fan is OFF",
                                              "Nurse has been called")]
        out += ["Cancelled.", "The nurse is coming.", "Voice on", self.SAY_CALIBRATE, self.SAY_READY]
        out += [t["say"] for t in TILES]
        return out

    def start_calibration(self):
        self.play_cue(None, self.SAY_CALIBRATE)
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
        self.move_highlight(0)
        if not self.calibrated:
            self.calibrated = True
            self.perform_action("READY")

    # -----------------------------------------------------------------
    # ACTIONS - the ONE place where things actually happen.
    # Every action (blink, keyboard, later a nurse phone page) comes through here.
    # -----------------------------------------------------------------
    def perform_action(self, action, source=""):
        done_text = None             # set for actions that get the green DONE screen
        if action == "SOS":
            self.paused = False          # an emergency always un-pauses
            self.mode = "SOS"
            self.pending_tile = None
            self.sos_start = time.monotonic()
            self.arduino.send("S")
            text = "SOS ALERT"
        elif action == "NURSE":
            self.nurse_called = True
            self.nurse_coming = False
            self.arduino.send("N")
            text = "Nurse called"
            done_text = "Nurse has been called"
        elif action == "LIGHT":
            self.light_on = not self.light_on
            self.arduino.send("L" if self.light_on else "l")
            text = f"Light {'ON' if self.light_on else 'OFF'}"
            done_text = f"Light is {'ON' if self.light_on else 'OFF'}"
        elif action == "FAN":
            self.fan_on = not self.fan_on
            self.arduino.send("F" if self.fan_on else "f")
            text = f"Fan {'ON' if self.fan_on else 'OFF'}"
            done_text = f"Fan is {'ON' if self.fan_on else 'OFF'}"
        elif action == "NURSE_ACK":
            # the nurse says "I'm coming" (key A now, nurse phone later)
            self.nurse_coming = True
            text = "Nurse is coming"
            self.play_cue(None, "The nurse is coming.")
        elif action == "RESET":
            self.nurse_called = False
            self.nurse_coming = False
            self.detector.blink_times.clear()
            if self.mode == "SOS":
                if self.calibrated:
                    self.back_to_scan()
                else:
                    self.start_calibration()
            self.arduino.send("X")
            text = "SOS / nurse call cleared"
        elif action == "SLEEP":
            self.mode = "SLEEP"
            self.pending_tile = None
            self.sleep_start = time.monotonic()
            self.open_since = None
            text = "Patient is sleeping - scanning paused"
        elif action == "WAKE":
            self.back_to_scan()
            text = "Patient is awake - scanning again"
        elif action == "READY":
            self.play_cue(None, self.SAY_READY)
            self.arduino.send("G")
            text = "App ready"
        elif action == "PAUSE":
            self.paused = True
            self.detector.reset()
            if self.mode in ("CONFIRM", "DONE", "CANCELLED", "SLEEP"):
                self.back_to_scan()      # drop any half-finished choice
            text = "Paused"
        elif action == "RESUME":
            self.paused = False
            self.detector.reset()        # don't count eyes that were closed during the pause
            self.back_to_scan()
            self.move_highlight(0)
            text = "Resumed"
        # elif action == "MEDICINE":
        #     self.arduino.send("M")
        #     text = "Medicine requested"
        #     done_text = "Medicine has been requested"
        else:
            text = f"Unknown action: {action}"

        self.log_event(text + (f" ({source})" if source else ""))
        self.last_action = f"{text} at {datetime.now():%H:%M:%S}"
        self.notify_nurse(action, text)
        if done_text:
            self.done_action = action
            self.done_text = done_text
            self.show_message("DONE", DONE_SECONDS)
            self.play_cue("DONE", self.spoken_done(done_text))   # chime, then "Done. Fan is on."

    def notify_nurse(self, action, text):
        """Send every action to the caretaker's phone (ntfy). Called from perform_action()."""
        at = datetime.now().strftime("%H:%M")
        on_off = lambda on: "ON" if on else "OFF"
        # action: (title, message, priority 1-5, ntfy emoji tag, show "On my way" button)
        messages = {
            "SOS":   ("SOS! Patient needs help NOW",
                      f"The patient blinked for SOS at {at}. Please go to them right away. Tap 'On my way'.",
                      5, "rotating_light", True),
            "NURSE": ("Patient asked: CALL THE NURSE",
                      f"The patient is calling you ({at}). Tap 'On my way' so they know you're coming.",
                      4, "bell", True),
            "LIGHT": (f"Patient asked: TURN LIGHT {on_off(self.light_on)}",
                      f"Done - the light is now {on_off(self.light_on)} ({at}).", 3, "bulb", False),
            "FAN":   (f"Patient asked: TURN FAN {on_off(self.fan_on)}",
                      f"Done - the fan is now {on_off(self.fan_on)} ({at}).", 3, "cyclone", False),
            "RESET": ("Alert cleared", f"The SOS / nurse call was reset at the bedside ({at}).", 2, "white_check_mark", False),
            "READY": ("Blinky is running", f"Phone alerts are working ({at}).", 2, "eye", False),
            "SLEEP": ("Patient is sleeping", f"Eyes closed for {SLEEP_AFTER:g} s ({at}). Blinky paused scanning "
                      f"so nothing is chosen by accident. SOS still works.", 2, "zzz", False),
            "WAKE":  ("Patient is awake", f"Blinky is scanning again ({at}).", 2, "sunny", False),
            "PAUSE": ("Blinky paused", f"Blink control was paused at the bedside ({at}). SOS still works.",
                      2, "pause_button", False),
            "RESUME": ("Blinky resumed", f"Blink control is back on ({at}).", 2, "arrow_forward", False),
        }
        if action in messages:           # NURSE_ACK comes FROM the phone, so it isn't sent back
            self.phone.send(*messages[action])
        if action == "SOS":              # the first SOS alert just went; tick() sends the repeats
            self.sos_alerts_sent = 1
            self.next_sos_alert = time.monotonic() + SOS_ALERT_GAP

    def repeat_sos_alert(self, now):
        """During SOS, alert the phone again every few seconds, so it rings again and again.
        Stops after SOS_ALERT_REPEATS, or as soon as the nurse taps 'On my way' or R is pressed."""
        if self.mode != "SOS" or self.nurse_coming or self.sos_alerts_sent >= SOS_ALERT_REPEATS:
            return
        if now >= self.next_sos_alert:
            self.sos_alerts_sent += 1
            secs = int(now - self.sos_start)
            self.phone.send(f"SOS! ({self.sos_alerts_sent}/{SOS_ALERT_REPEATS}) Patient still needs help",
                            f"Nobody has answered yet - alert started {secs} s ago. Tap 'On my way'.",
                            5, "rotating_light", True)
            self.next_sos_alert = now + SOS_ALERT_GAP

    def check_arduino_link(self):
        """Log what the background connector found; when the board (re)connects,
        send it the current light/fan state so it matches the screen."""
        while self.arduino.messages:
            self.log_event(self.arduino.messages.popleft())
        if self.arduino.just_connected:
            self.arduino.just_connected = False
            self.arduino.send("Z")
            if self.light_on:
                self.arduino.send("L")
            if self.fan_on:
                self.arduino.send("F")
            if self.mode == "SOS":
                self.arduino.send("S")
            elif self.calibrated:
                self.arduino.send("G")

    def log_event(self, text):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log.append((stamp, text))
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
        elif key == "p":
            self.toggle_pause()
        elif key == "space":
            if self.mode == "CALIBRATE":
                self.finish_calibration(EAR_THRESHOLD or EAR_FALLBACK, "skipped with Space")
            elif self.mode in ("SCAN", "CONFIRM") and not self.paused:
                self.on_long_close(time.monotonic())
        elif key == "s" and self.mode != "SOS":
            self.perform_action("SOS", "keyboard")
        elif key == "r":
            self.perform_action("RESET", "keyboard")
        elif key == "c" and self.mode != "SOS":
            self.start_calibration()
        elif key == "z" and self.calibrated and not self.paused:           # demo: sleep / wake without waiting 20 s
            if self.mode == "SLEEP":
                self.perform_action("WAKE", "keyboard")
            elif self.mode in ("SCAN", "CONFIRM", "DONE", "CANCELLED"):
                self.perform_action("SLEEP", "keyboard")
        elif key == "a":
            self.perform_action("NURSE_ACK", "keyboard")
        elif key == "d":
            self.patient_mode = not self.patient_mode
        elif key == "v" and self.sounds.voice:
            self.voice_on = not self.voice_on
            if self.voice_on:
                self.sounds.play(None, "Voice on")
        elif key == "b" and winsound:
            self.beep_on = not self.beep_on
        elif key == "up":
            self.detector.threshold += 0.01
        elif key == "down":
            self.detector.threshold = max(0.05, self.detector.threshold - 0.01)
        elif key == "f11":
            self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    # -----------------------------------------------------------------
    # PAUSE BUTTON (bottom-left corner)
    # -----------------------------------------------------------------
    def toggle_pause(self):
        if not self.calibrated or self.mode == "SOS":
            return                       # nothing to pause yet / never pause an SOS
        self.perform_action("RESUME" if self.paused else "PAUSE", "button")

    def over_pause_btn(self, event):
        b = self.pause_btn
        return b is not None and b[0] <= event.x <= b[2] and b[1] <= event.y <= b[3]

    def on_click(self, event):
        if self.over_pause_btn(event):
            self.toggle_pause()

    def on_mouse_move(self, event):
        self.canvas.config(cursor="hand2" if self.over_pause_btn(event) else "")

    # -----------------------------------------------------------------
    # TEXT HELPERS
    # -----------------------------------------------------------------
    def font(self, px, bold=False):
        """Font sized in pixels, so text scales with the window. Cached by size."""
        px = max(8, int(px))
        key = (px, bold)
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(root=self.root, family=self.font_family, size=-px,
                                          weight="bold" if bold else "normal")
        return self.fonts[key]

    def fit_font(self, text, px, max_w, bold=True):
        """Biggest font (up to px) where the text still fits inside max_w pixels."""
        key = (text, int(px), int(max_w), bold)
        if key not in self.fit_cache:
            if len(self.fit_cache) > 2000:
                self.fit_cache.clear()
            lines = text.split("\n")
            while px > 8 and max(self.font(px, bold).measure(l) for l in lines) > max_w:
                px *= 0.92
            self.fit_cache[key] = self.font(px, bold)
        return self.fit_cache[key]

    def tile_state(self, tile):
        """Small line under the tile name."""
        if tile["id"] == "LIGHT":
            return "ON" if self.light_on else "OFF"
        if tile["id"] == "FAN":
            return "ON" if self.fan_on else "OFF"
        if tile["id"] == "NURSE":
            return "COMING" if self.nurse_coming else ("CALLED" if self.nurse_called else "")
        return ""

    def icon_is_on(self, tile_id, after_toggle=False):
        """Should the light show rays / the fan spin? after_toggle = show the new state."""
        state = {"LIGHT": self.light_on, "FAN": self.fan_on}.get(tile_id, False)
        return (not state) if after_toggle and tile_id in ("LIGHT", "FAN") else state

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

    # -----------------------------------------------------------------
    # ICONS - all drawn with canvas shapes. s = icon size in pixels.
    # fg = icon colour, bg = colour behind it (used for cut-outs).
    # -----------------------------------------------------------------
    def draw_icon(self, kind, cx, cy, s, fg, bg, on=False):
        c = self.canvas
        u = s / 2                      # half size, all shapes use this unit
        lw = max(2, u * 0.11)          # line width
        cos, sin = math.cos, math.sin

        if kind == "SOS":              # warning triangle with "!"
            c.create_polygon(cx, cy - 0.9 * u, cx + 0.95 * u, cy + 0.75 * u, cx - 0.95 * u, cy + 0.75 * u,
                             fill=fg, outline=fg, width=max(2, u * 0.22), joinstyle="round")
            c.create_line(cx, cy - 0.32 * u, cx, cy + 0.2 * u, fill=bg, width=max(2, u * 0.2), capstyle="round")
            r = u * 0.11
            c.create_oval(cx - r, cy + 0.44 * u - r, cx + r, cy + 0.44 * u + r, fill=bg, outline="")

        elif kind == "NURSE":          # bell: dome + lip + small ball underneath
            body = [cx - 0.62 * u, cy + 0.42 * u, cx - 0.6 * u, cy - 0.05 * u, cx - 0.5 * u, cy - 0.6 * u,
                    cx, cy - 0.8 * u, cx + 0.5 * u, cy - 0.6 * u, cx + 0.6 * u, cy - 0.05 * u,
                    cx + 0.62 * u, cy + 0.42 * u]
            c.create_polygon(body, smooth=True, fill=fg, outline=fg)
            round_rect(c, cx - 0.88 * u, cy + 0.3 * u, cx + 0.88 * u, cy + 0.52 * u, 0.11 * u, fill=fg, outline="")
            r = 0.13 * u
            c.create_oval(cx - r, cy - 0.86 * u - r, cx + r, cy - 0.86 * u + r, fill=fg, outline="")
            r = 0.18 * u
            c.create_oval(cx - r, cy + 0.72 * u - r, cx + r, cy + 0.72 * u + r, fill=fg, outline="")

        elif kind == "LIGHT":          # bulb: glass circle + base. Rays when ON.
            gy, gr = cy - 0.22 * u, 0.58 * u
            if on:
                for deg in (0, 30, 60, 90, 120, 150, 180):
                    a = math.radians(deg)
                    c.create_line(cx + cos(a) * 0.8 * u, gy - sin(a) * 0.8 * u,
                                  cx + cos(a) * 1.05 * u, gy - sin(a) * 1.05 * u,
                                  fill=fg, width=lw, capstyle="round")
            c.create_oval(cx - gr, gy - gr, cx + gr, gy + gr, fill=fg if on else bg, outline=fg, width=lw)
            round_rect(c, cx - 0.3 * u, cy + 0.42 * u, cx + 0.3 * u, cy + 0.82 * u, 0.08 * u, fill=fg, outline="")
            c.create_line(cx - 0.3 * u, cy + 0.62 * u, cx + 0.3 * u, cy + 0.62 * u, fill=bg, width=max(1, u * 0.06))
            r = 0.12 * u
            c.create_oval(cx - r, cy + 0.86 * u - r, cx + r, cy + 0.86 * u + r, fill=fg, outline="")

        elif kind == "FAN":            # 3 blades around a centre dot, inside a guard ring
            c.create_oval(cx - u, cy - u, cx + u, cy + u, outline=fg, width=max(2, u * 0.08))
            for k in range(3):
                a = self.fan_angle + k * 2 * math.pi / 3
                pts = []
                for r, da in ((0.18, -0.45), (0.55, -0.6), (0.86, -0.3), (0.86, 0.15), (0.55, 0.3), (0.18, 0.35)):
                    pts += [cx + cos(a + da) * r * u, cy + sin(a + da) * r * u]
                c.create_polygon(pts, smooth=True, fill=fg, outline=fg)
            r = 0.2 * u
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fg, outline="")
            r = 0.08 * u
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=bg, outline="")

        elif kind == "MEDICINE":       # pill: capsule, left half filled
            h2 = 0.4 * u
            round_rect(c, cx - u, cy - h2, cx + u, cy + h2, h2, fill=bg, outline=fg, width=lw)
            round_rect(c, cx - u, cy - h2, cx, cy + h2, h2, fill=fg, outline=fg, width=lw)
            c.create_rectangle(cx - h2, cy - h2, cx, cy + h2, fill=fg, outline=fg)

        elif kind == "EYE":            # open eye: almond outline + iris + pupil
            pts = [cx - u, cy, cx - u, cy, cx - 0.4 * u, cy - 0.62 * u, cx + 0.4 * u, cy - 0.62 * u,
                   cx + u, cy, cx + u, cy, cx + 0.4 * u, cy + 0.62 * u, cx - 0.4 * u, cy + 0.62 * u]
            c.create_polygon(pts, smooth=True, fill=bg, outline=fg, width=lw)
            r = 0.3 * u
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fg, outline="")
            r = 0.12 * u
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=bg, outline="")

        elif kind == "TICK":           # check mark: 2 thick lines
            c.create_line(cx - 0.62 * u, cy + 0.02 * u, cx - 0.18 * u, cy + 0.46 * u, cx + 0.66 * u, cy - 0.48 * u,
                          fill=fg, width=max(3, u * 0.24), capstyle="round", joinstyle="round")

    def draw_ring(self, cx, cy, r, width, progress, colour, track):
        """Ring that fills clockwise from the top (progress 0..1)."""
        c = self.canvas
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=track, width=width)
        if progress > 0:
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-min(359.9, 360 * progress),
                         style="arc", outline=colour, width=width)

    # -----------------------------------------------------------------
    # DRAWING - the whole screen is redrawn on one canvas every frame
    # Theme: warm off-white background, white rounded cards with soft shadows,
    # black pill buttons, purple accent, playful shapes on the welcome screen.
    # -----------------------------------------------------------------
    def card(self, x0, y0, x1, y1, r, fill=PANEL, outline="", width=1, shadow=SHADOW, lift=None):
        """Rounded card with a soft shadow under it (the theme's main shape)."""
        if shadow:
            d = lift if lift is not None else max(3, (y1 - y0) * 0.018)
            round_rect(self.canvas, x0, y0 + d, x1, y1 + d, r, fill=shadow, outline="")
        return round_rect(self.canvas, x0, y0, x1, y1, r, fill=fill, outline=outline, width=width)

    def badge(self, kind, cx, cy, s, fg, circle, on=False):
        """Icon sitting in a round coloured circle."""
        r = s * 0.62
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=circle, outline="")
        self.draw_icon(kind, cx, cy, s * 0.8, fg, circle, on)

    # --- playful shapes (welcome screen) ---
    def draw_flower(self, cx, cy, r, colour):
        c = self.canvas
        for k in range(5):
            a = -math.pi / 2 + k * 2 * math.pi / 5
            px, py, pr = cx + math.cos(a) * r * 0.55, cy + math.sin(a) * r * 0.55, r * 0.48
            c.create_oval(px - pr, py - pr, px + pr, py + pr, fill=colour, outline="")
        cr = r * 0.45
        c.create_oval(cx - cr, cy - cr, cx + cr, cy + cr, fill=colour, outline="")

    def draw_moon(self, cx, cy, r, colour, bg):
        c = self.canvas
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=colour, outline="")
        ox, oy, rr = cx - r * 0.38, cy - r * 0.42, r * 0.8       # a "bite" in the background colour
        c.create_oval(ox - rr, oy - rr, ox + rr, oy + rr, fill=bg, outline="")

    def draw_blob(self, cx, cy, r, colour):
        pts = []
        for k, f in enumerate((1.0, 0.86, 1.06, 0.92, 1.0, 0.88, 1.07, 0.9)):
            a = k * math.pi / 4
            pts += [cx + math.cos(a) * r * f, cy + math.sin(a) * r * f]
        self.canvas.create_polygon(pts, smooth=True, fill=colour, outline="")

    def draw_star(self, cx, cy, r, colour, points=8):
        pts = []
        for k in range(points * 2):
            a = -math.pi / 2 + k * math.pi / points
            rr = r if k % 2 == 0 else r * 0.55
            pts += [cx + math.cos(a) * rr, cy + math.sin(a) * rr]
        self.canvas.create_polygon(pts, fill=colour, outline=colour, width=max(1, r * 0.08), joinstyle="round")

    def draw_squiggle(self, x0, cy, w, amp, colour, width, bumps=3):
        pts = []
        for k in range(bumps * 8 + 1):
            x = x0 + w * k / (bumps * 8)
            pts += [x, cy - amp * abs(math.sin(math.pi * bumps * k / (bumps * 8)))]
        self.canvas.create_line(pts, smooth=True, fill=colour, width=width, capstyle="round", joinstyle="round")

    # --- the screen ---
    def draw(self, now):
        c = self.canvas
        c.delete("all")
        self.pause_btn = None            # set again by draw_status() when the button is drawn
        W = max(c.winfo_width(), 800)
        H = max(c.winfo_height(), 500)

        if self.mode == "SOS":
            self.draw_sos(W, H, now)
            return

        pad = int(H * 0.022)
        header_h = int(H * 0.11)
        status_h = int(H * 0.08)
        side_w = int(W * 0.26)

        self.draw_header(W, header_h, pad)
        area = (pad * 1.5, header_h, W - side_w - pad, H - status_h - pad * 1.3)

        if self.mode == "CALIBRATE":
            self.draw_calibration(area)
        else:
            # banners at the top of the tile area (each one takes a strip of space)
            if not self.camera_ok:
                area = self.draw_banner(area, pad, self.camera_msg, PANEL, TEXT, "EYE", RED)
            if self.nurse_coming:
                area = self.draw_banner(area, pad, "Nurse is coming", GREEN, "#FFFFFF", "TICK", "#FFFFFF")
            elif self.nurse_called:
                area = self.draw_banner(area, pad, "Nurse has been called – help is on the way",
                                        PURPLE, "#FFFFFF", "NURSE", "#FFFFFF")

            if self.paused:
                self.draw_paused(area)
            elif self.mode == "CONFIRM":
                self.draw_confirm(area, now)
            elif self.mode == "DONE":
                self.draw_done(area)
            elif self.mode == "CANCELLED":
                self.draw_cancelled(area)
            elif self.mode == "SLEEP":
                self.draw_sleep(area, now)
            else:
                self.draw_tiles(area, now, pad)

            if self.camera_ok and not self.face_visible and self.mode in ("SCAN", "CONFIRM") and not self.paused:
                self.draw_face_lost(area)

        self.draw_sidebar(W - side_w, header_h, side_w - pad, H - status_h - pad, H)
        self.draw_status(W, H, status_h, pad)

    def draw_header(self, W, hh, pad):
        c = self.canvas
        # logo: white eye in a black circle
        r = hh * 0.3
        lx, ly = pad * 1.5 + r, hh * 0.5
        c.create_oval(lx - r, ly - r, lx + r, ly + r, fill=INK, outline="")
        self.draw_icon("EYE", lx, ly, r * 1.2, "#FFFFFF", INK)
        tx = lx + r + pad * 0.8
        c.create_text(tx, hh * 0.38, text="Blinky", anchor="w", fill=TEXT, font=self.font(hh * 0.42, True))
        c.create_text(tx, hh * 0.77, text="Every blink is a decision.", anchor="w", fill=MUTED,
                      font=self.font(hh * 0.19))
        # live clock in a white pill - patients lose track of time
        clock = datetime.now()
        f = self.font(hh * 0.28, True)
        t = clock.strftime("%I:%M %p").lstrip("0")
        w, h = f.measure(t) + hh * 0.55, hh * 0.48
        x1, y0 = W - pad * 1.5, hh * 0.12
        self.card(x1 - w, y0, x1, y0 + h, h / 2, lift=2)
        c.create_text(x1 - w / 2, y0 + h / 2, text=t, fill=TEXT, font=f)
        c.create_text(x1, hh * 0.84, text=clock.strftime("%A, %d %B"), anchor="e", fill=MUTED,
                      font=self.font(hh * 0.17))

    def draw_banner(self, area, pad, text, fill, text_colour, icon, icon_colour):
        """A rounded strip across the top of the tile area. Returns the area left below it."""
        c = self.canvas
        x0, y0, x1, y1 = area
        bh = (y1 - y0) * 0.12
        self.card(x0, y0, x1, y0 + bh, bh * 0.4, fill=fill)
        self.draw_icon(icon, x0 + bh * 0.62, y0 + bh / 2, bh * 0.5, icon_colour, fill)
        c.create_text(x0 + bh * 1.15, y0 + bh / 2, anchor="w", text=text, fill=text_colour,
                      font=self.fit_font(text, bh * 0.4, (x1 - x0) - bh * 1.5, True))
        return (x0, y0 + bh + pad, x1, y1)

    def draw_tiles(self, area, now, gap):
        x0, y0, x1, y1 = area
        n = len(TILES)
        cols = math.ceil(math.sqrt(n))          # 4 tiles -> 2x2, 5 tiles -> 3 + 2
        rows = math.ceil(n / cols)
        tw = (x1 - x0 - gap * (cols - 1)) / cols
        th = (y1 - y0 - gap * (rows - 1)) / rows

        boxes = []
        for i in range(n):
            r, col = divmod(i, cols)
            in_row = min(cols, n - r * cols)
            shift = (cols - in_row) * (tw + gap) / 2       # centre a shorter last row
            boxes.append((x0 + shift + col * (tw + gap), y0 + r * (th + gap)))
        # draw the highlighted tile last, so its bigger size sits on top
        order = [i for i in range(n) if i != self.highlight] + [self.highlight]
        for i in order:
            bx, by = boxes[i]
            self.draw_tile(TILES[i], bx, by, tw, th, i == self.highlight, now)

    def draw_tile(self, tile, x, y, w, h, active, now):
        c = self.canvas
        accent = tile["color"]
        if active:
            t = min(1.0, (now - self.highlight_changed_at) / FADE_SECONDS)   # fade in
            gx, gy = w * 0.02, h * 0.02                                       # +4% bigger
            x0, y0, x1, y1 = x - gx, y - gy, x + w + gx, y + h + gy
            fill = mix(PANEL, accent, t)
            fg = mix(TEXT, tile["text_on_fill"], t)
            icon_fg = mix(accent, tile["text_on_fill"], t)
            circle = mix(mix(accent, PANEL, 0.85), mix(fill, "#FFFFFF", 0.25), t)
            sub_colour = fg
            shadow, lift = mix(accent, BG, 0.45), max(6, h * 0.05)
        else:
            x0, y0, x1, y1 = x, y, x + w, y + h
            fill, fg, icon_fg = PANEL, TEXT, accent
            circle = mix(accent, PANEL, 0.85)                                 # light tint of the colour
            sub_colour = MUTED
            shadow, lift = SHADOW, None
        tw, th = x1 - x0, y1 - y0
        cx = (x0 + x1) / 2
        self.card(x0, y0, x1, y1, min(tw, th) * 0.12, fill=fill, shadow=shadow, lift=lift)

        # icon in a round badge, with the hold ring around it while the eyes are closed
        icon_s = min(th * 0.28, tw * 0.3)
        icon_cy = y0 + th * 0.28
        self.badge(tile["id"], cx, icon_cy, icon_s, icon_fg, circle, self.icon_is_on(tile["id"]))
        progress = self.detector.hold_progress(now) if (active and self.face_visible) else 0.0
        if progress > 0:
            self.draw_ring(cx, icon_cy, icon_s * 0.74, max(4, th * 0.035), progress, fg, mix(fill, fg, 0.25))

        # big word, then the state line (or the "keep closed" hint)
        c.create_text(cx, y0 + th * 0.62, text=tile["name"], fill=fg,
                      font=self.fit_font(tile["name"], th * 0.21, tw * 0.86, True))
        sub = "Keep your eyes closed…" if progress > 0 else self.tile_state(tile)
        if sub:
            c.create_text(cx, y0 + th * 0.8, text=sub, fill=sub_colour,
                          font=self.fit_font(sub, th * 0.09, tw * 0.86, True))

        # scan timer: thin bar along the bottom that empties over SCAN_INTERVAL
        if active and self.mode == "SCAN":
            left = 1.0 - min(1.0, self.scan_elapsed / SCAN_INTERVAL)
            bh = max(4, th * 0.035)
            m = min(tw, th) * 0.12                       # keep clear of the rounded corners
            bx0, bx1 = x0 + m, x1 - m
            by1 = y1 - th * 0.06
            round_rect(c, bx0, by1 - bh, bx1, by1, bh / 2, fill=mix(fill, fg, 0.22), outline="")
            if left > 0.01:
                round_rect(c, bx0, by1 - bh, bx0 + (bx1 - bx0) * left, by1, bh / 2, fill=fg, outline="")

    def draw_confirm(self, area, now):
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        cx = (x0 + x1) / 2
        tile = self.pending_tile
        colour = tile["color"]
        remaining = max(0.0, CONFIRM_WINDOW - (now - self.confirm_start))
        self.card(x0, y0, x1, y1, min(w, h) * 0.06, outline=colour, width=max(4, int(h * 0.01)))

        # the chosen icon in a tinted badge, with the hold ring
        icon_s = h * 0.14
        icon_cy = y0 + h * 0.16
        self.badge(tile["id"], cx, icon_cy, icon_s, colour, mix(colour, PANEL, 0.85), self.icon_is_on(tile["id"], True))
        closed_again = self.detector.closed and self.detector.hold_fired   # still closed from the first hold
        progress = 0.0 if (closed_again or not self.face_visible) else self.detector.hold_progress(now)
        if progress > 0:
            self.draw_ring(cx, icon_cy, icon_s * 0.76, max(4, h * 0.02), progress, colour, mix(PANEL, colour, 0.25))

        c.create_text(cx, y0 + h * 0.33, text="Are you sure?", fill=TEXT,
                      font=self.fit_font("Are you sure?", h * 0.075, w * 0.9, True))
        # the action, in a chip of the tile's colour
        action = self.confirm_text(tile)
        f = self.fit_font(action, h * 0.07, w * 0.7, True)
        pw, ph = f.measure(action) + h * 0.12, h * 0.11
        round_rect(c, cx - pw / 2, y0 + h * 0.45 - ph / 2, cx + pw / 2, y0 + h * 0.45 + ph / 2, ph / 2,
                   fill=colour, outline="")
        c.create_text(cx, y0 + h * 0.45, text=action, fill=tile["text_on_fill"], font=f)

        # black pill (like a "Book now" button) that fills up while the eyes are closed
        if closed_again:
            msg = "Open your eyes first, then close them to confirm"
        elif progress > 0:
            msg = "Keep your eyes closed…"
        else:
            msg = f"Close your eyes for {CONFIRM_HOLD:g} seconds to say YES"
        bw, bh = w * 0.8, h * 0.11
        bx0, by0 = cx - bw / 2, y0 + h * 0.6
        round_rect(c, bx0, by0, bx0 + bw, by0 + bh, bh / 2, fill=INK, outline="")
        if progress > 0:
            round_rect(c, bx0, by0, bx0 + max(bh, bw * progress), by0 + bh, bh / 2, fill=PURPLE, outline="")
        c.create_text(cx, by0 + bh / 2, text=msg, fill="#FFFFFF", font=self.fit_font(msg, bh * 0.38, bw * 0.88, True))

        # cancel row: countdown circle that empties + text, centred together
        r = h * 0.075
        row_y = y0 + h * 0.85
        txt = "Keep your eyes open to cancel"
        f = self.fit_font(txt, h * 0.045, w * 0.9 - r * 2.6, False)
        gx = cx - (r * 2.6 + f.measure(txt)) / 2
        self.draw_ring(gx + r, row_y, r, max(4, h * 0.012), remaining / CONFIRM_WINDOW, TEXT, mix(PANEL, MUTED, 0.3))
        c.create_text(gx + r, row_y, text=str(math.ceil(remaining)), fill=TEXT, font=self.font(r, True))
        c.create_text(gx + r * 2.6, row_y, anchor="w", text=txt, fill=MUTED, font=f)

    def draw_done(self, area):
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        cx = (x0 + x1) / 2
        self.card(x0, y0, x1, y1, min(w, h) * 0.06)
        s = h * 0.22
        iy = y0 + h * 0.32
        # green circle with a white tick, next to the action's icon
        r = s * 0.62
        c.create_oval(cx - s * 0.8 - r, iy - r, cx - s * 0.8 + r, iy + r, fill=GREEN, outline="")
        self.draw_icon("TICK", cx - s * 0.8, iy, s * 0.75, "#FFFFFF", GREEN)
        tile = TILE_BY_ID.get(self.done_action)
        if tile:
            self.badge(tile["id"], cx + s * 0.8, iy, s, tile["color"], mix(tile["color"], PANEL, 0.85),
                       self.icon_is_on(tile["id"]))
        c.create_text(cx, y0 + h * 0.62, text=self.done_text, fill=TEXT,
                      font=self.fit_font(self.done_text, h * 0.11, w * 0.9, True))
        c.create_text(cx, y0 + h * 0.8, text="Going back to the menu…", fill=MUTED, font=self.font(h * 0.04))

    def draw_sleep(self, area, now):
        """Calm 'Sleeping' screen: moon, soft text, and a pill that fills as the eyes open."""
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        cx = (x0 + x1) / 2
        self.card(x0, y0, x1, y1, min(w, h) * 0.06)
        # moon with a few stars, and "z z z" that drifts gently (no flashing)
        my = y0 + h * 0.22
        self.draw_moon(cx, my, h * 0.11, PURPLE, PANEL)
        self.draw_star(cx - h * 0.2, my - h * 0.06, h * 0.025, YELLOW, 5)
        self.draw_star(cx + h * 0.17, my + h * 0.07, h * 0.018, YELLOW, 5)
        for k, size in enumerate((0.035, 0.045, 0.055)):
            bob = math.sin(now * 1.2 + k) * h * 0.01
            c.create_text(cx + h * (0.15 + k * 0.05), my - h * (0.07 + k * 0.05) + bob, text="z",
                          fill=mix(PURPLE, PANEL, 0.3 - k * 0.1), font=self.font(h * size, True))

        c.create_text(cx, y0 + h * 0.46, text="Sleeping", fill=TEXT, font=self.font(h * 0.11, True))
        t2 = "Scanning is paused, so nothing is chosen by accident."
        c.create_text(cx, y0 + h * 0.58, text=t2, fill=MUTED, font=self.fit_font(t2, h * 0.045, w * 0.9, False))

        # black pill that fills purple while the eyes stay open
        progress = 0.0 if self.open_since is None else min(1.0, (now - self.open_since) / WAKE_AFTER)
        msg = "Waking up…" if progress > 0 else "Open your eyes to wake Blinky"
        bw, bh = w * 0.62, h * 0.1
        bx0, by0 = cx - bw / 2, y0 + h * 0.67
        round_rect(c, bx0, by0, bx0 + bw, by0 + bh, bh / 2, fill=INK, outline="")
        if progress > 0:
            round_rect(c, bx0, by0, bx0 + max(bh, bw * progress), by0 + bh, bh / 2, fill=PURPLE, outline="")
        c.create_text(cx, by0 + bh / 2, text=msg, fill="#FFFFFF", font=self.fit_font(msg, bh * 0.38, bw * 0.9, True))

        mins = int(now - self.sleep_start) // 60
        t3 = f"Asleep for {mins} min  ·  Blink fast {RAPID_BLINK_COUNT} times for SOS – it still works"
        c.create_text(cx, y0 + h * 0.88, text=t3, fill=MUTED, font=self.fit_font(t3, h * 0.035, w * 0.9, False))

    def draw_paused(self, area):
        """Calm 'Paused' card: big pause symbol, what it means, how to resume."""
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        cx = (x0 + x1) / 2
        self.card(x0, y0, x1, y1, min(w, h) * 0.06)
        r = h * 0.13
        iy = y0 + h * 0.24
        c.create_oval(cx - r, iy - r, cx + r, iy + r, fill=INK, outline="")
        bw, bh = r * 0.22, r * 0.9                       # two white bars
        for dx in (-r * 0.25, r * 0.25):
            round_rect(c, cx + dx - bw / 2, iy - bh / 2, cx + dx + bw / 2, iy + bh / 2, bw / 2,
                       fill="#FFFFFF", outline="")
        c.create_text(cx, y0 + h * 0.5, text="Paused", fill=TEXT, font=self.font(h * 0.11, True))
        t2 = "Blinks won't choose anything right now."
        c.create_text(cx, y0 + h * 0.62, text=t2, fill=MUTED, font=self.fit_font(t2, h * 0.05, w * 0.9, False))
        t3 = "Click Resume (bottom left) or press P to start again."
        c.create_text(cx, y0 + h * 0.72, text=t3, fill=TEXT, font=self.fit_font(t3, h * 0.045, w * 0.9, True))
        t4 = f"Blink fast {RAPID_BLINK_COUNT} times for SOS – it still works"
        c.create_text(cx, y0 + h * 0.88, text=t4, fill=MUTED, font=self.fit_font(t4, h * 0.035, w * 0.9, False))

    def draw_cancelled(self, area):
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        self.card(x0, y0, x1, y1, min(w, h) * 0.06)
        txt = "Cancelled. Nothing changed."
        c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=txt, fill=MUTED,
                      font=self.fit_font(txt, h * 0.075, w * 0.85, False))

    def draw_calibration(self, area):
        """Welcome screen: friendly big text and playful shapes, like an app's start screen."""
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        cx = (x0 + x1) / 2
        # playful shapes in the corners (drawn first, text goes on top)
        self.draw_flower(x0 + w * 0.11, y0 + h * 0.15, h * 0.13, YELLOW)
        self.draw_moon(x1 - w * 0.1, y0 + h * 0.15, h * 0.12, PURPLE, BG)
        self.draw_blob(x0 + w * 0.09, y1 - h * 0.12, h * 0.12, BLUE)
        self.draw_star(x1 - w * 0.33, y1 - h * 0.1, h * 0.07, RED)
        for k in range(2):
            self.draw_squiggle(x1 - w * 0.22, y1 - h * (0.12 - k * 0.09), w * 0.2, h * 0.045, GREEN, max(4, h * 0.03))

        t1 = "Hello! Let's get started."
        c.create_text(cx, y0 + h * 0.37, text=t1, fill=TEXT, font=self.fit_font(t1, h * 0.1, w * 0.9, True))
        t2 = "Look at the camera and keep your eyes open."
        c.create_text(cx, y0 + h * 0.48, text=t2, fill=TEXT, font=self.fit_font(t2, h * 0.045, w * 0.9, False))

        # black pill that fills up while we measure the open eyes
        if not self.camera_ok:
            msg = self.camera_msg
        elif not self.face_visible:
            msg = "Waiting to see your face…"
        else:
            msg = "Great, hold still…"
        bw, bh = w * 0.6, h * 0.1
        bx0, by0 = cx - bw / 2, y0 + h * 0.58
        progress = min(1.0, self.calib_time / CALIBRATION_SECONDS)
        round_rect(c, bx0, by0, bx0 + bw, by0 + bh, bh / 2, fill=INK, outline="")
        if progress > 0.02:
            round_rect(c, bx0, by0, bx0 + max(bh, bw * progress), by0 + bh, bh / 2, fill=PURPLE, outline="")
        c.create_text(cx, by0 + bh / 2, text=msg, fill="#FFFFFF", font=self.fit_font(msg, bh * 0.38, bw * 0.9, True))
        c.create_text(cx, y0 + h * 0.74, text="Space = skip", fill=MUTED, font=self.font(h * 0.035))

    def draw_face_lost(self, area):
        """Calm 'I can't see your eyes' card over faded tiles."""
        c = self.canvas
        x0, y0, x1, y1 = area
        w, h = x1 - x0, y1 - y0
        c.create_rectangle(x0 - 12, y0 - 12, x1 + 12, y1 + 16, fill=BG, stipple="gray50", outline="")
        cw, ch = w * 0.72, h * 0.48
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        kx0, ky0 = cx - cw / 2, cy - ch / 2
        self.card(kx0, ky0, kx0 + cw, ky0 + ch, ch * 0.14)
        r = ch * 0.14
        ey = ky0 + ch * 0.26
        c.create_oval(cx - r, ey - r, cx + r, ey + r, fill=INK, outline="")
        self.draw_icon("EYE", cx, ey, r * 1.3, "#FFFFFF", INK)
        t1 = "I can't see your eyes"
        c.create_text(cx, ky0 + ch * 0.57, text=t1, fill=TEXT, font=self.fit_font(t1, ch * 0.14, cw * 0.9, True))
        c.create_text(cx, ky0 + ch * 0.79, text="Scanning is paused. Move into view of the camera.", fill=MUTED,
                      font=self.font(ch * 0.065), width=cw * 0.85, justify="center")

    def draw_sos(self, W, H, now):
        c = self.canvas
        # smooth pulse, SOS_PULSE_HZ times a second (2 or less = photosensitive safe)
        t = 0.5 + 0.5 * math.sin(2 * math.pi * SOS_PULSE_HZ * now)
        bg = mix("#8E1A0E", RED, t)
        c.create_rectangle(0, 0, W, H, fill=bg, outline="")
        self.draw_icon("SOS", W / 2, H * 0.14, H * 0.13, "#FFFFFF", bg)
        c.create_text(W / 2, H * 0.36, text="SOS", fill="white", font=self.font(H * 0.24, True))
        t1 = "HELP IS BEING CALLED"
        c.create_text(W / 2, H * 0.56, text=t1, fill="white", font=self.fit_font(t1, H * 0.075, W * 0.9, True))
        t2 = "A nurse is on the way." if self.nurse_coming else "Stay calm. Someone is coming."
        c.create_text(W / 2, H * 0.68, text=t2, fill="white", font=self.fit_font(t2, H * 0.05, W * 0.9, False))
        secs = int(now - self.sos_start)
        c.create_text(W / 2, H * 0.78, text=f"Alert sent {secs // 60}:{secs % 60:02d} ago", fill="#FFE1DC",
                      font=self.font(H * 0.038))
        # black pill at the bottom, like the theme's buttons
        f = self.font(H * 0.03, True)
        txt = "Nurse: press R to reset"
        pw, ph = f.measure(txt) + H * 0.08, H * 0.065
        round_rect(c, W / 2 - pw / 2, H * 0.91 - ph / 2, W / 2 + pw / 2, H * 0.91 + ph / 2, ph / 2, fill=INK, outline="")
        c.create_text(W / 2, H * 0.91, text=txt, fill="#FFFFFF", font=f)

    def draw_sidebar(self, x, y, w, bottom, H):
        c = self.canvas
        # camera preview inside a white card (eye outlines: green = open, red = closed)
        m = max(6, w * 0.035)
        pw = int(w - 2 * m)
        ph = int(pw * 3 / 4)
        self.card(x, y, x + w, y + ph + 2 * m, w * 0.07)
        if self.frame is not None:
            img = self.frame.copy()
            colour = (63, 85, 242) if self.detector.closed else (58, 184, 25)   # BGR: red / green
            for eye in self.eye_points:
                pts = np.array(eye, dtype=np.int32)
                cv2.polylines(img, [pts], True, colour, 2)
                for px, py in pts:
                    cv2.circle(img, (int(px), int(py)), 3, colour, -1)
            img = cv2.cvtColor(cv2.resize(img, (pw, ph)), cv2.COLOR_BGR2RGB)
            self.preview_photo = ImageTk.PhotoImage(Image.fromarray(img))
            c.create_image(x + m, y + m, image=self.preview_photo, anchor="nw")
        else:
            round_rect(c, x + m, y + m, x + m + pw, y + m + ph, w * 0.05, fill=mix(BG, PANEL, 0.4), outline="")
            c.create_text(x + w / 2, y + m + ph / 2, text="No camera", fill=MUTED, font=self.font(H * 0.03))

        # eyes status in a white pill with a coloured dot
        if not self.camera_ok:
            eyes, col = "No camera", MUTED
        elif not self.face_visible:
            eyes, col = "Eyes: not seen", MUTED
        elif self.detector.closed:
            eyes, col = "Eyes: CLOSED", RED
        else:
            eyes, col = "Eyes: OPEN", GREEN
        py0 = y + ph + 2 * m + H * 0.022
        pill_h = H * 0.065
        ty = py0 + pill_h / 2
        self.card(x, py0, x + w, py0 + pill_h, pill_h / 2)
        r = H * 0.014
        c.create_oval(x + pill_h * 0.4, ty - r, x + pill_h * 0.4 + 2 * r, ty + r, fill=col, outline="")
        tx = x + pill_h * 0.4 + 3 * r
        c.create_text(tx, ty, anchor="w", text=eyes, fill=TEXT,
                      font=self.fit_font(eyes, H * 0.036, x + w - tx - pill_h * 0.3, True))

        # "How to use" card (text first, then the card is drawn behind it)
        by = py0 + pill_h + H * 0.022
        inner = w * 0.07
        title = c.create_text(x + inner, by + inner, anchor="nw", text="How to use", fill=TEXT,
                              font=self.font(H * 0.03, True))
        body = c.create_text(x + inner, by + inner + H * 0.045, anchor="nw", width=w - 2 * inner, fill=TEXT,
                             font=self.font(H * 0.027),
                             text=f"•  Close your eyes for {SELECT_HOLD:g} seconds to choose.\n"
                                  f"•  Blink fast {RAPID_BLINK_COUNT} times for SOS.")
        bb = c.bbox(body)
        d = 3
        shadow = round_rect(c, x, by + d, x + w, bb[3] + inner + d, w * 0.07, fill=SHADOW, outline="")
        box = round_rect(c, x, by, x + w, bb[3] + inner, w * 0.07, fill=PANEL, outline="")
        c.tag_lower(box)
        c.tag_lower(shadow)

        if self.patient_mode:
            return
        # tech details for us and the judges (D hides them)
        ny = bb[3] + inner + H * 0.028
        ear_txt = f"EAR {self.ear:.3f}  ·  closed below {self.detector.threshold:.3f}" if self.face_visible \
            else f"EAR –  ·  closed below {self.detector.threshold:.3f}"
        c.create_text(x, ny, anchor="w", text=ear_txt, fill=MUTED, font=self.fit_font(ear_txt, H * 0.022, w, False))
        phone_txt = f"Phone alerts: {self.phone.status}  ·  Voice: {self.sounds.voice_state}"
        ny += H * 0.027
        c.create_text(x, ny, anchor="w", text=phone_txt, fill=RED if "NOT" in self.phone.status else MUTED,
                      font=self.fit_font(phone_txt, H * 0.022, w, False))
        voice = ("on" if self.voice_on else "off") if self.sounds.voice else "n/a"
        beep = ("on" if self.beep_on else "off") if winsound else "n/a"
        keys = ("Space select · S SOS · R reset · C calibrate\n"
                "↑/↓ threshold · A nurse coming · Z sleep · P pause · D patient\n"
                f"V voice {voice} · B tick {beep} · F11 full screen · Esc quit")
        f = self.fit_font(keys, H * 0.021, w, False)
        if ny + H * 0.015 + f.metrics("linespace") * 3 <= bottom:     # only if there is room
            c.create_text(x, ny + H * 0.015, anchor="nw", text=keys, fill=MUTED, font=f)

    def draw_status(self, W, H, status_h, pad):
        """White bar at the bottom, like an app's tab bar."""
        c = self.canvas
        y0 = H - status_h
        cy = y0 + status_h / 2
        c.create_rectangle(0, y0 - 2, W, y0, fill=SHADOW, outline="")
        c.create_rectangle(0, y0, W, H, fill=PANEL, outline="")
        fpx = status_h * 0.33
        f = self.font(fpx)
        fb = self.font(fpx, True)
        icon_s = status_h * 0.44

        if self.nurse_coming:
            nurse = "Nurse: coming"
        elif self.nurse_called:
            nurse = "Nurse: called"
        else:
            nurse = "Nurse: –"
        items = [("LIGHT", f"Light {'ON' if self.light_on else 'OFF'}", self.light_on),
                 ("FAN", f"Fan {'ON' if self.fan_on else 'OFF'}", self.fan_on),
                 ("NURSE", nurse, self.nurse_called or self.nurse_coming)]

        # Pause / Resume button in the bottom-left corner (black = Pause, purple = Resume)
        label = "Resume" if self.paused else "Pause"
        bh = status_h * 0.62
        sym = bh * 0.36                                   # size of the pause bars / play triangle
        bw = bh * 0.45 + sym + bh * 0.3 + fb.measure(label) + bh * 0.5
        bx0, by0 = pad * 1.5, cy - bh / 2
        bx1, by1 = bx0 + bw, cy + bh / 2
        usable = self.calibrated
        fill = (PURPLE if self.paused else INK) if usable else mix(MUTED, PANEL, 0.4)
        round_rect(c, bx0, by0, bx1, by1, bh / 2, fill=fill, outline="")
        sx = bx0 + bh * 0.45                              # left edge of the symbol
        if self.paused:                                   # play triangle
            c.create_polygon(sx, cy - sym / 2, sx, cy + sym / 2, sx + sym * 0.9, cy, fill="#FFFFFF", outline="")
        else:                                             # two pause bars
            b = sym * 0.32
            c.create_rectangle(sx, cy - sym / 2, sx + b, cy + sym / 2, fill="#FFFFFF", outline="")
            c.create_rectangle(sx + sym - b, cy - sym / 2, sx + sym, cy + sym / 2, fill="#FFFFFF", outline="")
        c.create_text(sx + sym + bh * 0.3, cy, anchor="w", text=label, fill="#FFFFFF", font=fb)
        self.pause_btn = (bx0, by0, bx1, by1) if usable else None

        x = bx1 + pad * 1.5
        for kind, txt, on in items:
            icon_col = TILE_BY_ID[kind]["color"] if on else MUTED
            self.draw_icon(kind, x + icon_s / 2, cy, icon_s, icon_col, PANEL, on and kind != "NURSE")
            x += icon_s + pad * 0.5
            item = c.create_text(x, cy, anchor="w", text=txt, fill=TEXT if on else MUTED, font=fb if on else f)
            x = c.bbox(item)[2] + pad * 1.5

        # Arduino status on the right, in a soft pill
        if self.arduino.connected:
            txt, col = f"Arduino connected ({self.arduino.port})", GREEN
        else:
            txt = ("Searching for Arduino…" if self.arduino.searching
                   else "Demo mode – Arduino not connected (retrying)")
            col = RED
        r = status_h * 0.1
        tw = f.measure(txt)
        ph = status_h * 0.56
        px1 = W - pad * 1.5
        px0 = px1 - tw - ph * 0.6 - 3 * r - ph * 0.4
        round_rect(c, px0, cy - ph / 2, px1, cy + ph / 2, ph / 2, fill=mix(col, PANEL, 0.88), outline="")
        c.create_oval(px0 + ph * 0.4, cy - r, px0 + ph * 0.4 + 2 * r, cy + r, fill=col, outline="")
        c.create_text(px1 - ph * 0.3, cy, anchor="e", text=txt, fill=TEXT, font=f)

        # last action in the space between
        last = f"Last action: {self.last_action}"
        avail = px0 - pad * 1.5 - x
        if avail > 60:
            c.create_text(x, cy, anchor="w", text=last, fill=MUTED, font=self.fit_font(last, fpx, avail, False))

    # -----------------------------------------------------------------
    def quit(self):
        self.log_event("App closed")
        self.root.after_cancel(self.after_id)
        self.cap.release()
        self.face_finder.close()
        self.arduino.close()
        self.sounds.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    BlinkyApp(root)
    root.mainloop()
