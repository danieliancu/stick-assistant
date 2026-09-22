#include <M5Unified.h>

#include "display.h"
#include "wifi_manager.h"

namespace {

constexpr int STATUS_BAR_H = 18;
constexpr size_t MAX_MESSAGE_CHARS = 180;

uint16_t stateColor(UiState state) {
  switch (state) {
    case UiState::Ready: return TFT_GREEN;
    case UiState::Recording: return TFT_RED;
    case UiState::Uploading:
    case UiState::Thinking: return TFT_YELLOW;
    case UiState::Speaking: return TFT_CYAN;
    case UiState::Error: return TFT_ORANGE;
    case UiState::Offline: return TFT_DARKGREY;
    case UiState::MicTest: return TFT_MAGENTA;
    default: return TFT_WHITE;
  }
}

// Maps UTF-8 Romanian letters (both comma-below and cedilla forms) to ASCII, since
// the built-in fonts lack these glyphs. Other non-ASCII code points become '?'.
void transliterate(const char *in, char *out, size_t out_size) {
  size_t o = 0;
  const auto *p = reinterpret_cast<const uint8_t *>(in);
  while (*p && o + 1 < out_size) {
    uint32_t cp;
    if (*p < 0x80) {
      cp = *p++;
    } else if ((*p & 0xE0) == 0xC0 && p[1]) {
      cp = ((p[0] & 0x1F) << 6) | (p[1] & 0x3F);
      p += 2;
    } else if ((*p & 0xF0) == 0xE0 && p[1] && p[2]) {
      cp = ((p[0] & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F);
      p += 3;
    } else {
      ++p;  // skip 4-byte sequences / invalid bytes
      out[o++] = '?';
      continue;
    }
    char c;
    switch (cp) {
      case 0x0103: case 0x00E2: c = 'a'; break;  // ă â
      case 0x0102: case 0x00C2: c = 'A'; break;  // Ă Â
      case 0x00EE: c = 'i'; break;               // î
      case 0x00CE: c = 'I'; break;               // Î
      case 0x0219: case 0x015F: c = 's'; break;  // ș ş
      case 0x0218: case 0x015E: c = 'S'; break;  // Ș Ş
      case 0x021B: case 0x0163: c = 't'; break;  // ț ţ
      case 0x021A: case 0x0162: c = 'T'; break;  // Ț Ţ
      case 0x201E: case 0x201D: case 0x201C: c = '"'; break;
      case 0x2019: case 0x2018: c = '\''; break;
      case 0x2013: case 0x2014: c = '-'; break;
      case 0x2026: c = '.'; break;
      default: c = cp < 0x80 ? static_cast<char>(cp) : '?';
    }
    out[o++] = c;
  }
  out[o] = '\0';
}

}  // namespace

const char *uiStateName(UiState state) {
  switch (state) {
    case UiState::Booting: return "BOOTING";
    case UiState::Connecting: return "CONNECTING";
    case UiState::Ready: return "READY";
    case UiState::Recording: return "RECORDING";
    case UiState::Uploading: return "UPLOADING";
    case UiState::Thinking: return "THINKING";
    case UiState::Speaking: return "SPEAKING";
    case UiState::Error: return "ERROR";
    case UiState::Offline: return "OFFLINE";
    case UiState::MicTest: return "MIC TEST";
  }
  return "?";
}

void displayBegin() {
  M5.Display.setRotation(1);  // landscape 240x135
  M5.Display.setBrightness(160);
  M5.Display.fillScreen(TFT_BLACK);
}

void displayStatusBar() {
  auto &d = M5.Display;
  d.fillRect(0, 0, d.width(), STATUS_BAR_H, TFT_NAVY);
  d.setTextFont(1);
  d.setTextSize(1);
  d.setTextColor(TFT_WHITE, TFT_NAVY);
  d.setCursor(4, 5);
  if (wifiConnected()) {
    d.printf("WiFi %d dBm", wifiRssi());
  } else {
    d.print("WiFi --");
  }
  const int32_t battery = M5.Power.getBatteryLevel();
  if (battery >= 0) {
    d.setCursor(d.width() - 48, 5);
    d.printf("%3d%%", (int)battery);
  }
}

void displayShow(UiState state, const char *message) {
  auto &d = M5.Display;
  d.startWrite();
  d.fillScreen(TFT_BLACK);
  displayStatusBar();

  d.setTextColor(stateColor(state), TFT_BLACK);
  d.setTextFont(2);
  d.setTextSize(1);
  d.setCursor(6, STATUS_BAR_H + 6);
  d.print(uiStateName(state));

  if (message != nullptr && *message) {
    char ascii[MAX_MESSAGE_CHARS + 1];
    transliterate(message, ascii, sizeof(ascii));
    d.setTextColor(TFT_WHITE, TFT_BLACK);
    d.setTextFont(1);
    d.setTextWrap(true);
    d.setCursor(6, STATUS_BAR_H + 30);
    d.print(ascii);  // wraps; long replies are cut to fit the screen
  }
  d.endWrite();
}

void displayRecording(uint32_t elapsed_ms, uint32_t max_ms, int peak) {
  auto &d = M5.Display;
  const int y = STATUS_BAR_H + 34;
  d.startWrite();
  d.fillRect(0, y, d.width(), 40, TFT_BLACK);
  d.setTextColor(TFT_WHITE, TFT_BLACK);
  d.setTextFont(2);
  d.setCursor(6, y);
  d.printf("%2u.%u s / %u s", (unsigned)(elapsed_ms / 1000), (unsigned)(elapsed_ms % 1000) / 100,
           (unsigned)(max_ms / 1000));
  const int bar_w = d.width() - 12;
  const int level = min(bar_w, peak * bar_w / 12000);
  d.drawRect(6, y + 22, bar_w, 10, TFT_DARKGREY);
  d.fillRect(7, y + 23, max(0, level - 2), 8, TFT_RED);
  d.endWrite();
}
