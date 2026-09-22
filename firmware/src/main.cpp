// Stick Assistant firmware for the M5Stack M5StickS3.
//
// The device is a voice terminal: it records while BtnA is held, uploads the WAV to
// the Django backend, and plays the spoken reply. All intelligence, OpenAI access
// and agenda storage stay on the server.
//
//   BtnA (front, G11): hold to talk, release to send.
//   BtnB (side, G12):  click to hear today's agenda.
//   Any button during playback: stop playback.
//   Hold BtnB while powering on: microphone/speaker self-test (no network).

#include <M5Unified.h>

#include "api_client.h"
#include "audio.h"
#include "config.h"
#include "display.h"
#include "wifi_manager.h"

namespace {

UiState g_state = UiState::Booting;
uint32_t g_error_since = 0;
uint32_t g_last_status_bar = 0;
bool g_mic_test = false;

constexpr const char *READY_HINT = "Hold A to talk.  B: today's agenda.";

void setState(UiState state, const char *message = nullptr) {
  g_state = state;
  Serial.printf("[state] %s%s%s\n", uiStateName(state), message ? " - " : "",
                message ? message : "");
  displayShow(state, message);
}

void showError(const char *message) {
  setState(UiState::Error, message);
  g_error_since = millis();
}

bool keepRecording() {
#if RECORD_MODE_HOLD
  return M5.BtnA.isPressed();
#else
  return !M5.BtnA.wasPressed();  // toggle mode: the next press stops
#endif
}

void onLevel(uint32_t elapsed_ms, int peak) {
  displayRecording(elapsed_ms, MAX_RECORD_SECONDS * 1000, peak);
}

void onUploaded() { setState(UiState::Thinking, "Waiting for the assistant..."); }

// Short human-readable description of a failed API call.
String describeFailure(const ApiResult &r) {
  if (r.transportError()) return String("Server unreachable (") + r.error + ")";
  switch (r.http_status) {
    case 401: return "Device token rejected. Check DEVICE_API_TOKEN in secrets.h.";
    case 503: return "Server not configured (DEVICE_API_TOKEN missing on server).";
    case 409: return "Request ID conflict. Please try again.";
    case 413: return "Recording too large.";
    case 415:
    case 400: return String("Recording rejected: ") + r.error;
    case 429: return "Server busy. Try again in a moment.";
    default: break;
  }
  if (r.reply.length()) return r.reply;
  String text = String("Server error ") + r.http_status;
  if (r.error.length()) text += String(": ") + r.error;
  return text;
}

// Polls the request status until it is no longer processing (bounded).
bool waitForResult(const char *request_id, ApiResult &result) {
  const uint32_t start = millis();
  while (millis() - start < STATUS_POLL_MAX_MS) {
    M5.delay(STATUS_POLL_INTERVAL_MS);
    M5.update();
    ApiResult status;
    apiGetStatus(request_id, status);
    if (status.transportError()) {
      if (!wifiConnected()) wifiConnect(WIFI_CONNECT_TIMEOUT_MS);
      continue;
    }
    if (status.http_status == 200 && status.status != "processing") {
      result = status;
      return true;
    }
    if (status.http_status != 200) {
      result = status;
      return false;
    }
  }
  result.error = "timeout waiting for the server";
  return false;
}

// Uploads a recording. Every retry reuses the SAME request_id, so the server executes
// the command at most once even if a response was lost.
bool submitVoice(size_t samples, ApiResult &result) {
  char request_id[37];
  makeRequestId(request_id);
  Serial.printf("[voice] request_id %s\n", request_id);

  for (int attempt = 0; attempt < UPLOAD_ATTEMPTS; ++attempt) {
    if (attempt > 0) {
      M5.delay(RETRY_DELAY_MS);
      Serial.printf("[voice] retry %d with the same request_id\n", attempt);
    }
    if (!wifiConnected() && !wifiConnect(WIFI_CONNECT_TIMEOUT_MS)) {
      result = ApiResult();
      result.http_status = -1;
      result.error = "no Wi-Fi";
      continue;
    }
    setState(UiState::Uploading, "Sending recording...");
    apiPostVoice(request_id, audioRecordBuffer(), samples, MIC_SAMPLE_RATE, result, onUploaded);

    if (result.transportError()) {
      // The upload may have reached the server before the connection dropped:
      // ask for the outcome before sending the audio again.
      ApiResult status;
      apiGetStatus(request_id, status);
      if (status.http_status == 200) {
        result = status;
        return result.status == "processing" ? waitForResult(request_id, result) : true;
      }
      continue;  // unknown to the server (404) or unreachable: re-upload, same id
    }
    if (result.http_status == 202 || result.status == "processing") {
      return waitForResult(request_id, result);
    }
    if (result.http_status == 429) {
      continue;
    }
    if (result.http_status == 502 && result.retryable && attempt + 1 < UPLOAD_ATTEMPTS) {
      continue;  // failed before touching the agenda: safe to retry
    }
    return result.http_status == 200 || result.http_status == 502;
  }
  return false;
}

// Shows the reply, downloads the generated WAV and plays it.
void speakResult(const ApiResult &result) {
  const char *reply = result.reply.length() ? result.reply.c_str() : "(no reply)";
  if (!result.audio_url.length()) {
    setState(result.status == "error" ? UiState::Error : UiState::Ready, reply);
    g_error_since = millis();
    return;
  }
  setState(UiState::Speaking, reply);
  uint8_t *wav = nullptr;
  size_t wav_len = 0;
  String error;
  if (!apiDownloadAudio(result.audio_url, &wav, &wav_len, error)) {
    showError((String("Audio download failed: ") + error + "\n" + reply).c_str());
    return;
  }
  const char *play_error = nullptr;
  const PlayResult played = audioPlayWav(wav, wav_len, &play_error);
  free(wav);
  if (played == PlayResult::Failed) {
    showError((String("Playback failed: ") + (play_error ? play_error : "?")).c_str());
    return;
  }
  setState(result.status == "error" ? UiState::Error : UiState::Ready, reply);
  g_error_since = millis();
}

void handleVoiceButton() {
  if (!wifiConnected()) {
    setState(UiState::Offline, "No Wi-Fi. Reconnecting...");
    return;
  }
  setState(UiState::Recording,
           RECORD_MODE_HOLD ? "Speak now. Release A to send." : "Speak now. Press A to send.");
  const size_t samples = audioRecord(keepRecording, onLevel);
  if (samples * 1000 / MIC_SAMPLE_RATE < MIN_RECORD_MS) {
    setState(UiState::Ready, "Too short. Hold A while you speak.");
    return;
  }
  ApiResult result;
  if (!submitVoice(samples, result)) {
    showError(describeFailure(result).c_str());
    return;
  }
  if (result.transcript.length()) {
    Serial.printf("[voice] heard: %s\n", result.transcript.c_str());
  }
  speakResult(result);
}

void handleAgendaButton() {
  if (!wifiConnected()) {
    setState(UiState::Offline, "No Wi-Fi. Reconnecting...");
    return;
  }
  char request_id[37];
  makeRequestId(request_id);
  setState(UiState::Thinking, "Reading today's agenda...");
  ApiResult result;
  for (int attempt = 0; attempt < UPLOAD_ATTEMPTS; ++attempt) {
    if (attempt > 0) M5.delay(RETRY_DELAY_MS);
    apiPostAgendaSummary(request_id, SUMMARY_LANGUAGE, result);  // same id on retry
    if (!result.transportError() && result.http_status != 429) break;
  }
  if (result.http_status != 200) {
    showError(describeFailure(result).c_str());
    return;
  }
  speakResult(result);
}

// Local hardware test: record while A is held, then play it back. No network.
void micTestLoop() {
  if (!M5.BtnA.wasPressed()) {
    return;
  }
  setState(UiState::Recording, "Mic test: speak, release A to play back.");
  const size_t samples = audioRecord([]() { return M5.BtnA.isPressed(); }, onLevel);
  int peak = 0;
  const int16_t *buf = audioRecordBuffer();
  for (size_t i = 0; i < samples; ++i) peak = max(peak, abs(static_cast<int>(buf[i])));
  char msg[96];
  snprintf(msg, sizeof(msg), "Playing %.1f s @ %u Hz, peak %d", samples / (float)MIC_SAMPLE_RATE,
           (unsigned)MIC_SAMPLE_RATE, peak);
  setState(UiState::Speaking, msg);
  audioPlayPcm(buf, samples, MIC_SAMPLE_RATE, false);
  setState(UiState::MicTest, "Hold A: record. Release: play back.");
}

}  // namespace

