// Compile-time configuration for the Stick Assistant firmware.
#pragma once

#include <stddef.h>
#include <stdint.h>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing include/secrets.h: copy include/secrets.example.h to include/secrets.h"
#endif

// --- Recording (ES8311 via M5Unified Mic_Class) ------------------------------------
// The WAV header always states exactly this rate; the mic is configured with it.
static constexpr uint32_t MIC_SAMPLE_RATE = 16000;
static constexpr uint32_t MAX_RECORD_SECONDS = 12;
static constexpr uint32_t MIN_RECORD_MS = 400;
static constexpr size_t MIC_CHUNK_SAMPLES = 256;
static constexpr size_t MAX_RECORD_SAMPLES = MIC_SAMPLE_RATE * MAX_RECORD_SECONDS;

// 1 = press and hold BtnA to record (default). 0 = press once to start, again to stop
// (fallback if holding the front key interferes with power management on your unit).
#ifndef RECORD_MODE_HOLD
#define RECORD_MODE_HOLD 1
#endif

// --- Playback ------------------------------------------------------------------------
static constexpr uint8_t SPEAKER_VOLUME = 200;                 // 0..255
static constexpr size_t MAX_REPLY_AUDIO_BYTES = 3 * 1024 * 1024;  // ~60 s at 24 kHz
static constexpr uint32_t MIN_PLAYBACK_RATE = 8000;
static constexpr uint32_t MAX_PLAYBACK_RATE = 48000;

// --- Network ---------------------------------------------------------------------------
static constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
static constexpr uint32_t WIFI_RETRY_INTERVAL_MS = 10000;
static constexpr uint32_t HTTP_CONNECT_TIMEOUT_MS = 8000;
// Server-side processing (STT + LLM + TTS) usually takes 5-20 s.
static constexpr uint16_t HTTP_RESPONSE_TIMEOUT_MS = 60000;
static constexpr uint32_t HTTP_DOWNLOAD_TIMEOUT_MS = 30000;
static constexpr size_t MAX_JSON_RESPONSE_BYTES = 8 * 1024;
static constexpr int UPLOAD_ATTEMPTS = 3;
static constexpr uint32_t RETRY_DELAY_MS = 1500;
static constexpr uint32_t STATUS_POLL_INTERVAL_MS = 1500;
static constexpr uint32_t STATUS_POLL_MAX_MS = 90000;

// --- Behaviour ---------------------------------------------------------------------------
static constexpr const char *SUMMARY_LANGUAGE = "ro";  // Button B summary: "ro" or "en"
static constexpr uint32_t ERROR_DISPLAY_MS = 5000;

// --- Transport security (checked at compile time) -----------------------------------
namespace cfgcheck {
constexpr bool startsWith(const char *s, const char *prefix) {
  return *prefix == '\0' ? true : (*s == *prefix && startsWith(s + 1, prefix + 1));
}
}  // namespace cfgcheck

static constexpr bool BACKEND_IS_HTTPS = cfgcheck::startsWith(BACKEND_BASE_URL, "https://");
static constexpr bool BACKEND_IS_HTTP = cfgcheck::startsWith(BACKEND_BASE_URL, "http://");

#ifdef ALLOW_INSECURE_HTTP_DEV
static constexpr bool INSECURE_HTTP_ALLOWED = true;
#else
static constexpr bool INSECURE_HTTP_ALLOWED = false;
#endif

static_assert(BACKEND_IS_HTTPS || BACKEND_IS_HTTP,
              "BACKEND_BASE_URL must start with https:// or http://");
static_assert(!BACKEND_IS_HTTP || INSECURE_HTTP_ALLOWED,
              "Plain http:// requires #define ALLOW_INSECURE_HTTP_DEV 1 (LAN development only)");
static_assert(!cfgcheck::startsWith(BACKEND_BASE_URL, "http://localhost") &&
                  !cfgcheck::startsWith(BACKEND_BASE_URL, "https://localhost") &&
                  !cfgcheck::startsWith(BACKEND_BASE_URL, "http://127.") &&
                  !cfgcheck::startsWith(BACKEND_BASE_URL, "https://127."),
              "localhost/127.x is the device itself; use the computer's LAN IP");
