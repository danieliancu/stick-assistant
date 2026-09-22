// Simple status UI for the 240x135 ST7789 display.
#pragma once

#include <stdint.h>

enum class UiState {
  Booting,
  Connecting,
  Ready,
  Recording,
  Uploading,
  Thinking,
  Speaking,
  Error,
  Offline,
  MicTest,
};

const char *uiStateName(UiState state);
void displayBegin();
// Redraws the whole screen: status bar, state title and an optional message.
// The message is UTF-8; Romanian diacritics are transliterated for the built-in font.
void displayShow(UiState state, const char *message = nullptr);
// Lightweight update of the recording screen (elapsed time and input level).
void displayRecording(uint32_t elapsed_ms, uint32_t max_ms, int peak);
// Refreshes only the status bar (Wi-Fi / battery).
void displayStatusBar();
