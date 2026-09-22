// Copy this file to include/secrets.h and fill in real values.
// include/secrets.h is gitignored and must never be committed.
#pragma once

// Wi-Fi (2.4 GHz only; the ESP32-S3 has no 5 GHz radio).
#define WIFI_SSID "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// Django backend. Never use "localhost": that is the device itself.
// Production: an https:// URL plus the server's root CA below.
#define BACKEND_BASE_URL "https://assistant.example.com"

// LAN development only: to use plain http:// (e.g. "http://192.168.1.50:8000"),
// uncomment the next line. Traffic, including the device token, is NOT encrypted.
// #define ALLOW_INSECURE_HTTP_DEV 1

// Same value as DEVICE_API_TOKEN in backend/.env.
#define DEVICE_API_TOKEN "change-me-to-the-backend-device-token"

// PEM root certificate that signed the backend's HTTPS certificate
// (e.g. ISRG Root X1 for Let's Encrypt). Required for https:// URLs;
// certificate verification is never disabled.
#define BACKEND_ROOT_CA \
  "-----BEGIN CERTIFICATE-----\n" \
  "REPLACE_WITH_ROOT_CA_PEM\n" \
  "-----END CERTIFICATE-----\n"
