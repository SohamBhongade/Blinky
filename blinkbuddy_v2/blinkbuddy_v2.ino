/*
  BlinkBuddy - Arduino UNO Q sketch
  Listens for single letters from the laptop (sent through the HC-05 Bluetooth module)
  and switches the room light (3 LEDs), the fan and the buzzer, and shows the status
  on the 16x2 I2C screen (address 0x27).

  Letters (the laptop app sends these; you can also type them in the Serial Monitor):
    L / l   light on / off    (all 3 LEDs together)
    F / f   fan on / off
    S       SOS               (all 3 LEDs blink fast + continuous beep)
    X       clear SOS / nurse call (LEDs go back to the light's on/off state)
    N       call nurse        (3 short beeps + screen message)
    G       app ready         (screen message)
    Z       everything off    (sent when the app starts)
*/

#include <Wire.h>

#define BT Serial1          // HC-05 is on D0/D1 (this worked in the Bluetooth test)

// ---- PINS ----
const int FAN_PIN  = 2;               // L298N IN1 -> fan motor
const int LAMP_PIN = 4;               // L298N IN3 -> extra lamp (optional, switches with the light)
const int LED_PINS[] = {5, 6, 7};     // the 3 LEDs = the room light
const int BUZZER   = 8;               // active buzzer, + leg

const unsigned long SOS_BLINK_MS = 100;   // SOS blink speed: lower = faster

bool lightOn = false;
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

void showStatus(const char *text) {
  lcdLine(1, text);
}

// =====================================================================

void setLeds(bool on) {
  for (int p : LED_PINS) digitalWrite(p, on ? HIGH : LOW);
}

void allOff() {
  lightOn = false;
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
  lcdLine(0, "  Blink Buddy");
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
        showStatus("Light ON");
        break;
      case 'l':
        lightOn = false;
        digitalWrite(LAMP_PIN, LOW);
        showStatus("Light OFF");
        break;
      case 'F': digitalWrite(FAN_PIN, HIGH); showStatus("Fan ON");  break;
      case 'f': digitalWrite(FAN_PIN, LOW);  showStatus("Fan OFF"); break;
      case 'S':
        sosActive = true;
        showStatus("!!!  SOS  !!!");
        break;
      case 'N':
        nurseActive = true;
        nurseStart = millis();
        showStatus("NURSE CALLED");
        break;
      case 'X':
        sosActive = false;
        nurseActive = false;
        showStatus("All clear");
        break;
      case 'G': showStatus("Ready"); break;
      case 'Z': allOff(); showStatus("All off"); break;
      default: break;       // ignore anything else (e.g. Enter key characters)
    }
  }

  // 2. LEDs and buzzer, using millis() so new letters are never missed
  if (sosActive) {
    // SOS: all 3 LEDs blink fast together, buzzer on all the time
    setLeds((millis() / SOS_BLINK_MS) % 2 == 0);
    digitalWrite(BUZZER, (millis() / SOS_BLINK_MS) % 2 == 0 ? HIGH : LOW);
  } else {
    setLeds(lightOn);          // normal: LEDs follow the light switch
    if (nurseActive) {
      // Nurse: 3 short beeps (250 ms on, 250 ms off), then quiet
      unsigned long t = millis() - nurseStart;
      digitalWrite(BUZZER, (t < 1500 && (t / 250) % 2 == 0) ? HIGH : LOW);
    } else {
      digitalWrite(BUZZER, LOW);
    }
  }
}
