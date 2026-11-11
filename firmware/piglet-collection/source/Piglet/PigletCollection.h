#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

bool collectionUConsoleMode();
bool collectionBusy();
// One boot-only discovery attempt, followed by a resumable batch if paired.
// Called before opening the new scan log. No existing scan settings are changed.
void collectionBoot();
void collectionAddStatus(JsonObject object);

// Local proof helpers; never create a remote service upload receipt.
bool collectionReceiptKnown(const String& digest, uint32_t size, const String& collectorId);
String collectionDigestFile(const String& path);
