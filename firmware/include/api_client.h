// HTTP client for the Django backend. The device never talks to OpenAI.
#pragma once

#include <Arduino.h>

struct ApiResult {
  int http_status = 0;       // <= 0: transport error (no HTTP response)
  String status;             // success | no_speech | processing | error | busy | ...
  String transcript;
  String reply;
  String audio_url;
  String error;
  bool retryable = false;

  bool transportError() const { return http_status <= 0; }
};

// RFC 4122 version 4 UUID from the hardware RNG.
void makeRequestId(char out[37]);

// POST /api/voice/ (multipart: audio WAV + request_id). Streams the PCM buffer
// directly from PSRAM without copying. onUploaded() fires after the last byte.
void apiPostVoice(const char *request_id, const int16_t *pcm, size_t samples,
                  uint32_t sample_rate, ApiResult &out, void (*onUploaded)());
// GET /api/voice/requests/<id>/
void apiGetStatus(const char *request_id, ApiResult &out);
// POST /api/voice/agenda/
void apiPostAgendaSummary(const char *request_id, const char *language, ApiResult &out);
// GET audio_url. On success *data is a PSRAM buffer the caller must free().
bool apiDownloadAudio(const String &path, uint8_t **data, size_t *len, String &error);
