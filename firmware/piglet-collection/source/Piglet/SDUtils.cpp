#include "SDUtils.h"
#include "BleCsv.h"
#include "Globals.h"
#include "Config.h"
#include "CollectionHandover.h"
#include "CollectionArchive.h"
#include "PigletCollection.h"
#include <mbedtls/md.h>
#include <algorithm>
#include <time.h>
#include <vector>

// ---- Path helpers ----

String pathBasename(const String& p) {
  int slash = p.lastIndexOf('/');
  if (slash < 0) return p;
  return p.substring(slash + 1);
}

String normalizeSdPath(const char* dir, const char* nameIn) {
  if (!dir || !nameIn) return "";

  String d(dir);
  String n(nameIn);

  d.trim();
  n.trim();

  if (d.length() == 0 || n.length() == 0) return "";

  // Ensure dir starts with "/"
  if (d[0] != '/') d = "/" + d;

  // Strip trailing "/" from dir
  while (d.endsWith("/")) d.remove(d.length() - 1);

  // Case A: name is already absolute: "/logs/foo.csv" or "/uploaded/foo.csv"
  if (n[0] == '/') {
    // If SD lib already gives full path, just return it
    return n;
  }

  // Case B: name is "logs/foo.csv" (no leading slash)
  // If it starts with the same directory name, convert to absolute.
  // Example: dir="/logs", name="logs/foo.csv" => "/logs/foo.csv"
  String dNoSlash = d;
  if (dNoSlash.startsWith("/")) dNoSlash = dNoSlash.substring(1); // "logs"

  if (n.startsWith(dNoSlash + "/")) {
    return "/" + n;  // make it absolute
  }

  // Case C: name is just "foo.csv"
  // Join dir + "/" + name
  return d + "/" + n;
}

bool isAllowedDataPath(const String& p) {
  return p.startsWith("/logs/") || p.startsWith("/uploaded/");
}

// ---- Uploaded CSV retention ----

struct UploadedCsvCandidate {
  String path;
  time_t lastWrite;
};

static bool isUploadedCsvRetentionCandidate(const String& path) {
  if (!path.startsWith("/uploaded/")) return false;
  if (!path.endsWith(".csv")) return false;
  if (currentCsvPath.length() > 0 && path == currentCsvPath) return false;
  if (pathBasename(path) == "battery_test.csv") return false;
  if (!collectionArchiveRetentionEligible(path)) return false;
  return true;
}

void enforceUploadedCsvRetention() {
  if (!sdOk || !cfg.autoDeleteUploadedLogs) return;

  uint16_t keep = cfg.uploadedLogsToKeep;
  if (keep < UPLOADED_LOGS_TO_KEEP_MIN || keep > UPLOADED_LOGS_TO_KEEP_MAX) {
    keep = UPLOADED_LOGS_TO_KEEP_DEFAULT;
  }

  File root = SD.open("/uploaded");
  if (!root || !root.isDirectory()) {
    if (root) root.close();
    return;
  }

  std::vector<UploadedCsvCandidate> candidates;
  File file = root.openNextFile();
  while (file) {
    String path = normalizeSdPath("/uploaded", file.name());
    bool eligible = !file.isDirectory() && isUploadedCsvRetentionCandidate(path);
    time_t lastWrite = eligible ? file.getLastWrite() : 0;
    file.close();

    if (eligible) {
      UploadedCsvCandidate candidate = {path, lastWrite};
      candidates.push_back(candidate);
    }

    file = root.openNextFile();
  }
  root.close();

  if (candidates.size() <= keep) return;

  std::sort(candidates.begin(), candidates.end(),
            [](const UploadedCsvCandidate& a, const UploadedCsvCandidate& b) {
              if (a.lastWrite != b.lastWrite) return a.lastWrite < b.lastWrite;
              return a.path < b.path;
            });

  size_t deleteTarget = candidates.size() - keep;
  size_t deleted = 0;

  Serial.printf("[RETENTION] enabled keep=%u uploaded=%u\n",
                (unsigned)keep, (unsigned)candidates.size());

  for (size_t i = 0; i < deleteTarget; ++i) {
    const String& path = candidates[i].path;
    if (!isUploadedCsvRetentionCandidate(path)) continue;
    if (!collectionArchiveCanDelete(path)) continue;

    Serial.print("[RETENTION] deleting: ");
    Serial.println(pathBasename(path));

    if (SD.remove(path)) {
      ++deleted;
    } else {
      Serial.print("[RETENTION] delete failed: ");
      Serial.println(pathBasename(path));
    }
  }

  Serial.printf("[RETENTION] done kept=%u deleted=%u\n",
                (unsigned)(candidates.size() - deleted), (unsigned)deleted);
}

