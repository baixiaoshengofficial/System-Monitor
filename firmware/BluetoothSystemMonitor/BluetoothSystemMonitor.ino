#include "BluetoothSerial.h"
#include <TFT_eSPI.h>

static const uint32_t SERIAL_BAUD = 115200;
static const uint32_t STALE_AFTER_MS = 5000;
static const int MAX_SLOTS = 10;
static const int TITLE_BITMAP_W = 230;
static const int TITLE_BITMAP_H = 24;
static const int TITLE_BITMAP_BYTES = (TITLE_BITMAP_W * TITLE_BITMAP_H + 7) / 8;

TFT_eSPI tft = TFT_eSPI();
BluetoothSerial BTSerial;

struct Slot {
  String label;
  String value;
};

Slot slots[MAX_SLOTS];
String title = "Windows System Monitor";
String timestamp = "---- -- -- --:--:--";
String lastTitle;
String lastTimestamp;
Slot lastSlots[MAX_SLOTS];
int slotCount = 0;
int lastSlotCount = -1;
bool hasData = false;
bool frameDirty = true;
uint8_t titleBitmap[TITLE_BITMAP_BYTES];
bool titleBitmapValid = false;

String usbBuffer;
String btBuffer;
uint32_t lastDataMs = 0;
uint32_t lastStatusMs = 0;
bool staleDrawn = false;

void setup() {
  Serial.begin(SERIAL_BAUD);
  BTSerial.begin("SystemMonitor");

  tft.begin();
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  drawFrame();
  drawStatus("Waiting for Windows agent on USB serial...");

  Serial.println("System monitor firmware ready.");
}

void loop() {
  readFromStream(Serial, usbBuffer);
  readFromStream(BTSerial, btBuffer);

  if (millis() - lastStatusMs >= 1000) {
    lastStatusMs = millis();
    if (!hasData || millis() - lastDataMs > STALE_AFTER_MS) {
      if (!staleDrawn) {
        drawStatus("Waiting for Windows agent on USB serial...");
        staleDrawn = true;
      }
    }
  }
}

template <typename T>
void readFromStream(T &stream, String &buffer) {
  while (stream.available()) {
    char ch = stream.read();
    if (ch == '\n') {
      buffer.trim();
      if (buffer.length() > 0) {
        handlePacket(buffer);
      }
      buffer = "";
    } else if (ch != '\r') {
      buffer += ch;
      if (buffer.length() > 1700) {
        buffer = "";
      }
    }
  }
}

void handlePacket(const String &packet) {
  if (packet == "CMD|RELOAD") {
    forceReload();
  } else if (packet.startsWith("IMG|TITLE|")) {
    handleTitleImagePacket(packet);
  } else if (packet.startsWith("V2|")) {
    handleV2Packet(packet);
  } else {
    handleLegacyPacket(packet);
  }
}

void forceReload() {
  if (!hasData) {
    frameDirty = true;
    drawStatus("Waiting for Windows agent on USB serial...");
    return;
  }
  frameDirty = true;
  staleDrawn = false;
  drawMetrics();
}

void handleTitleImagePacket(const String &packet) {
  String parts[5];
  int count = split(packet, '|', parts, 5);
  if (count != 5) {
    Serial.println("Bad title image packet");
    return;
  }

  int width = parts[2].toInt();
  int height = parts[3].toInt();
  String hex = parts[4];
  if (width != TITLE_BITMAP_W || height != TITLE_BITMAP_H || hex.length() < TITLE_BITMAP_BYTES * 2) {
    Serial.println("Bad title image size");
    return;
  }

  for (int i = 0; i < TITLE_BITMAP_BYTES; i++) {
    int high = hexNibble(hex.charAt(i * 2));
    int low = hexNibble(hex.charAt(i * 2 + 1));
    if (high < 0 || low < 0) {
      Serial.println("Bad title image hex");
      return;
    }
    titleBitmap[i] = (high << 4) | low;
  }
  titleBitmapValid = true;
  lastTitle = "";
}

