#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <M5Unified.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include <esp_heap_caps.h>
#include <esp_system.h>  // esp_fill_random (IDF 4.4)

#include <algorithm>

#include "api_client.h"
#include "config.h"

namespace {

constexpr const char *BOUNDARY = "----StickAssistantBoundary7MA4YWxkTrZu0gW";

// Owns the transport for one request: TLS with CA verification for https://,
// plain TCP only when ALLOW_INSECURE_HTTP_DEV is defined (enforced in config.h).
struct Transport {
  WiFiClientSecure secure;
  WiFiClient plain;

  WiFiClient &get() {
    if (BACKEND_IS_HTTPS) {
      secure.setCACert(BACKEND_ROOT_CA);
      secure.setHandshakeTimeout(HTTP_CONNECT_TIMEOUT_MS / 1000);
      return secure;
    }
    return plain;
  }
};

String urlFor(const String &path) {
  String base(BACKEND_BASE_URL);
  while (base.endsWith("/")) base.remove(base.length() - 1);
  return base + path;
}

bool beginRequest(HTTPClient &http, Transport &transport, const String &path) {
  http.setReuse(false);
  http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(HTTP_RESPONSE_TIMEOUT_MS);
  if (!http.begin(transport.get(), urlFor(path))) {
    return false;
  }
  http.addHeader("Authorization", String("Bearer ") + DEVICE_API_TOKEN);
  http.setUserAgent("StickAssistant/2.0 (M5StickS3)");
  return true;
}

// Stream sink that refuses to grow beyond a fixed size (bounded response bodies).
class BoundedSink : public Stream {
 public:
  explicit BoundedSink(size_t max) : max_(max) { body_.reserve(512); }
  size_t write(uint8_t c) override {
    if (body_.length() >= max_) {
      overflow_ = true;
      return 0;
    }
    body_ += static_cast<char>(c);
    return 1;
  }
  size_t write(const uint8_t *buf, size_t n) override {
    size_t written = 0;
    while (written < n && write(buf[written])) ++written;
    return written;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
  const String &body() const { return body_; }
  bool overflow() const { return overflow_; }

 private:
  size_t max_;
  String body_;
  bool overflow_ = false;
};

void parseResult(HTTPClient &http, int code, ApiResult &out) {
  out.http_status = code;
  if (code <= 0) {
    out.error = HTTPClient::errorToString(code);
    return;
  }
  BoundedSink sink(MAX_JSON_RESPONSE_BYTES);
  http.writeToStream(&sink);
  if (sink.overflow()) {
    out.error = "response_too_large";
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, sink.body()) != DeserializationError::Ok) {
    out.error = "invalid_json";
    return;
  }
  out.status = doc["status"] | "";
  out.transcript = doc["transcript"] | "";
  out.reply = doc["reply"] | "";
  out.audio_url = doc["audio_url"] | "";
  out.retryable = doc["retryable"] | false;
  // Voice endpoints use "error" for a code; generic API errors use "error" for text.
  out.error = doc["error"] | "";
}

void writeLE16(uint8_t *p, uint16_t v) {
  p[0] = v & 0xFF;
  p[1] = v >> 8;
}

void writeLE32(uint8_t *p, uint32_t v) {
  for (int i = 0; i < 4; ++i) p[i] = (v >> (8 * i)) & 0xFF;
}

// Canonical 44-byte header for mono 16-bit PCM at the real recording rate.
void buildWavHeader(uint8_t header[44], uint32_t data_bytes, uint32_t rate) {
  memcpy(header, "RIFF", 4);
  writeLE32(header + 4, 36 + data_bytes);
  memcpy(header + 8, "WAVEfmt ", 8);
  writeLE32(header + 16, 16);          // fmt chunk size
  writeLE16(header + 20, 1);           // PCM
  writeLE16(header + 22, 1);           // mono
  writeLE32(header + 24, rate);        // sample rate
  writeLE32(header + 28, rate * 2);    // byte rate
  writeLE16(header + 32, 2);           // block align
  writeLE16(header + 34, 16);          // bits per sample
  memcpy(header + 36, "data", 4);
  writeLE32(header + 40, data_bytes);
}

// Presents [preamble][WAV header][PCM in PSRAM][epilogue] as one Stream, so the
// upload never duplicates the recording in memory.
class MultipartBody : public Stream {
 public:
  MultipartBody(const String &preamble, const uint8_t *wav_header, const uint8_t *pcm,
                size_t pcm_len, const String &epilogue, void (*on_done)())
      : on_done_(on_done) {
    parts_[0] = {reinterpret_cast<const uint8_t *>(preamble.c_str()), preamble.length()};
    parts_[1] = {wav_header, 44};
    parts_[2] = {pcm, pcm_len};
    parts_[3] = {reinterpret_cast<const uint8_t *>(epilogue.c_str()), epilogue.length()};
    for (const auto &p : parts_) remaining_ += p.len;
  }

  size_t total() const {
    size_t t = 0;
    for (const auto &p : parts_) t += p.len;
    return t;
  }
  int available() override {
    return remaining_ > 0x7FFFFFFF ? 0x7FFFFFFF : static_cast<int>(remaining_);
  }
  int read() override {
    uint8_t c;
    return readBytes(reinterpret_cast<char *>(&c), 1) == 1 ? c : -1;
  }
  int peek() override {
    skipEmpty();
    return idx_ < 4 ? parts_[idx_].data[off_] : -1;
  }
  size_t readBytes(char *buffer, size_t length) {
    size_t copied = 0;
    while (copied < length) {
      skipEmpty();
      if (idx_ >= 4) break;
      const size_t n = std::min(length - copied, parts_[idx_].len - off_);
      memcpy(buffer + copied, parts_[idx_].data + off_, n);
      copied += n;
      off_ += n;
      remaining_ -= n;
    }
    if (remaining_ == 0 && on_done_ != nullptr) {
      auto cb = on_done_;
      on_done_ = nullptr;
      cb();
    }
    return copied;
  }
  size_t write(uint8_t) override { return 0; }
  void flush() override {}

