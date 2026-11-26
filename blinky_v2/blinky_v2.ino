/*
  Blinky - Arduino UNO Q sketch
  Listens for single letters from the laptop (sent through the HC-05 Bluetooth module)
  and switches the room light (3 LEDs), the fan and the buzzer, and shows the status
  on the 16x2 I2C screen (address 0x27).

  Letters (the laptop app sends these; you can also type them in the Serial Monitor):
    L / l   light on / off    (GREEN LED = room light)
    F / f   fan on / off      (BLUE LED = fan; steady, or BLINKING if the light is also on)
    S       SOS               (all 3 LEDs blink fast + continuous beep)
    X       clear SOS / nurse call
    N       call nurse        (RED LED on + 3 short beeps + screen message)
    G       app ready         (screen message)
    Z       everything off    (sent when the app starts)
*/

#include <Wire.h>

#define BT Serial1          // HC-05 is on D0/D1 (this worked in the Bluetooth test)

// ---- PINS ----
const int FAN_PIN  = 2;               // L298N IN1 -> fan motor
const int LAMP_PIN = 4;               // L298N IN3 -> extra lamp (optional, switches with the light)
// LED colours - if a colour is wrong, just swap these three numbers.
// (At start-up the LEDs flash in the order RED, GREEN, BLUE so you can check.)
const int RED_PIN   = 5;              // red   = nurse call
const int GREEN_PIN = 6;              // green = room light
const int BLUE_PIN  = 7;              // blue  = fan
const int LED_PINS[] = {RED_PIN, GREEN_PIN, BLUE_PIN};
const int BUZZER   = 8;               // active buzzer, + leg

const unsigned long FAN_BLINK_MS = 400;   // blue LED blink speed when fan + light are both on
const unsigned long SOS_BLINK_MS = 250;   // 250 ms on + 250 ms off = 2 flashes a second (epilepsy-safe)

bool lightOn = false;
bool fanOn = false;
bool sosActive = false;
bool nurseActive = false;
unsigned long nurseStart = 0;   // when the nurse call started, for the beeps

// =====================================================================
// SCREEN: 16x2 LCD with an I2C backpack (PCF8574 chip).
// Written by hand so no library needs installing.
// The backpack's 8 outputs: bit0 RS, bit1 RW, bit2 EN, bit3 backlight, bits4-7 data.
// =====================================================================
const uint8_t LCD_ADDR = 0x27;
const uint8_t LCD_RS = 0x01, LCD_EN = 0x04, LCD_LIGHT = 0x08;

void lcdPulse(uint8_t data) {
  // send 4 data bits: set them, then pulse the EN (enable) pin high then low
  Wire.beginTransmission(LCD_ADDR);
  Wire.write((uint8_t)(data | LCD_EN | LCD_LIGHT));
  Wire.endTransmission();
  delayMicroseconds(2);
  Wire.beginTransmission(LCD_ADDR);
  Wire.write((uint8_t)((data & ~LCD_EN) | LCD_LIGHT));
  Wire.endTransmission();
  delayMicroseconds(60);
}

void lcdSend(uint8_t value, uint8_t mode) {
  // a full byte goes as two halves: high 4 bits first, then low 4 bits
  lcdPulse((value & 0xF0) | mode);
  lcdPulse(((value << 4) & 0xF0) | mode);
}

void lcdCommand(uint8_t cmd) {
  lcdSend(cmd, 0);
  if (cmd == 0x01) delay(2);       // "clear" needs extra time
}

void lcdInit() {
  Wire.begin();
  delay(50);
  // wake-up sequence from the LCD datasheet, ending in 4-bit mode
  lcdPulse(0x30); delay(5);
  lcdPulse(0x30); delay(1);
  lcdPulse(0x30); delay(1);
  lcdPulse(0x20); delay(1);
  lcdCommand(0x28);   // 2 lines, normal font
  lcdCommand(0x0C);   // display on, no cursor
  lcdCommand(0x06);   // text moves left to right
  lcdCommand(0x01);   // clear
}

void lcdLine(uint8_t row, const char *text) {
  // write one whole line (16 characters), padding with spaces to wipe old text
  lcdCommand(0x80 | (row == 0 ? 0x00 : 0x40));
  for (int i = 0; i < 16; i++) {
    char ch = (*text) ? *text++ : ' ';
    lcdSend((uint8_t)ch, LCD_RS);
  }
}

// ---- EMOJI: the LCD can hold 8 home-made 5x8 pictures (slots 0-7). ----
// Each picture is 8 rows of 5 dots (1 = dot on). We use slots 1-7, because
// character 0 would end a text string. In text, write them as "\x01" ... "\x07"
// (always put a space after, e.g. "\x02 Light ON").
const uint8_t EMO_SPARKLE = 1, EMO_BULB = 2, EMO_FAN = 3, EMO_BELL = 4,
              EMO_HEART = 5, EMO_SMILE = 6, EMO_ALERT = 7;

const uint8_t PIC_SPARKLE[8] = {0b00100, 0b00100, 0b01110, 0b11111, 0b01110, 0b00100, 0b00100, 0b00000};
const uint8_t PIC_BULB[8]  = {0b01110, 0b10001, 0b10001, 0b10001, 0b01010, 0b01110, 0b01110, 0b00100};
const uint8_t PIC_FAN[8]   = {0b00000, 0b11001, 0b01011, 0b00100, 0b11010, 0b10011, 0b00000, 0b00000};
const uint8_t PIC_BELL[8]  = {0b00100, 0b01110, 0b01110, 0b01110, 0b11111, 0b00000, 0b00100, 0b00000};
const uint8_t PIC_HEART[8] = {0b00000, 0b01010, 0b11111, 0b11111, 0b01110, 0b00100, 0b00000, 0b00000};
const uint8_t PIC_SMILE[8] = {0b00000, 0b01010, 0b01010, 0b00000, 0b10001, 0b01110, 0b00000, 0b00000};
const uint8_t PIC_ALERT[8] = {0b00100, 0b01110, 0b01010, 0b11011, 0b11111, 0b11011, 0b11111, 0b00000};