int hexNibble(char ch) {
  if (ch >= '0' && ch <= '9') {
    return ch - '0';
  }
  if (ch >= 'a' && ch <= 'f') {
    return ch - 'a' + 10;
  }
  if (ch >= 'A' && ch <= 'F') {
    return ch - 'A' + 10;
  }
  return -1;
}

void handleV2Packet(const String &packet) {
  String parts[14];
  int count = split(packet, '|', parts, 14);
  if (count < 4) {
    Serial.println("Bad V2 packet");
    return;
  }

  title = cleanText(parts[1]);
  timestamp = cleanText(parts[2]);
  slotCount = min(count - 3, MAX_SLOTS);

  for (int i = 0; i < slotCount; i++) {
    int eq = parts[i + 3].indexOf('=');
    if (eq < 0) {
      slots[i].label = "Slot";
      slots[i].value = cleanText(parts[i + 3]);
    } else {
      slots[i].label = cleanText(parts[i + 3].substring(0, eq));
      slots[i].value = cleanText(parts[i + 3].substring(eq + 1));
    }
  }
  for (int i = slotCount; i < MAX_SLOTS; i++) {
    slots[i].label = "";
    slots[i].value = "";
  }

  hasData = true;
  staleDrawn = false;
  lastDataMs = millis();
  drawMetrics();
}

void handleLegacyPacket(const String &packet) {
  String fields[10];
  int count = split(packet, ',', fields, 10);
  if (count != 10) {
    Serial.print("Bad packet: ");
    Serial.println(packet);
    return;
  }

  title = "Windows System Monitor";
  timestamp = cleanText(fields[0]);
  slotCount = 7;
  slots[0] = {"CPU Temp", fields[1] + " C"};
  slots[1] = {"CPU Load", fields[3] + "%"};
  slots[2] = {"Fan", fields[2] + "%"};
  slots[3] = {"Memory", fields[4] + "/" + fields[5] + " GB"};
  slots[4] = {"GPU Temp", fields[6] + " C"};
  slots[5] = {"GPU Load", fields[7] + "%"};
  slots[6] = {"C Drive", fields[8] + "/" + fields[9] + " GB"};

  hasData = true;
  staleDrawn = false;
  lastDataMs = millis();
  drawMetrics();
}

int split(const String &text, char delimiter, String *out, int maxParts) {
  int start = 0;
  int count = 0;
  for (int i = 0; i <= text.length() && count < maxParts; i++) {
    if (i == text.length() || text.charAt(i) == delimiter) {
      out[count++] = text.substring(start, i);
      start = i + 1;
    }
  }
  return count;
}

String cleanText(String value) {
  value.replace("`", "'");
  value.replace("|", "/");
  value.trim();
  return value;
}

void drawFrame() {
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(TL_DATUM);
  drawSeparators();

  lastTitle = "";
  lastTimestamp = "";
  lastSlotCount = -1;
  for (int i = 0; i < MAX_SLOTS; i++) {
    lastSlots[i].label = "";
    lastSlots[i].value = "";
  }
  frameDirty = false;
}

void drawContentFrame() {
  tft.fillRect(0, 42, 480, 250, TFT_BLACK);
  drawSeparators();
  for (int i = 0; i < MAX_SLOTS; i++) {
    lastSlots[i].label = "";
    lastSlots[i].value = "";
  }
  lastSlotCount = -1;
  frameDirty = false;
}

void drawSeparators() {
  tft.drawFastHLine(10, 40, 460, TFT_DARKGREY);
  tft.drawFastHLine(10, 41, 460, TFT_DARKGREY);
  tft.drawFastHLine(10, 292, 460, TFT_DARKGREY);
  tft.drawFastHLine(10, 293, 460, TFT_DARKGREY);
  tft.drawFastVLine(240, 46, 236, TFT_DARKGREY);
}

void drawStatus(const String &message) {
  tft.fillRect(0, 0, 480, 320, TFT_BLACK);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextFont(2);
  tft.drawString("Windows System Monitor", 12, 10);
  tft.setTextColor(TFT_ORANGE, TFT_BLACK);
  tft.drawString(message, 24, 140);
  lastTitle = "";
  lastTimestamp = "";
  frameDirty = true;
}