 private:
  struct Part {
    const uint8_t *data;
    size_t len;
  };
  void skipEmpty() {
    while (idx_ < 4 && off_ >= parts_[idx_].len) {
      ++idx_;
      off_ = 0;
    }
  }
  Part parts_[4];
  size_t idx_ = 0, off_ = 0, remaining_ = 0;
  void (*on_done_)();
};

}  // namespace

void makeRequestId(char out[37]) {
  uint8_t b[16];
  esp_fill_random(b, sizeof(b));
  b[6] = (b[6] & 0x0F) | 0x40;  // version 4
  b[8] = (b[8] & 0x3F) | 0x80;  // RFC 4122 variant
  snprintf(out, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12],
           b[13], b[14], b[15]);
}

void apiPostVoice(const char *request_id, const int16_t *pcm, size_t samples,
                  uint32_t sample_rate, ApiResult &out, void (*onUploaded)()) {
  out = ApiResult();
  const size_t pcm_bytes = samples * sizeof(int16_t);
  uint8_t header[44];
  buildWavHeader(header, pcm_bytes, sample_rate);

  String preamble;
  preamble.reserve(320);
  preamble += "--"; preamble += BOUNDARY; preamble += "\r\n";
  preamble += "Content-Disposition: form-data; name=\"request_id\"\r\n\r\n";
  preamble += request_id; preamble += "\r\n";
  preamble += "--"; preamble += BOUNDARY; preamble += "\r\n";
  preamble += "Content-Disposition: form-data; name=\"audio\"; filename=\"recording.wav\"\r\n";
  preamble += "Content-Type: audio/wav\r\n\r\n";
  String epilogue = String("\r\n--") + BOUNDARY + "--\r\n";

  MultipartBody body(preamble, header, reinterpret_cast<const uint8_t *>(pcm), pcm_bytes,
                     epilogue, onUploaded);
  HTTPClient http;
  Transport transport;
  if (!beginRequest(http, transport, "/api/voice/")) {
    out.http_status = -1;
    out.error = "invalid_url";
    return;
  }
  http.addHeader("Content-Type", String("multipart/form-data; boundary=") + BOUNDARY);
  const int code = http.sendRequest("POST", &body, body.total());
  parseResult(http, code, out);
  http.end();
}

void apiGetStatus(const char *request_id, ApiResult &out) {
  out = ApiResult();
  HTTPClient http;
  Transport transport;
  if (!beginRequest(http, transport, String("/api/voice/requests/") + request_id + "/")) {
    out.http_status = -1;
    return;
  }
  parseResult(http, http.GET(), out);
  http.end();
}

void apiPostAgendaSummary(const char *request_id, const char *language, ApiResult &out) {
  out = ApiResult();
  HTTPClient http;
  Transport transport;
  if (!beginRequest(http, transport, "/api/voice/agenda/")) {
    out.http_status = -1;
    return;
  }
  http.addHeader("Content-Type", "application/json");
  char payload[96];
  snprintf(payload, sizeof(payload), "{\"request_id\":\"%s\",\"language\":\"%s\"}", request_id,
           language);
  parseResult(http, http.POST(reinterpret_cast<uint8_t *>(payload), strlen(payload)), out);
  http.end();
}

bool apiDownloadAudio(const String &path, uint8_t **data, size_t *len, String &error) {
  *data = nullptr;
  *len = 0;
  if (!path.startsWith("/api/voice/audio/")) {
    error = "unexpected audio url";  // only ever fetch from our own backend
    return false;
  }
  HTTPClient http;
  Transport transport;
  if (!beginRequest(http, transport, path)) {
    error = "invalid url";
    return false;
  }
  const int code = http.GET();
  if (code != HTTP_CODE_OK) {
    error = code > 0 ? String("HTTP ") + code : HTTPClient::errorToString(code);
    http.end();
    return false;
  }
  const int size = http.getSize();
  if (size <= 0 || static_cast<size_t>(size) > MAX_REPLY_AUDIO_BYTES) {
    error = size <= 0 ? "missing Content-Length" : "audio too large";
    http.end();
    return false;
  }
  auto *buffer = static_cast<uint8_t *>(
      heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (buffer == nullptr) {
    error = "out of memory";
    http.end();
    return false;
  }
  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0;
  const uint32_t deadline = millis() + HTTP_DOWNLOAD_TIMEOUT_MS;
  while (got < static_cast<size_t>(size) && static_cast<int32_t>(millis() - deadline) < 0) {
    const int avail = stream->available();
    if (avail > 0) {
      const size_t want = std::min(static_cast<size_t>(avail), static_cast<size_t>(size) - got);
      got += stream->readBytes(buffer + got, want);
    } else if (!http.connected()) {
      break;
    } else {
      M5.delay(1);
    }
  }
  http.end();
  if (got != static_cast<size_t>(size)) {
    free(buffer);
    error = "download incomplete";
    return false;
  }
  *data = buffer;
  *len = got;
  return true;
}
