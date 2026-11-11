#pragma once
#include <Arduino.h>

void startAP();
bool connectSTA(uint32_t timeoutMs);
void stopAPIfAllowed();
void handleStaTransitions();
bool shouldPauseScanning();

// UConsole-only home-STA WebUI uses the existing 60 s / Stay 5 min controls.
// A real fallback AP continues to use its original lifecycle.
void startUConsoleStaWebWindow();
bool uConsoleStaWebWindowActive();