// ---- Move to uploaded ----

bool moveToUploaded(const String& srcPath) {
  if (collectionUConsoleMode()) return collectionArchiveLegacyMove(srcPath);
  if (!sdOk) return false;
  if (!SD.exists(srcPath)) {
    Serial.print("[SD] moveToUploaded: source missing: ");
    Serial.println(srcPath);
    return false;
  }

  // Ensure folder exists
  if (!SD.exists("/uploaded")) {
    Serial.println("[SD] Creating /uploaded ...");
    if (!SD.mkdir("/uploaded")) {
      Serial.println("[SD] ERROR: SD.mkdir(/uploaded) failed");
      return false;
    }
  }

  String dstPath = String("/uploaded/") + pathBasename(srcPath);

  // If destination exists, remove it first (rename may fail otherwise)
  if (SD.exists(dstPath)) {
    Serial.print("[SD] Removing existing dst: ");
    Serial.println(dstPath);
    SD.remove(dstPath);
  }

  Serial.print("[SD] Moving ");
  Serial.print(srcPath);
  Serial.print(" -> ");
  Serial.println(dstPath);

  bool ok = SD.rename(srcPath, dstPath);
  if (!ok) {
    Serial.println("[SD] ERROR: SD.rename failed");
    // Last resort: copy + delete (some SD libs are picky)
    File in = SD.open(srcPath, FILE_READ);
    if (!in) { Serial.println("[SD] copy fallback: open src failed"); return false; }

    File out = SD.open(dstPath, FILE_WRITE);
    if (!out) { Serial.println("[SD] copy fallback: open dst failed"); in.close(); return false; }

    uint8_t buf[1024];
    while (true) {
      int n = in.read(buf, sizeof(buf));
      if (n <= 0) break;
      out.write(buf, n);
      delay(0);
    }
    out.flush();
    out.close();
    in.close();

    // Verify copy
    if (!SD.exists(dstPath)) {
      Serial.println("[SD] copy fallback: dst does not exist after write");
      return false;
    }

    if (!SD.remove(srcPath)) {
      Serial.println("[SD] copy fallback: WARNING failed to remove src after copy");
      // still consider it moved-ish, but warn
    }

    Serial.println("[SD] copy fallback: OK");
    enforceUploadedCsvRetention();
    return true;
  }

  Serial.println("[SD] Move OK");
  enforceUploadedCsvRetention();
  return true;
}

// ---- Collection receipt handover (legacy mover above stays unchanged) ----

static PigletCollection::FileProof collectionFileProof(const String& path,
                                                       const String& expected,
                                                       uint32_t expectedSize) {
  using PigletCollection::FileProof;
  if (!SD.exists(path)) return FileProof::Missing;
  File file = SD.open(path, FILE_READ);
  if (!file) return FileProof::Unreadable;
  if (file.isDirectory() || file.size() != expectedSize) {
    file.close(); return FileProof::Different;
  }
  mbedtls_md_context_t context;
  mbedtls_md_init(&context);
  const auto* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  bool ok = info && mbedtls_md_setup(&context, info, 0) == 0 &&
            mbedtls_md_starts(&context) == 0;
  uint8_t buffer[1024];
  uint32_t total = 0;
  while (ok && file.available()) {
    const int count = file.read(buffer, sizeof(buffer));
    ok = count > 0 && mbedtls_md_update(&context, buffer, count) == 0;
    if (count > 0) total += count;
    while (GPSSerial.available()) gps.encode(GPSSerial.read());
    yield();
  }
  unsigned char digest[32];
  ok = ok && total == expectedSize && mbedtls_md_finish(&context, digest) == 0;
  mbedtls_md_free(&context);
  file.close();
  if (!ok) return FileProof::Unreadable;
  static const char hex[] = "0123456789abcdef";
  char actual[65];
  for (size_t i = 0; i < sizeof(digest); ++i) {
    actual[2 * i] = hex[digest[i] >> 4];
    actual[2 * i + 1] = hex[digest[i] & 15];
  }
  actual[64] = '\0';
  return expected == actual ? FileProof::Matches : FileProof::Different;
}

