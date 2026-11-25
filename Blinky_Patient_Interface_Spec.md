# Blinky: Patient Interface Spec

> **How to use this file:** paste this whole file into a new chat **together with the current `blinky.py`**. Ask: *"Rebuild the screen of blinky.py to match this spec. Keep the blink detection and the Arduino link exactly as they are. Give me the full file."*

---

## 1. What Blinky is

Blinky lets a **paralysed patient who can only move their eyes** control their room with blinks.
A webcam watches the eyes. The screen moves a highlight through 4 big buttons. The patient closes their eyes for 3 seconds to pick one, then does it again to confirm. Fast blinking sends an SOS.

- **Tagline:** *Every blink is a decision.*
- **Event:** Technovate hackathon, theme "The Last Human Decision", live on stage on 26 Nov 2026.
- **Where the screen is shown:** the laptop screen is **mirrored to a projector**. It has to be readable from the back of a hall **and** comfortable for a patient lying in bed 1–2 m away.

---

## 2. What already works (DO NOT CHANGE)

### 2.1 Hardware, done and tested
Laptop → **HC-05 Bluetooth (COM port)** → **Arduino UNO Q** → 3 LEDs (room light), fan (L298N), buzzer, 16x2 LCD.

### 2.2 Arduino protocol, single letters at 9600 baud. Use these exactly:

| Letter | Meaning |
|---|---|
| `L` / `l` | Light ON / OFF |
| `F` / `f` | Fan ON / OFF |
| `S` | SOS (LEDs flash + buzzer) |
| `N` | Call nurse (3 beeps + LCD message) |
| `X` | Clear SOS / nurse call |
| `G` | App ready |
| `Z` | Everything off (send at start-up) |

