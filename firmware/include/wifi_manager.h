#pragma once

#include <stdint.h>

void wifiBegin();
// Blocking connection attempt with timeout. Never reboots on failure.
bool wifiConnect(uint32_t timeout_ms);
// Non-blocking: retries periodically while disconnected.
void wifiMaintain();
bool wifiConnected();
int wifiRssi();