bool moveToUploaded(const String& srcPath, const String& expectedSha256,
                     uint32_t expectedSize) {
  using namespace PigletCollection;
  if (!sdOk || !lowerHex(expectedSha256.c_str(), 64)) return false;
  const String basename = pathBasename(srcPath);
  String lowerName = basename; lowerName.toLowerCase();
  if (!lowerName.endsWith(".csv") || lowerName == ".csv" ||
      (srcPath != "/logs/" + basename && srcPath != "/uploaded/" + basename))
    return false;
  const FileProof source = collectionFileProof(srcPath, expectedSha256, expectedSize);
  if (srcPath.startsWith("/uploaded/")) {
    if (source != FileProof::Matches ||
        !collectionArchiveMoved(srcPath, srcPath, expectedSha256, expectedSize)) return false;
    enforceUploadedCsvRetention();
    return true;
  }
  if (!SD.exists("/uploaded") && !SD.mkdir("/uploaded")) return false;
  String destination = "/uploaded/" + basename;
  for (unsigned attempt = 0; attempt <= 32; ++attempt) {
    const FileProof target = collectionFileProof(destination, expectedSha256, expectedSize);
    const HandoverAction action = planHandover(source, target);
    if (action == HandoverAction::Complete) {
      if (!collectionArchiveMoved(srcPath, destination, expectedSha256, expectedSize)) return false;
      enforceUploadedCsvRetention();
      return true;
    }
    if (action == HandoverAction::Retry) return false;
    if (action == HandoverAction::RemoveDuplicate) {
      // Both byte streams match the signed, durably acknowledged digest.
      if (!collectionArchivePlanMove(srcPath, destination, expectedSha256, expectedSize) ||
          !SD.remove(srcPath) ||
          !collectionArchiveMoved(srcPath, destination, expectedSha256, expectedSize)) return false;
      enforceUploadedCsvRetention();
      return true;
    }
    if (action == HandoverAction::Rename) {
      // Never remove an existing destination. No copy/delete fallback: an SD
      // failure leaves the source and receipt available for the next attempt.
      if (SD.exists(destination) ||
          !collectionArchivePlanMove(srcPath, destination, expectedSha256, expectedSize) ||
          !SD.rename(srcPath, destination)) return false;
      if (collectionFileProof(destination, expectedSha256, expectedSize) != FileProof::Matches) {
        if (!SD.exists(srcPath)) SD.rename(destination, srcPath);
        return false;
      }
      if (!collectionArchiveMoved(srcPath, destination, expectedSha256, expectedSize)) return false;
      enforceUploadedCsvRetention();
      return true;
    }
    char uniqueName[96];
    if (!handoverCollisionName(uniqueName, sizeof(uniqueName), expectedSha256.c_str(), attempt))
      return false;
    destination = "/uploaded/" + String(uniqueName);
  }
  return false;
}

bool collectionMatchesFile(const String& path, const String& expectedSha256,
                            uint32_t expectedSize) {
  return collectionFileProof(path, expectedSha256, expectedSize) == PigletCollection::FileProof::Matches;
}

// ---- Log file ----

// Row limit per CSV file: ~120 bytes/row × 100k = ~12 MB, safely under the
// WDGoWars 15 MB upload cap. When the limit is reached, the active CSV is
// closed and a fresh one is opened with proper WiGLE headers.
static const uint32_t CSV_MAX_ROWS = 100000;
static uint32_t       csvRowCount  = 0;

// Sanitise a user-provided device name for safe use in filenames.
// Keeps alphanumerics, hyphens, underscores; replaces spaces with _;
// strips everything else; truncates to 20 chars.
static String sanitiseDeviceName(const String& raw) {
  String s = raw;
  s.replace(" ", "_");
  for (int i = (int)s.length() - 1; i >= 0; i--) {
    char c = s[i];
    if (!isAlphaNumeric(c) && c != '_' && c != '-') s.remove(i, 1);
  }
  if (s.length() > 20) s = s.substring(0, 20);
  return s;
}

static bool uConsoleLogSpaceReady() {
  if (!collectionUConsoleMode()) return true;
  const uint64_t total = SD.totalBytes(), used = SD.usedBytes();
  if (total && (used >= total || total - used < 1024)) {
    Serial.println("[SD] UConsole storage full: pending CSV retained; no new log can be created");
    return false;
  }
  return true;
}