### 2.3 Logic to keep from the current `blinky.py`
Keep these parts **as they are**. Only the drawing/screen code gets rebuilt:
- `ArduinoLink` class (including demo mode when the COM port isn't found)
- `BlinkDetector` class (EAR, BLINK / HOLD / RAPID events)
- `eye_aspect_ratio()` and the eye landmark lists
- The state machine: `CALIBRATE → SCAN → CONFIRM`, plus `SOS`
- `perform_action()` as the **one place** where actions happen
- The log file `blinky_log.txt`
- All the settings at the top (`SERIAL_PORT`, `SCAN_INTERVAL = 3.0`, `SELECT_HOLD = 3.0`, `CONFIRM_WINDOW = 6.0`, `RAPID_BLINK_COUNT = 5`, etc.)
- Keyboard backup: `Space` select/confirm, `S` SOS, `R` reset, `C` recalibrate, `Up/Down` threshold, `F11` fullscreen, `Esc` quit

### 2.4 Tech rules
- **Python 3.10–3.12**, **one file** (`blinky.py`), **Tkinter** for the window.
- Libraries: `opencv-python`, `mediapipe`, `pyserial`, `Pillow`, `numpy`. Nothing else is required.
- **Must work fully offline** (no internet at the venue). No web fonts, no downloaded images. Draw all icons with Tkinter canvas shapes.
- Windows laptop. Font: `Segoe UI` (fallback `Arial`).
- Must scale to any window size (1280x720 window **and** fullscreen on a projector). Size everything as a fraction of window width/height, like the current code does.

---

## 3. Design goals (who this is for)

The person using this **cannot move, cannot speak, may be tired, scared, or have weak eyesight.** So:

1. **Huge and simple.** 4 buttons only. Each one has a big icon **and** a big word. Never colour alone (colour-blind safe).
2. **Calm, not scary.** Dark, soft background. Only SOS uses strong red. No fast flashing except during SOS.
3. **Always tell the patient what is happening.** "Keep your eyes closed…", "Light is ON", "Nurse is coming". The patient must never wonder if it worked.
4. **Show time visually.** The patient should *see* how long until the highlight moves, and how far through the 3-second hold they are.
5. **Readable from far away** for judges. Main words at least ~8% of screen height.
6. **Safe flashing.** SOS flashing must stay at **2 flashes per second or slower** (photosensitive epilepsy safety, worth saying to the judges).

---

## 4. Look and feel

### 4.1 Colours

| Use | Colour |
|---|---|
| Background | `#0E1117` (deep navy-black) |
| Panels / idle tiles | `#1A1F2B` |
| Main text | `#F4F6FA` |
| Soft text | `#9AA4B5` |
| Good / connected / done | `#3DDC84` (green) |
| Problem | `#FF5A5A` |

Tile accent colours:

| Tile | Accent | Text on filled tile |
|---|---|---|
| SOS | `#FF3B3B` red | white |
| CALL NURSE | `#FFB020` amber | black |
| LIGHT | `#FFE58A` warm yellow | black |
| FAN | `#3CC8E0` cyan | black |

### 4.2 Shapes
- Rounded corners everywhere (draw rounded rectangles on the canvas with a helper `round_rect()`).
- Highlighted tile: **filled** with its accent colour, white border, slightly bigger (+4%).
- Not highlighted: dark panel with a thin coloured border, icon and word in the accent colour, slightly dimmed.

### 4.3 Icons (drawn with canvas shapes, no image files)
- **SOS:** a bold triangle with "!" inside, or a circle with "SOS".
- **CALL NURSE:** a bell (dome + small ball underneath).
- **LIGHT:** a bulb (circle + small rectangle base). When light is ON, draw short rays around it.
- **FAN:** 3 or 4 blades around a centre dot. When fan is ON, **slowly rotate** the blades (about 1 turn per 2 s). This looks great on stage.

---

## 5. Screen layout (main SCAN screen)

```
┌──────────────────────────────────────────────────────────────┐
│  👁 Blinky        Every blink is a decision.     10:42 AM │  ← top bar
├──────────────────────────────────────────┬───────────────────┤
│                                          │  [camera preview] │
│   ┌──────────────┐   ┌──────────────┐    │   eyes outlined   │
│   │     SOS      │   │  CALL NURSE  │    │   green = open    │
│   │   (icon)     │   │   (icon)     │    │   red = closed    │
│   └──────────────┘   └──────────────┘    │                   │
│   ┌──────────────┐   ┌──────────────┐    │  Eyes: OPEN ●     │
│   │    LIGHT     │   │     FAN      │    │                   │
│   │   (icon) OFF │   │   (icon) ON  │    │  How to use:      │
│   └──────────────┘   └──────────────┘    │  • Close eyes 3 s │
│                                          │    to choose      │
│                                          │  • Blink fast 5x  │
│                                          │    for SOS        │
├──────────────────────────────────────────┴───────────────────┤
│ 💡 Light OFF   🌀 Fan ON   🔔 Nurse: –    ● Arduino connected │  ← status bar
└──────────────────────────────────────────────────────────────┘
```
(The emoji above are just for this sketch. In the app, draw small icons with shapes.)

### 5.1 Top bar
- "Blinky" big and bold on the left, tagline in soft text under or beside it.
- Live clock on the right (patients lose track of time; this is a nice human touch).

### 5.2 The 4 tiles (2 x 2 grid)
- Order: **SOS, CALL NURSE, LIGHT, FAN** (same as now).
- Each tile: icon on top, word in the middle, state underneath for LIGHT/FAN ("ON" / "OFF"), and "CALLED" for nurse once pressed.
- **Scan timer on the highlighted tile:** a thin bar along the bottom edge of the tile that empties over `SCAN_INTERVAL` (3 s). When it runs out, the highlight moves to the next tile. Pause it while eyes are closed or face is lost (the current logic already pauses `scan_elapsed`).
- **Hold progress:** while the patient holds their eyes closed on a tile, draw a **thick ring that fills clockwise** around the icon (0→100% over 3 s) and show the text **"Keep your eyes closed…"** on the tile. Use `detector.hold_progress(now)`.
- Smooth highlight movement: when the highlight moves, fade the new tile's fill in over ~200 ms (simple colour interpolation per frame). Optional but nice.

### 5.3 Side panel (right, about 26% width)
- Camera preview (mirror image) with eye outlines: **green = open, red = closed** (as now).
- Big "Eyes: OPEN" / "Eyes: CLOSED" with a coloured dot.
- A short **"How to use"** box: *Close your eyes for 3 seconds to choose. Blink fast 5 times for SOS.*
- Small soft-text line: EAR value and threshold (for us, not the patient).
- Key `D` toggles a **"patient mode"** that hides the EAR numbers and keyboard help, leaving only camera + how-to. Default = shown (judges like seeing the tech).

### 5.4 Status bar (bottom)
- Small icons + text: Light ON/OFF, Fan ON/OFF, Nurse status.
- Arduino status on the right: green dot "Arduino connected (COM5)" or red dot "Demo mode – Arduino not connected".
- "Last action: Light ON at 10:41:12" in soft text.

---

## 6. The other screens

### 6.1 CALIBRATE (start-up)
- Big friendly text: **"Hello! Let's get started."**
- Under it: **"Look at the camera and keep your eyes open."**
- Large green progress bar filling over 3 s (pauses if no face).
- An open-eye icon drawn with shapes.
- Soft hint: "Space = skip".

### 6.2 CONFIRM ("Are you sure?")
Fills the tile area. Border in the chosen tile's colour.
- The chosen tile's **icon, big**, at the top.
- **"Are you sure?"**
- The action in the tile colour: e.g. **"TURN LIGHT ON"**, **"CALL THE NURSE"**, **"SEND SOS"** (use the existing `confirm_text()`).
- **"Close your eyes for 3 seconds to say YES"**
- **"Keep your eyes open to cancel"** with a big countdown number (6 → 0) inside a circle that empties.
- Hold progress bar/ring that fills while eyes are closed.
- If eyes are still closed from the first hold, show: "Open your eyes first, then close them to confirm" (already in the current code).

### 6.3 DONE (new, about 2 seconds after any action)
After LIGHT, FAN or NURSE is done, show a short, calm confirmation over the tile area before going back to scanning:
- Big green tick (drawn with 2 lines) + the icon.
- **"Light is ON"** / **"Fan is OFF"** / **"Nurse has been called"**
- Then return to SCAN automatically. (Add a `DONE` mode with a 2 s timer. Blinks are ignored during it, except RAPID → SOS.)

### 6.4 Nurse called banner
While `nurse_called` is true, show an amber banner across the top of the tile area:
**"🔔 Nurse has been called – help is on the way"** (bell drawn with shapes). Cleared by `R` / reset.

### 6.5 Cancelled (small)
If confirm times out, show "Cancelled. Nothing changed." for 1.5 s in soft text, then back to scanning.

### 6.6 SOS
Full screen:
- Background slowly pulses between `#C40000` and `#4A0000` at **max 2 times per second**.
- Huge **"SOS"**, under it **"HELP IS BEING CALLED"**.
- Under that, for the patient: **"Stay calm. Someone is coming."** (reassuring, not scary).
- Time since SOS started: "Alert sent 0:12 ago".
- Bottom soft text: "Nurse: press R to reset".

### 6.7 Face not visible
Don't cover the whole screen in red. Instead:
- Dim the tiles (draw a semi-dark overlay; use a stipple pattern `stipple="gray50"` on a black rectangle).
- Centre card with an eye icon and **"I can't see your eyes"** and soft text **"Scanning is paused. Move into view of the camera."**
- If the camera itself is missing: "Camera not found – use the keyboard (Space to select)".

---

## 7. Optional extras (only if time allows, in this order)

1. **Spoken labels (offline):** key `V` toggles a voice that says the tile name when it gets highlighted ("Light", "Fan"…) and "Done" after an action. Use `pyttsx3` (works offline on Windows) in a **background thread** so it never freezes the screen. Off by default. If `pyttsx3` isn't installed, just disable the feature silently.
2. **Soft tick sound** when the highlight moves (`winsound.Beep(800, 40)` in a thread). Off by default, key `B` toggles.
3. **Medicine tile** (5th tile, purple `#B388FF`, pill icon). The grid must handle 5 tiles (3 + 2). Needs a new Arduino letter later, so leave it commented out like now.

---

## 8. Hooks for the nurse phone (later, not now)

Don't build the nurse phone page in this step. But make it easy to add:
- Keep `perform_action()` as the single place actions happen.
- Add an empty function `notify_nurse(action, text)` that `perform_action()` calls for every action. Right now it does nothing.
- Add `perform_action("NURSE_ACK")` that sets a flag `nurse_coming = True` and changes the nurse banner to **"✅ Nurse is coming"** (green). For now, key `A` triggers it so we can demo it.

---

## 9. Things to be careful about

- **The window must never freeze.** Everything runs in the existing `tick()` loop with `root.after()`. No `time.sleep()` in the main thread.
- Redrawing the whole canvas every frame is fine (that's what the current code does), but keep it light: don't recreate fonts every frame (cache them by size).
- Keep the `PhotoImage` reference for the camera preview, or Tkinter drops the image.
- On start: send `Z`, then after calibration send `G` (already the case).
- Every visible text must fit at both 1280x720 and 1920x1080. Test by resizing the window.
- Comments in the code should be simple and short. We are students and need to explain the code in the technical interview.

---

## 10. Done when…

- [ ] Starts on CALIBRATE, then shows the 2x2 tiles with icons.
- [ ] Highlight moves every 3 s with a visible draining bar.
- [ ] Holding eyes closed fills a ring, then shows the "Are you sure?" screen.
- [ ] Confirming shows the DONE screen and the real LEDs / fan react.
- [ ] Light bulb shows rays when ON; fan icon spins when ON.
- [ ] 5 fast blinks go straight to SOS (Arduino flashes and beeps). `R` clears it.
- [ ] Nurse banner appears after CALL NURSE; `A` turns it into "Nurse is coming".
- [ ] Face leaving the camera pauses everything with the calm "I can't see your eyes" card.
- [ ] Works without the Arduino (demo mode) and without the camera (keyboard only).
- [ ] Looks good fullscreen on a projector (F11).
