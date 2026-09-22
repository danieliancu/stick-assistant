#include <M5Unified.h>
#include <esp_heap_caps.h>

#include "audio.h"
#include "config.h"

namespace {
int16_t *g_record_buffer = nullptr;
}

bool audioBegin() {
  if (g_record_buffer != nullptr) {
    return true;
  }
  // 12 s * 16 kHz * 2 bytes = 384 KB: PSRAM, never the stack or internal RAM.
  g_record_buffer = static_cast<int16_t *>(
      heap_caps_malloc(MAX_RECORD_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (g_record_buffer == nullptr) {
    Serial.println("[audio] PSRAM allocation for recording buffer failed");
    return false;
  }
  memset(g_record_buffer, 0, MAX_RECORD_SAMPLES * sizeof(int16_t));
  return true;
}

int16_t *audioRecordBuffer() { return g_record_buffer; }

void audioSwitchToMic() {
  if (M5.Speaker.isEnabled()) {
    M5.Speaker.stop();
    M5.Speaker.end();
  }
  if (!M5.Mic.isEnabled()) {
    auto cfg = M5.Mic.config();
    cfg.sample_rate = MIC_SAMPLE_RATE;
    M5.Mic.config(cfg);
    M5.Mic.begin();
  }
}

void audioSwitchToSpeaker() {
  if (M5.Mic.isEnabled()) {
    while (M5.Mic.isRecording()) {
      M5.delay(1);
    }
    M5.Mic.end();
  }
  if (!M5.Speaker.isEnabled()) {
    M5.Speaker.begin();
  }
  M5.Speaker.setVolume(SPEAKER_VOLUME);
}

void audioStopAll() {
  if (M5.Speaker.isEnabled()) {
    M5.Speaker.stop();
    M5.Speaker.end();
  }
  if (M5.Mic.isEnabled()) {
    while (M5.Mic.isRecording()) {
      M5.delay(1);
    }
    M5.Mic.end();
  }
}

size_t audioRecord(bool (*keepGoing)(), void (*onLevel)(uint32_t elapsed_ms, int peak)) {
  if (g_record_buffer == nullptr) {
    return 0;
  }
  audioSwitchToMic();

  size_t queued = 0;
  const uint32_t start = millis();
  uint32_t last_level = 0;

  // Mic_Class::record() queues a chunk and fills it asynchronously; the chunks are
  // contiguous in the PSRAM buffer. Stop when the caller says so or the buffer is full.
  while (queued + MIC_CHUNK_SAMPLES <= MAX_RECORD_SAMPLES) {
    M5.update();
    if (!keepGoing()) {
      break;
    }
    if (M5.Mic.record(g_record_buffer + queued, MIC_CHUNK_SAMPLES, MIC_SAMPLE_RATE)) {
      queued += MIC_CHUNK_SAMPLES;
    } else {
      M5.delay(1);  // queue full: yield (keeps the watchdog and Wi-Fi task happy)
    }

    const uint32_t now = millis();
    if (onLevel != nullptr && now - last_level >= 100 && queued > MIC_CHUNK_SAMPLES * 2) {
      last_level = now;
      // Peak of the most recently *completed* chunk (two chunks behind the queue head).
      const int16_t *chunk = g_record_buffer + queued - MIC_CHUNK_SAMPLES * 3;
      int peak = 0;
      for (size_t i = 0; i < MIC_CHUNK_SAMPLES; ++i) {
        const int v = abs(static_cast<int>(chunk[i]));
        if (v > peak) peak = v;
      }
      onLevel(now - start, peak);
    }
  }

  // Wait until every queued chunk has actually been filled before using the data.
  while (M5.Mic.isRecording()) {
    M5.delay(1);
  }
  M5.Mic.end();
  Serial.printf("[audio] recorded %u samples (%.2f s at %u Hz)\n", (unsigned)queued,
                queued / (float)MIC_SAMPLE_RATE, (unsigned)MIC_SAMPLE_RATE);
  return queued;
}
