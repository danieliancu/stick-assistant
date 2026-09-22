// Microphone capture and speaker playback through M5Unified.
// The mic (I2S1) and speaker (I2S0) share the ES8311 clock pins on the StickS3,
// so only one of them is ever active: every entry point switches explicitly.
#pragma once

#include <stddef.h>
#include <stdint.h>

// Allocates the bounded recording buffer in PSRAM. Returns false on failure.
bool audioBegin();
int16_t *audioRecordBuffer();

void audioSwitchToMic();
void audioSwitchToSpeaker();
void audioStopAll();

// Records into the PSRAM buffer while keepGoing() returns true, up to
// MAX_RECORD_SAMPLES. onLevel() is called ~10x per second. Returns sample count.
size_t audioRecord(bool (*keepGoing)(), void (*onLevel)(uint32_t elapsed_ms, int peak));

struct WavInfo {
  uint32_t sample_rate;
  uint16_t channels;
  const int16_t *samples;  // interleaved if stereo
  size_t sample_count;     // total int16 values
};

// Validates RIFF/WAVE, walks the chunk list (metadata chunks are skipped, no fixed
// 44-byte offset is assumed), requires 16-bit PCM and returns the data chunk.
// ``data`` must be writable: a misaligned data chunk is moved to an aligned offset.
bool parseWav(uint8_t *data, size_t len, WavInfo &out, const char **error);

enum class PlayResult { Finished, Cancelled, Failed };

// Plays PCM through the speaker; any button press cancels. Bounded by duration.
PlayResult audioPlayPcm(const int16_t *samples, size_t count, uint32_t rate, bool stereo);
PlayResult audioPlayWav(uint8_t *data, size_t len, const char **error);
