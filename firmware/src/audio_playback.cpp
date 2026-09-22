#include <M5Unified.h>

#include "audio.h"
#include "config.h"

namespace {

uint32_t readLE32(const uint8_t *p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

uint16_t readLE16(const uint8_t *p) {
  return static_cast<uint16_t>(p[0] | (p[1] << 8));
}

constexpr uint16_t WAVE_FORMAT_PCM = 0x0001;
constexpr uint16_t WAVE_FORMAT_EXTENSIBLE = 0xFFFE;

}  // namespace

bool parseWav(uint8_t *data, size_t len, WavInfo &out, const char **error) {
  const char *dummy = nullptr;
  const char **err = error != nullptr ? error : &dummy;
  if (data == nullptr || len < 12 || memcmp(data, "RIFF", 4) != 0 ||
      memcmp(data + 8, "WAVE", 4) != 0) {
    *err = "not a RIFF/WAVE file";
    return false;
  }

  bool have_fmt = false;
  uint16_t format = 0, channels = 0, bits = 0;
  uint32_t rate = 0;
  size_t offset = 12;

  // Walk chunks; the RIFF size field is ignored (it may be 0 or 0xFFFFFFFF when streamed).
  while (offset + 8 <= len) {
    const uint8_t *hdr = data + offset;
    const uint32_t size = readLE32(hdr + 4);
    const size_t body = offset + 8;

    if (memcmp(hdr, "fmt ", 4) == 0) {
      if (size < 16 || body + size > len) {
        *err = "invalid fmt chunk";
        return false;
      }
      format = readLE16(data + body);
      channels = readLE16(data + body + 2);
      rate = readLE32(data + body + 4);
      bits = readLE16(data + body + 14);
      if (format == WAVE_FORMAT_EXTENSIBLE && size >= 40) {
        format = readLE16(data + body + 24);  // first two bytes of the SubFormat GUID
      }
      have_fmt = true;
    } else if (memcmp(hdr, "data", 4) == 0) {
      if (!have_fmt) {
        *err = "data chunk before fmt chunk";
        return false;
      }
      if (format != WAVE_FORMAT_PCM || bits != 16) {
        *err = "only 16-bit PCM is supported";
        return false;
      }
      if (channels != 1 && channels != 2) {
        *err = "unsupported channel count";
        return false;
      }
      if (rate < MIN_PLAYBACK_RATE || rate > MAX_PLAYBACK_RATE) {
        *err = "unsupported sample rate";
        return false;
      }
      size_t data_size = size;
      if (body + data_size > len) {
        data_size = len - body;  // truncated download: play what arrived
      }
      const size_t frame_bytes = 2u * channels;
      data_size -= data_size % frame_bytes;
      if (data_size == 0) {
        *err = "empty data chunk";
        return false;
      }
      uint8_t *pcm = data + body;
      if (reinterpret_cast<uintptr_t>(pcm) % alignof(int16_t) != 0) {
        memmove(data, pcm, data_size);  // align for int16_t access; header no longer needed
        pcm = data;
      }
      out.sample_rate = rate;
      out.channels = channels;
      out.samples = reinterpret_cast<const int16_t *>(pcm);
      out.sample_count = data_size / 2;
      return true;
    }
    // Skip this chunk (LIST, fact, ...), including the RIFF pad byte for odd sizes.
    const size_t next = body + static_cast<size_t>(size) + (size & 1u);
    if (next <= offset) {
      break;  // overflow guard
    }
    offset = next;
  }
  *err = have_fmt ? "no data chunk" : "no fmt chunk";
  return false;
}

PlayResult audioPlayPcm(const int16_t *samples, size_t count, uint32_t rate, bool stereo) {
  if (samples == nullptr || count == 0 || rate == 0) {
    return PlayResult::Failed;
  }
  audioSwitchToSpeaker();
  if (!M5.Speaker.playRaw(samples, count, rate, stereo, 1, 0, true)) {
    Serial.println("[audio] playRaw rejected the buffer");
    return PlayResult::Failed;
  }

  const uint32_t frames = stereo ? count / 2 : count;
  const uint32_t expected_ms = static_cast<uint32_t>((uint64_t)frames * 1000 / rate);
  const uint32_t deadline = millis() + expected_ms + 3000;  // bounded playback
  PlayResult result = PlayResult::Finished;
  while (M5.Speaker.isPlaying()) {
    M5.update();
    if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) {
      result = PlayResult::Cancelled;
      break;
    }
    if (static_cast<int32_t>(millis() - deadline) > 0) {
      Serial.println("[audio] playback exceeded expected duration; stopping");
      break;
    }
    M5.delay(5);
  }
  M5.Speaker.stop();
  // Release the I2S bus so the next recording can start cleanly.
  M5.Speaker.end();
  return result;
}

PlayResult audioPlayWav(uint8_t *data, size_t len, const char **error) {
  WavInfo info{};
  if (!parseWav(data, len, info, error)) {
    return PlayResult::Failed;
  }
  Serial.printf("[audio] playing %u Hz, %u ch, %.2f s\n", (unsigned)info.sample_rate,
                (unsigned)info.channels,
                info.sample_count / (float)info.channels / info.sample_rate);
  return audioPlayPcm(info.samples, info.sample_count, info.sample_rate, info.channels == 2);
}