void drawMetrics() {
  bool layoutChanged = frameDirty || slotCount != lastSlotCount || title != lastTitle;
  if (!layoutChanged) {
    for (int i = 0; i < MAX_SLOTS; i++) {
      if (slots[i].label != lastSlots[i].label) {
        layoutChanged = true;
        break;
      }
    }
  }

  if (layoutChanged) {
    drawContentFrame();
  }

  if (title != lastTitle) {
    drawIp();
    lastTitle = title;
  }

  if (timestamp != lastTimestamp) {
    drawTimestamp(lastTimestamp.length() == 0);
  }

  for (int i = 0; i < MAX_SLOTS; i++) {
    drawSlot(i, layoutChanged);
  }
  lastSlotCount = slotCount;
}

void drawIp() {
  tft.fillRect(10, 294, 460, 26, TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextFont(4);
  tft.drawCentreString(title, 240, 295, 4);
}

void drawTimestamp(bool force) {
  const int charCount = 19;
  const int cellW = 17;
  const int startX = (480 - charCount * cellW) / 2;
  const int y = 8;
  String current = timestamp;
  String previous = lastTimestamp;

  while (current.length() < charCount) {
    current += " ";
  }
  while (previous.length() < charCount) {
    previous += " ";
  }

  if (force) {
    tft.fillRect(10, 4, 460, 34, TFT_BLACK);
  }

  tft.setTextColor(TFT_SKYBLUE, TFT_BLACK);
  tft.setTextFont(4);
  for (int i = 0; i < charCount; i++) {
    if (force || current.charAt(i) != previous.charAt(i)) {
      int x = startX + i * cellW;
      tft.fillRect(x, 4, cellW, 34, TFT_BLACK);
      String ch = current.substring(i, i + 1);
      if (ch != " ") {
        tft.drawCentreString(ch, x + cellW / 2, y, 4);
      }
    }
  }
  lastTimestamp = timestamp;
}

void drawSlot(int index, bool layoutChanged) {
  int col = index % 2;
  int row = index / 2;
  int x = col == 0 ? 14 : 250;
  int y = 46 + row * 48;
  int w = col == 0 ? 210 : 216;

  if (index >= slotCount) {
    if (lastSlots[index].label.length() > 0 || lastSlots[index].value.length() > 0 || layoutChanged) {
      tft.fillRect(x, y, w, 36, TFT_BLACK);
      lastSlots[index].label = "";
      lastSlots[index].value = "";
    }
    return;
  }

  if (layoutChanged || slots[index].label != lastSlots[index].label) {
    tft.fillRect(x, y, w, 16, TFT_BLACK);
    tft.setTextColor(TFT_SKYBLUE, TFT_BLACK);
    tft.setTextFont(2);
    tft.drawString(slots[index].label, x, y);
    lastSlots[index].label = slots[index].label;
  }

  if (layoutChanged || slots[index].value != lastSlots[index].value) {
    uint16_t newColor = slotColor(slots[index].value);
    uint16_t oldColor = slotColor(lastSlots[index].value);
    tft.setTextFont(4);
    if (layoutChanged || newColor != oldColor) {
      tft.fillRect(x, y + 18, w, 24, TFT_BLACK);
    } else {
      int oldW = tft.textWidth(lastSlots[index].value, 4);
      int newW = tft.textWidth(slots[index].value, 4);
      if (newW < oldW) {
        tft.fillRect(x + newW, y + 18, oldW - newW + 4, 24, TFT_BLACK);
      }
    }
    tft.setTextColor(newColor, TFT_BLACK);
    tft.drawString(slots[index].value, x, y + 18);
    lastSlots[index].value = slots[index].value;
  }
}

uint16_t slotColor(const String &value) {
  int percentAt = value.indexOf('%');
  if (percentAt > 0) {
    int valueStart = percentAt - 1;
    while (valueStart > 0 && isDigit(value.charAt(valueStart - 1))) {
      valueStart--;
    }
    int pct = value.substring(valueStart, percentAt).toInt();
    if (pct >= 90) {
      return TFT_RED;
    }
  }
  return TFT_WHITE;
}