void lcdCreateChar(uint8_t slot, const uint8_t rows[8]) {
  lcdCommand(0x40 | (slot << 3));      // point at the custom-picture memory
  for (int i = 0; i < 8; i++) lcdSend(rows[i], LCD_RS);
  lcdCommand(0x80);                    // back to normal text
}

void lcdLoadEmoji() {
  lcdCreateChar(EMO_SPARKLE, PIC_SPARKLE);
  lcdCreateChar(EMO_BULB, PIC_BULB);
  lcdCreateChar(EMO_FAN, PIC_FAN);
  lcdCreateChar(EMO_BELL, PIC_BELL);
  lcdCreateChar(EMO_HEART, PIC_HEART);
  lcdCreateChar(EMO_SMILE, PIC_SMILE);
  lcdCreateChar(EMO_ALERT, PIC_ALERT);
}

void showStatus(const char *text) {
  lcdLine(1, text);
}

// Top line = live summary, e.g. [bulb] ON    [fan] OFF
void showSummary() {
  char line[17];
  snprintf(line, sizeof(line), "\x02 %-4s  \x03 %-4s", lightOn ? "ON" : "OFF", fanOn ? "ON" : "OFF");
  lcdLine(0, line);
}

// =====================================================================

void setLeds(bool on) {
  for (int p : LED_PINS) digitalWrite(p, on ? HIGH : LOW);
}

void allOff() {
  lightOn = false;
  fanOn = false;
  sosActive = false;
  nurseActive = false;
  setLeds(false);
  digitalWrite(LAMP_PIN, LOW);
  digitalWrite(FAN_PIN, LOW);
  digitalWrite(BUZZER, LOW);
}

void setup() {
  pinMode(FAN_PIN, OUTPUT);
  pinMode(LAMP_PIN, OUTPUT);
  pinMode(BUZZER, OUTPUT);
  for (int p : LED_PINS) pinMode(p, OUTPUT);
  allOff();

  lcdInit();
  lcdLoadEmoji();
  lcdLine(0, "   \x01 Blinky \x05");      // sparkle Blinky heart
  showStatus("Starting...");

  // Start-up self-test: flash each LED once, in order. If you see this, the code is running.
  for (int p : LED_PINS) {
    digitalWrite(p, HIGH);
    delay(400);
    digitalWrite(p, LOW);
  }

  BT.begin(9600);           // HC-05 default speed
  showStatus("Waiting for app");
}

void loop() {
  // 1. Handle every letter that has arrived
  while (BT.available() > 0) {
    char c = BT.read();
    switch (c) {
      case 'L':
        lightOn = true;
        digitalWrite(LAMP_PIN, HIGH);
        showSummary();
        showStatus("Green: Light ON");
        break;
      case 'l':
        lightOn = false;
        digitalWrite(LAMP_PIN, LOW);
        showSummary();
        showStatus("Green: Light OFF");
        break;
      case 'F': fanOn = true;  digitalWrite(FAN_PIN, HIGH); showSummary(); showStatus("Blue: Fan ON");  break;
      case 'f': fanOn = false; digitalWrite(FAN_PIN, LOW);  showSummary(); showStatus("Blue: Fan OFF"); break;
      case 'S':
        sosActive = true;
        showStatus("\x07 SOS! HELP! \x07");
        break;
      case 'N':
        nurseActive = true;
        nurseStart = millis();
        showStatus("Red: Nurse call");
        break;
      case 'X':
        sosActive = false;
        nurseActive = false;
        showStatus("\x06 All clear");
        break;
      case 'G': showSummary(); showStatus("\x06 Ready"); break;
      case 'Z': allOff(); showSummary(); showStatus("All off"); break;
      default: break;       // ignore anything else (e.g. Enter key characters)
    }
  }

  // 2. LEDs and buzzer, using millis() so new letters are never missed
  if (sosActive) {
    // SOS: all 3 LEDs blink fast together, buzzer on all the time
    setLeds((millis() / SOS_BLINK_MS) % 2 == 0);
    digitalWrite(BUZZER, (millis() / SOS_BLINK_MS) % 2 == 0 ? HIGH : LOW);
  } else {
    // normal: GREEN = light, BLUE = fan, RED = nurse call
    digitalWrite(GREEN_PIN, lightOn ? HIGH : LOW);
    bool blueOn = false;
    if (fanOn) {
      // fan on: steady blue; if the light is on too, blue BLINKS so you can tell them apart
      blueOn = lightOn ? ((millis() / FAN_BLINK_MS) % 2 == 0) : true;
    }
    digitalWrite(BLUE_PIN, blueOn ? HIGH : LOW);
    digitalWrite(RED_PIN, nurseActive ? HIGH : LOW);
    if (nurseActive) {
      // Nurse: 3 short beeps (250 ms on, 250 ms off), then quiet
      unsigned long t = millis() - nurseStart;
      digitalWrite(BUZZER, (t < 1500 && (t / 250) % 2 == 0) ? HIGH : LOW);
    } else {
      digitalWrite(BUZZER, LOW);
    }
  }
}