static bool uConsoleLogWriteComplete(size_t written, size_t expected) {
  if (!collectionUConsoleMode() || written == expected) return true;
  Serial.println("[SD] UConsole storage/write failure: pending CSV retained; logging stopped");
  closeLogFile();
  sdOk = false;
  return false;
}

static String newCsvFilename() {
  if (!SD.exists("/logs")) SD.mkdir("/logs");

  // Build optional prefix:  "name_Piglet_"  or empty
  String prefix = "";
  if (cfg.deviceName.length() > 0) {
    String safe = sanitiseDeviceName(cfg.deviceName);
    if (safe.length() > 0) prefix = safe + "_Piglet_";
  }

  // Make collisions extremely unlikely: millis + esp_random
  for (int tries = 0; tries < 25; tries++) {
    uint32_t r = (uint32_t)esp_random();
    char buf[100];
    snprintf(buf, sizeof(buf), "/logs/%sWiGLE_%lu_%08lX.csv",
             prefix.c_str(), (unsigned long)millis(), (unsigned long)r);
    String p(buf);
    if (!SD.exists(p)) return p;
  }

  // last-resort fallback
  char buf2[100];
  snprintf(buf2, sizeof(buf2), "/logs/%sWiGLE_%lu.csv",
           prefix.c_str(), (unsigned long)millis());
  if (collectionUConsoleMode() && SD.exists(buf2)) {
    Serial.println("[SD] UConsole filename collision: existing CSV retained; no log overwritten");
    return "";
  }
  return String(buf2);
}

bool openLogFile() {
  if (!sdOk) return false;

  // Close any previous handle
  closeLogFile();
  if (!uConsoleLogSpaceReady()) {
    currentCsvPath = "";
    sdOk = false;
    return false;
  }

  // Pick a fresh filename FIRST
  currentCsvPath = newCsvFilename();
  if (collectionUConsoleMode() && currentCsvPath.length() == 0) {
    sdOk = false;
    return false;
  }

  Serial.print("[SD] Opening log file: ");
  Serial.println(currentCsvPath);

  logFile = SD.open(currentCsvPath, FILE_WRITE);
  if (!logFile) {
    Serial.println("[SD] Failed to open log file for write");
    return false;
  }

  // Build device field: Piglet-{name} if set, otherwise Piglet-Wardriver
  String deviceField = "Piglet-Wardriver";
  if (cfg.deviceName.length() > 0) {
    String safe = sanitiseDeviceName(cfg.deviceName);
    if (safe.length() > 0) deviceField = "Piglet-" + safe;
  }

  // Derive board model from the ACTIVE pin-map name (set by detectPinsByChip /
  // pickPinsFromConfig at boot). This reflects the real chip even when
  // cfg.board="auto", avoiding the old fallback that always wrote "Xiao-ESP32S3".
  String pn = String(pins.name); pn.toUpperCase();
  String boardModel;
  if      (pn.indexOf("EXP") >= 0) boardModel = "Xiao-ESP32S3-Exp";
  else if (pn.indexOf("C5")  >= 0) boardModel = "Xiao-ESP32C5";
  else if (pn.indexOf("C6")  >= 0) boardModel = "Xiao-ESP32C6";
  else                              boardModel = "Xiao-ESP32S3";

  // WiGLE WiFi 1.6 header
  logFile.print("WigleWifi-1.6,appRelease=");
  logFile.print(FIRMWARE_VERSION);
  logFile.print(",model="); logFile.print(boardModel);
  logFile.print(",release=1,device="); logFile.print(deviceField);
  logFile.print(",display=SSD1306-128x64,board="); logFile.print(boardModel);
  logFile.println(",brand=Piglet,star=Sol,body=3,subBody=0");
  logFile.println("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type");
  logFile.flush();
  csvRowCount = 0;

  Serial.println("[SD] Log file initialized with WiGLE headers");
  return true;
}

void closeLogFile() {
  if (logFile) {
    Serial.println("[SD] Closing log file");
    logFile.flush();
    logFile.close();
  }
}

