#include <M5Unified.h>
#include <WiFi.h>

#include "config.h"
#include "wifi_manager.h"

namespace {
uint32_t g_last_attempt = 0;
}

void wifiBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);  // credentials come from secrets.h, not NVS
}

bool wifiConnected() { return WiFi.status() == WL_CONNECTED; }

int wifiRssi() { return wifiConnected() ? WiFi.RSSI() : 0; }

bool wifiConnect(uint32_t timeout_ms) {
  if (wifiConnected()) {
    return true;
  }
  g_last_attempt = millis();
  Serial.printf("[wifi] connecting to \"%s\"\n", WIFI_SSID);
  WiFi.disconnect(false, false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  const uint32_t start = millis();
  while (!wifiConnected() && millis() - start < timeout_ms) {
    M5.delay(100);
    M5.update();
  }
  if (wifiConnected()) {
    Serial.printf("[wifi] connected, IP %s, RSSI %d dBm\n", WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
    return true;
  }
  Serial.printf("[wifi] connection failed (status %d)\n", (int)WiFi.status());
  return false;
}

void wifiMaintain() {
  if (wifiConnected()) {
    return;
  }
  // The driver auto-reconnects; additionally retry explicitly, but never too often.
  if (millis() - g_last_attempt >= WIFI_RETRY_INTERVAL_MS) {
    g_last_attempt = millis();
    Serial.println("[wifi] disconnected; retrying");
    WiFi.disconnect(false, false);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}
