#pragma once
#include <Arduino.h>
#include "BleScanner.h"

bool   openLogFile();
void   closeLogFile();
void   appendWigleRow(const String& mac, const String& ssid, const String& auth,
                      const String& firstSeen, int channel, int rssi,
                      double lat, double lon, double altM, double accM);
bool   appendBleRow(const BleObservation& obs, const String& firstSeen,
                    double lat, double lon, double altM, double accM);

String normalizeSdPath(const char* dir, const char* nameIn);
String pathBasename(const String& p);
bool   isAllowedDataPath(const String& p);
bool   moveToUploaded(const String& srcPath);
// Collection-only overload. The caller must first verify and durably retain the
// signed receipt. It preserves collisions and retries instead of unsafe copying.
bool   moveToUploaded(const String& srcPath, const String& expectedSha256,
                       uint32_t expectedSize);
void   enforceUploadedCsvRetention();

bool collectionMatchesFile(const String& path, const String& expectedSha256, uint32_t expectedSize);
