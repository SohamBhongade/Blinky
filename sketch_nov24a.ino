// Screen finder - result shows on the LEDs
#include <Wire.h>

const int LED5 = 5, LED6 = 6, LED7 = 7;

bool found(byte addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void setup() {
  pinMode(LED5, OUTPUT);
  pinMode(LED6, OUTPUT);
  pinMode(LED7, OUTPUT);
  Wire.begin();
  delay(500);

  if (found(0x27)) { digitalWrite(LED5, HIGH); return; }
  if (found(0x3F)) { digitalWrite(LED6, HIGH); return; }
  for (byte a = 1; a < 127; a++) {
    if (found(a)) { digitalWrite(LED7, HIGH); return; }
  }
}

void loop() {
  // Nothing found -> all LEDs blink
  if (!digitalRead(LED5) && !digitalRead(LED6) && !digitalRead(LED7)) {
    digitalWrite(LED5, HIGH); digitalWrite(LED6, HIGH); digitalWrite(LED7, HIGH);
    delay(300);
    digitalWrite(LED5, LOW);  digitalWrite(LED6, LOW);  digitalWrite(LED7, LOW);
    delay(300);
  }
}