void setup() {
  auto cfg = M5.config();
  cfg.serial_baudrate = 115200;
  cfg.internal_mic = true;
  cfg.internal_spk = true;
  M5.begin(cfg);
  displayBegin();
  setState(UiState::Booting, "Starting...");

  if (M5.getBoard() != m5::board_t::board_M5StickS3) {
    Serial.printf("[boot] WARNING: board id %d is not M5StickS3\n", (int)M5.getBoard());
  }
  Serial.printf("[boot] PSRAM %u bytes free, heap %u bytes free\n",
                (unsigned)ESP.getFreePsram(), (unsigned)ESP.getFreeHeap());

  if (!audioBegin()) {
    // Stay responsive with a clear message; never enter a reboot loop.
    showError("PSRAM allocation failed. Check the PSRAM build settings.");
    for (;;) {
      M5.delay(1000);
    }
  }
  audioStopAll();

  M5.update();
  g_mic_test = M5.BtnB.isPressed();
  if (g_mic_test) {
    setState(UiState::MicTest, "Hold A: record. Release: play back.");
    return;
  }

  wifiBegin();
  setState(UiState::Connecting, WIFI_SSID);
  if (wifiConnect(WIFI_CONNECT_TIMEOUT_MS)) {
    setState(UiState::Ready, READY_HINT);
  } else {
    setState(UiState::Offline, "Wi-Fi failed. Retrying in the background.");
  }
}

void loop() {
  M5.update();
  if (g_mic_test) {
    micTestLoop();
    M5.delay(10);
    return;
  }

  wifiMaintain();
  const bool online = wifiConnected();
  if (!online && g_state != UiState::Offline) {
    setState(UiState::Offline, "Wi-Fi lost. Reconnecting...");
  } else if (online && g_state == UiState::Offline) {
    setState(UiState::Ready, READY_HINT);
  } else if (g_state == UiState::Error && millis() - g_error_since > ERROR_DISPLAY_MS) {
    setState(UiState::Ready, READY_HINT);
  }

  if (M5.BtnA.wasPressed()) {
    handleVoiceButton();
  } else if (M5.BtnB.wasClicked()) {
    handleAgendaButton();
  }

  if (millis() - g_last_status_bar > 10000) {
    g_last_status_bar = millis();
    displayStatusBar();
  }
  M5.delay(10);
}