void appendWigleRow(const String& mac, const String& ssid, const String& auth,
                    const String& firstSeen, int channel, int rssi,
                    double lat, double lon, double altM, double accM) {
  if (!sdOk || !logFile) return;

  // Rotate CSV before it exceeds the WDGoWars 15 MB upload limit
  if (csvRowCount >= CSV_MAX_ROWS) {
    Serial.println("[SD] CSV row limit reached, rotating log file");
    closeLogFile();
    if (!openLogFile()) return;
  }

  String safeSsid = ssid;
  safeSsid.replace("\"", "\"\"");

  String line;
  line.reserve(256);
  line += mac; line += ",";
  line += "\""; line += safeSsid; line += "\",";
  line += auth; line += ",";
  line += firstSeen; line += ",";
  line += String(channel); line += ",";
  // Frequency in MHz derived from channel number (WiGLE 1.6 requirement)
  uint32_t freq = 0;
  if      (channel >= 1  && channel <= 13) freq = 2407u + (uint32_t)channel * 5;
  else if (channel == 14)                  freq = 2484u;
  else if (channel >= 32)                  freq = 5000u + (uint32_t)channel * 5;
  line += String(freq); line += ",";
  line += String(rssi); line += ",";
  line += String(lat, 6); line += ",";
  line += String(lon, 6); line += ",";
  line += String(altM, 1); line += ",";
  line += String(accM, 1); line += ",";
  line += ",,WIFI"; // RCOIs (empty), MfgrId (empty), Type

  size_t written = logFile.println(line);
  csvRowCount++;
  if (!uConsoleLogWriteComplete(written, line.length() + 2)) return;

  // Detect silent write failure — if println() returns 0 for a non-empty line,
  // the SD card or file handle is broken. Attempt to reopen the log file once;
  // if that also fails, mark SD as unusable until next boot.
  if (written == 0 && line.length() > 0) {
    static uint8_t consecFails = 0;
    consecFails++;
    Serial.printf("[SD] Write failed (%u consecutive)\n", consecFails);
    if (consecFails >= 3) {
      Serial.println("[SD] Attempting log reopen...");
      closeLogFile();
      if (openLogFile()) {
        Serial.println("[SD] Reopen OK — retrying write");
        logFile.println(line);  // best-effort retry
        consecFails = 0;
      } else {
        Serial.println("[SD] Reopen FAILED — SD marked unusable");
        sdOk = false;
      }
    }
    return;
  }

  // Flush less often to avoid stalls (SD writes can block hard)
  static uint32_t lastFlushMs = 0;
  static uint32_t linesSinceFlush = 0;

  linesSinceFlush++;

  uint32_t nowMs = millis();
  if (linesSinceFlush >= 25 || (nowMs - lastFlushMs) >= 2000) {
    logFile.flush();
    lastFlushMs = nowMs;
    linesSinceFlush = 0;
  }
}

bool appendBleRow(const BleObservation& obs, const String& firstSeen,
                  double lat, double lon, double altM, double accM) {
  if (!sdOk || !logFile) return false;

  if (csvRowCount >= CSV_MAX_ROWS) {
    Serial.println("[SD] CSV row limit reached, rotating log file");
    closeLogFile();
    if (!openLogFile()) return false;
  }

  String line = formatBleWigleRow(obs.addr, obs.addrType, obs.name, firstSeen,
                                  obs.channel, obs.rssi,
                                  lat, lon, altM, accM,
                                  obs.serviceUuids, obs.mfgrId);

  size_t written = logFile.println(line);
  csvRowCount++;
  if (!uConsoleLogWriteComplete(written, line.length() + 2)) return false;

  if (written == 0 && line.length() > 0) {
    static uint8_t consecFails = 0;
    consecFails++;
    Serial.printf("[SD] BLE write failed (%u consecutive)\n", consecFails);
    if (consecFails >= 3) {
      Serial.println("[SD] Attempting log reopen...");
      closeLogFile();
      if (openLogFile()) {
        Serial.println("[SD] Reopen OK - retrying BLE write");
        size_t retryWritten = logFile.println(line);
        if (retryWritten > 0) {
          consecFails = 0;
          return true;
        }
      } else {
        Serial.println("[SD] Reopen FAILED - SD marked unusable");
        sdOk = false;
      }
    }
    return false;
  }

  static uint32_t lastFlushMs = 0;
  static uint32_t linesSinceFlush = 0;

  linesSinceFlush++;

  uint32_t nowMs = millis();
  if (linesSinceFlush >= 25 || (nowMs - lastFlushMs) >= 2000) {
    logFile.flush();
    lastFlushMs = nowMs;
    linesSinceFlush = 0;
  }

  return true;
}
