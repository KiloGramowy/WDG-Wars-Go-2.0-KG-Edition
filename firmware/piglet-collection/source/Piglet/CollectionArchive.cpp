#include "CollectionArchive.h"
#include "CollectionReceipt.h"
#include "Globals.h"
#include "PigletCollection.h"
#include "SDUtils.h"
#include <esp_heap_caps.h>
#include <mbedtls/md.h>

namespace {
String activeNamespace, journalDirectory, statePath, currentCollector;
bool ready = false, inventoryComplete = false, migrationComplete = false;
bool bodiesVerified = false;
uint32_t historicalTotal = 0, historicalPending = 0;
uint32_t pendingAvailable = 0;

String textDigest(const String& text) {
  unsigned char hash[32];
  const auto* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!info || mbedtls_md(info, reinterpret_cast<const unsigned char*>(text.c_str()),
                         text.length(), hash) != 0) return "";
  static const char hex[] = "0123456789abcdef";
  char result[65];
  for (size_t i = 0; i < 32; ++i) {
    result[2*i] = hex[hash[i] >> 4]; result[2*i+1] = hex[hash[i] & 15];
  }
  result[64] = 0;
  return result;
}

String pairingNamespace() {
  return textDigest("PC1\n" + cfg.collectionSsid + "\n" + cfg.collectionHost + "\n" +
                    String(cfg.collectionPort) + "\n" + cfg.collectionToken);
}

bool mkdirIfNeeded(const String& path) {
  return SD.exists(path) || SD.mkdir(path);
}

bool readJson(const String& path, DynamicJsonDocument& doc) {
  File file = SD.open(path, FILE_READ);
  if (!file) return false;
  const bool ok = !file.isDirectory() && file.size() <= 4096 &&
                  !deserializeJson(doc, file) && doc.is<JsonObject>();
  file.close();
  return ok;
}

bool loadJson(const String& path, DynamicJsonDocument& doc) {
  doc.clear();
  if (readJson(path, doc)) return true;
  doc.clear();
  return readJson(path + ".bak", doc);
}

bool validItem(const String& metadata, JsonDocument& item, const String& expectedPath);

bool validPrevious(const String& path, JsonDocument& previous, const JsonDocument& next) {
  if (next.containsKey("path"))
    return validItem(path, previous, String(next["path"] | ""));
  const String id = previous["collector_id"] | "invalid";
  return previous["protocol"] == 1 && previous["inventory_complete"].is<bool>() &&
         previous["migration_complete"].is<bool>() &&
         (id.length() == 0 || PigletCollection::lowerHex(id.c_str(), 32));
}

bool writeJson(const String& path, const JsonDocument& doc) {
  const String temporary = path + ".tmp", backup = path + ".bak";
  SD.remove(temporary); // Own metadata only; never an observation CSV.
  File file = SD.open(temporary, FILE_WRITE);
  if (!file) return false;
  const bool written = serializeJson(doc, file) == measureJson(doc);
  file.flush(); file.close();
  DynamicJsonDocument check(4096);
  if (!written || !readJson(temporary, check)) return false;
  DynamicJsonDocument previous(4096);
  const bool primaryExists = SD.exists(path);
  const bool primaryValid = primaryExists && readJson(path, previous) &&
                            validPrevious(path, previous, doc);
  if (primaryValid) {
    if (SD.exists(backup) && !SD.remove(backup)) return false;
    if (!SD.rename(path, backup)) return false;
  } else if (primaryExists) {
    // Do not destroy the only usable metadata generation. A corrupt primary
    // may be replaced only while a readable backup is retained.
    previous.clear();
    if (!readJson(backup, previous) || !validPrevious(path, previous, doc) ||
        !SD.remove(path)) return false;
  }
  if (!SD.rename(temporary, path)) {
    if (!SD.exists(path) && SD.exists(backup)) SD.rename(backup, path);
    return false;
  }
  return true; // Keep one previous metadata generation for interrupted updates.
}

String itemPath(const String& path) {
  return journalDirectory + "/" + textDigest(path) + ".json";
}

bool candidatePath(const String& path) {
  const String name = pathBasename(path);
  String lower = name; lower.toLowerCase();
  return lower.endsWith(".csv") && name.length() > 4 && name.length() <= 255 &&
         path != currentCsvPath &&
         (path == "/logs/" + name || path == "/uploaded/" + name);
}

bool stateSave() {
  DynamicJsonDocument state(1024);
  state["protocol"] = 1; state["collector_id"] = currentCollector;
  state["inventory_complete"] = inventoryComplete;
  state["migration_complete"] = migrationComplete;
  return writeJson(statePath, state);
}

bool validItem(const String& metadata, JsonDocument& item, const String& expectedPath) {
  if (!item["path"].is<const char*>()) return false;
  const String path = item["path"].as<String>();
  if (!candidatePath(path) || metadata != itemPath(path) ||
      (expectedPath.length() && path != expectedPath)) return false;
  const String sha = item["sha256"] | "invalid";
  const String moved = item["moved_to"] | "invalid";
  if (item.containsKey("source_obligation") && !item["source_obligation"].is<bool>()) return false;
  if ((item["source_obligation"] | false) &&
      !PigletCollection::lowerHex(String(item["source_collector"] | "").c_str(), 32)) return false;
  return item["size"].is<uint32_t>() && item["mtime"].is<int64_t>() &&
         item["historical"].is<bool>() && item["handover_done"].is<bool>() &&
         (sha.length() == 0 || PigletCollection::lowerHex(sha.c_str(), 64)) &&
         (moved.length() == 0 || candidatePath(moved));
}

bool readItem(const String& metadata, DynamicJsonDocument& item,
               const String& expectedPath = "") {
  for (const char* suffix : {"", ".bak"}) {
    item.clear();
    if (readJson(metadata + suffix, item) && validItem(metadata, item, expectedPath)) return true;
  }
  return false;
}

bool itemLoad(const String& path, DynamicJsonDocument& item) {
  return readItem(itemPath(path), item, path);
}

bool registerPath(const String& path, bool historical) {
  File file = SD.open(path, FILE_READ);
  if (!file || file.isDirectory()) { file.close(); return false; }
  const uint32_t size = file.size();
  const int64_t modified = file.getLastWrite();
  file.close();
  DynamicJsonDocument item(2048);
  const bool hadMetadata = SD.exists(itemPath(path)) || SD.exists(itemPath(path) + ".bak");
  const bool exists = itemLoad(path, item);
  const bool unchanged = exists && item["size"] == size && item["mtime"] == modified;
  const bool wasHistorical = exists ? (item["historical"] | false) : hadMetadata;
  const bool sourceObligation = exists && (item["source_obligation"] | false);
  const String sourceCollector = exists ? String(item["source_collector"] | "") : String();
  DynamicJsonDocument primary(2048);
  if (unchanged && (!historical || wasHistorical) && readJson(itemPath(path), primary)) return true;
  if (!unchanged) {
    item.clear();
    item["sha256"] = ""; item["handover_done"] = false;
    item["moved_to"] = "";
    if (sourceObligation) {
      item["source_obligation"] = true;
      item["source_collector"] = sourceCollector;
    }
  }
  item["path"] = path; item["size"] = size; item["mtime"] = modified;
  item["historical"] = historical || wasHistorical;
  return writeJson(itemPath(path), item);
}

bool knownReceipt(JsonDocument& item) {
  const String sha = item["sha256"] | "";
  return PigletCollection::lowerHex(sha.c_str(), 64) &&
         PigletCollection::lowerHex(currentCollector.c_str(), 32) &&
         collectionReceiptKnown(sha, item["size"] | 0U, currentCollector);
}

bool enumerate(CollectionFileList* list, bool pendingOnly, bool registerItems,
                bool verifyBodies = false) {
  uint32_t count = 0;
  for (const char* directory : {"/logs", "/uploaded"}) {
    if (!SD.exists(directory)) continue;
    File root = SD.open(directory);
    if (!root || !root.isDirectory()) { root.close(); return false; }
    File file = root.openNextFile();
    while (file) {
      const String path = normalizeSdPath(directory, file.name());
      const bool eligible = !file.isDirectory() && candidatePath(path);
      file.close();
      if (eligible) {
        if (registerItems && !registerPath(path, !inventoryComplete)) {
          root.close(); return false;
        }
        bool pending = true;
        if (pendingOnly && path.startsWith("/uploaded/")) {
          DynamicJsonDocument item(2048);
          pending = !itemLoad(path, item) || !knownReceipt(item) ||
                    !(item["handover_done"] | false);
          if (!pending && verifyBodies) pending = !collectionArchiveCanDelete(path);
        } else if (pendingOnly && verifyBodies) {
          DynamicJsonDocument item(2048);
          if (itemLoad(path, item) && knownReceipt(item) && (item["handover_done"] | false))
            collectionArchiveCanDelete(path); // Invalidate changed bytes; /logs still needs handover.
        }
        if (!pendingOnly || pending) {
          if (list && list->files) {
            if (count >= list->count || path.length() >= sizeof(list->files[count].path)) {
              root.close(); return false;
            }
            memcpy(list->files[count].path, path.c_str(), path.length() + 1);
          }
          ++count;
        }
      }
      while (GPSSerial.available()) gps.encode(GPSSerial.read());
      delay(1);
      file = root.openNextFile();
    }
    root.close();
  }
  if (list && !list->files) list->count = count;
  else if (list) list->count = count;
  return true;
}

bool sourceObligations(CollectionFileList* list, uint32_t capacity, uint32_t& count) {
  count = 0;
  File root = SD.open(journalDirectory);
  if (!root || !root.isDirectory()) { root.close(); return false; }
  File file = root.openNextFile();
  while (file) {
    const String raw = normalizeSdPath(journalDirectory.c_str(), file.name());
    const bool directory = file.isDirectory(); file.close();
    const bool backup = raw.endsWith(".json.bak");
    const String canonical = backup ? raw.substring(0, raw.length() - 4) : raw;
    if (!directory && (raw.endsWith(".json") || (backup && !SD.exists(canonical)))) {
      DynamicJsonDocument item(2048);
      if (!readItem(canonical, item)) { root.close(); return false; }
      if ((item["source_obligation"] | false) && item["source_collector"] == currentCollector) {
        ++count;
        if (list) {
          const String path = item["path"] | "";
          bool found = false;
          for (uint32_t i = 0; i < list->count; ++i)
            if (path == list->files[i].path) { found = true; break; }
          if (!found) {
            if (list->count >= capacity || path.length() >= sizeof(list->files[0].path)) {
              root.close(); return false;
            }
            memcpy(list->files[list->count++].path, path.c_str(), path.length() + 1);
          }
        }
      }
    }
    while (GPSSerial.available()) gps.encode(GPSSerial.read());
    delay(1); file = root.openNextFile();
  }
  root.close(); return true;
}
} // namespace

CollectionFileList::~CollectionFileList() { free(files); }

bool collectionArchivePrepare() {
  if (!collectionUConsoleMode()) return true;
  ready = false;
  bodiesVerified = false;
  if (!sdOk) return false;
  const String identity = pairingNamespace();
  if (!PigletCollection::lowerHex(identity.c_str(), 64)) return false;
  activeNamespace = identity;
  journalDirectory = "/pc_inventory/" + identity;
  statePath = "/pc_state/" + identity + ".json";
  if (!mkdirIfNeeded("/pc_inventory") || !mkdirIfNeeded("/pc_state") ||
      !mkdirIfNeeded(journalDirectory)) return false;
  DynamicJsonDocument state(1024);
  inventoryComplete = migrationComplete = false; currentCollector = "";
  if (loadJson(statePath, state)) {
    currentCollector = state["collector_id"] | "";
    if (!PigletCollection::lowerHex(currentCollector.c_str(), 32)) currentCollector = "";
    inventoryComplete = state["inventory_complete"] | false;
    migrationComplete = state["migration_complete"] | false;
  }
  // Metadata-only inventory runs even without pairing/hotspot, before any legacy
  // cleanup/upload/retention. It does not read whole CSVs or extend discovery.
  if (!enumerate(nullptr, false, true)) return false;
  inventoryComplete = true;
  if (!stateSave()) return false;
  ready = true;
  collectionArchiveRefresh();
  return ready;
}

String collectionArchiveCollector() { return currentCollector; }

bool collectionArchiveSetCollector(const String& id) {
  if (!ready || !PigletCollection::lowerHex(id.c_str(), 32)) return false;
  if (id == currentCollector) return true;
  currentCollector = id; inventoryComplete = migrationComplete = false;
  if (!stateSave()) { ready = false; return false; }
  return collectionArchivePrepare();
}

bool collectionArchiveList(CollectionFileList& list, bool pendingOnly,
                            bool includeSourceObligations) {
  if (!ready || activeNamespace != pairingNamespace() || list.files) return false;
  // Allocate from a cheap upper bound, then perform strict filtering once.
  // No directory is kept open during the later handover/retention operations.
  if (!enumerate(&list, false, false)) return false;
  uint32_t obligations = 0;
  if (includeSourceObligations && !sourceObligations(nullptr, 0, obligations)) return false;
  if (obligations > UINT32_MAX - list.count) return false;
  list.count += obligations;
  if (!list.count) return true;
  if (list.count > SIZE_MAX / sizeof(CollectionFileList::Entry)) return false;
  const size_t bytes = list.count * sizeof(CollectionFileList::Entry);
  const uint32_t capacity = list.count;
  list.files = static_cast<CollectionFileList::Entry*>(
      heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!list.files) list.files = static_cast<CollectionFileList::Entry*>(malloc(bytes));
  if (!list.files || !enumerate(&list, pendingOnly, false, true) || !ready) return false;
  return !includeSourceObligations || sourceObligations(&list, capacity, obligations);
}

bool collectionArchiveAnnounceSources(const CollectionFileList& list) {
  if (!ready || activeNamespace != pairingNamespace() ||
      !PigletCollection::lowerHex(currentCollector.c_str(), 32)) return false;
  for (uint32_t i = 0; i < list.count; ++i) {
    const String path = list.files[i].path;
    DynamicJsonDocument item(2048);
    if (!itemLoad(path, item)) return false;
    if ((item["source_obligation"] | false) && item["source_collector"] == currentCollector) continue;
    item["source_obligation"] = true; item["source_collector"] = currentCollector;
    if (!writeJson(itemPath(path), item)) return false;
  }
  return true;
}

bool collectionArchiveFinishSources(const CollectionFileList& list) {
  if (!ready || activeNamespace != pairingNamespace()) return false;
  for (uint32_t i = 0; i < list.count; ++i) {
    const String path = list.files[i].path;
    DynamicJsonDocument item(2048);
    if (!itemLoad(path, item)) return false;
    if (item["source_collector"] != currentCollector) return false;
    item["source_obligation"] = false;
    if (!writeJson(itemPath(path), item)) return false;
  }
  return true;
}

bool collectionArchiveObserve(const String& path, const String& sha, uint32_t size) {
  if (!collectionUConsoleMode()) return true;
  if (!ready || activeNamespace != pairingNamespace() ||
      !candidatePath(path) || !PigletCollection::lowerHex(sha.c_str(), 64)) return false;
  if (!registerPath(path, false)) return false;
  DynamicJsonDocument item(2048);
  if (!itemLoad(path, item) || item["size"] != size) return false;
  item["sha256"] = sha;
  return writeJson(itemPath(path), item);
}

bool collectionArchiveMoved(const String& source, const String& destination,
                             const String& sha, uint32_t size) {
  if (!collectionUConsoleMode()) return true;
  if (!ready || activeNamespace != pairingNamespace() || !candidatePath(destination)) return false;
  DynamicJsonDocument from(2048);
  if (!itemLoad(source, from)) return false;
  // A new destination is an alias of the original inventory entry, not a second
  // historical file. Existing historical destinations keep their original flag.
  if (!registerPath(destination, false)) return false;
  DynamicJsonDocument target(2048);
  if (!itemLoad(destination, target)) return false;
  target["sha256"] = sha; target["handover_done"] = true;
  if (!writeJson(itemPath(destination), target)) return false;
  from["sha256"] = sha; from["size"] = size;
  from["moved_to"] = destination; from["handover_done"] = true;
  return source == destination || writeJson(itemPath(source), from);
}

bool collectionArchivePlanMove(const String& source, const String& destination,
                                const String& sha, uint32_t size) {
  if (!ready || activeNamespace != pairingNamespace() ||
      !candidatePath(source) || !candidatePath(destination)) return false;
  DynamicJsonDocument item(2048);
  if (!itemLoad(source, item)) return false;
  item["sha256"] = sha; item["size"] = size;
  item["moved_to"] = destination; item["handover_done"] = false;
  return writeJson(itemPath(source), item);
}

bool collectionArchiveRetentionEligible(const String& path) {
  if (!collectionUConsoleMode()) return true;
  if (path == currentCsvPath && currentCsvPath.length()) return false;
  String lower = path; lower.toLowerCase();
  const bool csv = (path.startsWith("/logs/") || path.startsWith("/uploaded/")) &&
                   lower.endsWith(".csv");
  if (!csv) return true;
  if (!candidatePath(path)) return false;
  if (!ready || activeNamespace != pairingNamespace()) return false;
  DynamicJsonDocument item(2048);
  if (!itemLoad(path, item) || !knownReceipt(item) || !(item["handover_done"] | false)) return false;
  return true;
}

bool collectionArchiveCanDelete(const String& path) {
  if (!collectionArchiveRetentionEligible(path)) return false;
  if (!collectionUConsoleMode()) return true;
  String lower = path; lower.toLowerCase();
  if (!lower.endsWith(".csv") || (!path.startsWith("/logs/") && !path.startsWith("/uploaded/"))) return true;
  DynamicJsonDocument item(2048);
  if (!itemLoad(path, item)) return false;
  const String sha = item["sha256"] | "";
  if (collectionMatchesFile(path, sha, item["size"] | 0U)) return true;
  // A changed/corrupt file must become pending again, even if size/mtime did
  // not change. Never let a receipt for old bytes authorize deleting new bytes.
  item["sha256"] = ""; item["handover_done"] = false; item["moved_to"] = "";
  migrationComplete = false;
  if (!writeJson(itemPath(path), item) || !stateSave()) ready = false;
  return false;
}

bool collectionArchiveReceived(const String& path) {
  if (!collectionUConsoleMode() || !ready || activeNamespace != pairingNamespace()) return false;
  DynamicJsonDocument item(2048);
  if (!itemLoad(path, item) || !knownReceipt(item)) return false;
  File file = SD.open(path, FILE_READ);
  if (!file || file.isDirectory()) { file.close(); return false; }
  const bool sameSnapshot = item["size"] == file.size() &&
                            item["mtime"] == static_cast<int64_t>(file.getLastWrite());
  file.close();
  // Display the signed receipt for this recorded snapshot; destructive actions
  // still require their separate strict current-byte proof.
  return sameSnapshot;
}

bool collectionArchiveDeliveredSnapshot(const String& path, String& sha, uint32_t& size) {
  if (!collectionUConsoleMode() || !ready || activeNamespace != pairingNamespace() ||
      SD.exists(path)) return false;
  DynamicJsonDocument item(2048);
  if (!itemLoad(path, item) || !knownReceipt(item)) return false;
  bool archived = item["handover_done"] | false;
  if (!archived) {
    const String moved = item["moved_to"] | "";
    DynamicJsonDocument target(2048);
    archived = moved.length() && itemLoad(moved, target) && knownReceipt(target) &&
        target["sha256"] == item["sha256"] && target["size"] == item["size"] &&
        (target["handover_done"] | false);
  }
  if (!archived) return false;
  sha = item["sha256"].as<String>();
  size = item["size"].as<uint32_t>();
  return true;
}

bool collectionArchiveLegacyMove(const String& source) {
  if (!ready || activeNamespace != pairingNamespace()) return false;
  File file = SD.open(source, FILE_READ);
  if (!file || file.isDirectory()) { file.close(); return false; }
  const uint32_t size = file.size(); file.close();
  const String sha = collectionDigestFile(source);
  if (!collectionArchiveObserve(source, sha, size)) return false;
  // This follows a real existing WiGLE/WDGW upload, not a Collection ACK.
  // The inventory remains pending and retention cannot delete it without ACK.
  return moveToUploaded(source, sha, size);
}

void collectionArchiveRecover() {
  // Resolve the reset window after rename but before destination metadata was
  // committed. The durable pre-rename intent supplies the original digest;
  // no collector connection or repeat import is needed for this local step.
  File journal = SD.open(journalDirectory);
  if (journal && journal.isDirectory()) {
    File record = journal.openNextFile();
    while (record) {
      const String raw = normalizeSdPath(journalDirectory.c_str(), record.name());
      const bool directory = record.isDirectory(); record.close();
      const bool backup = raw.endsWith(".json.bak");
      const String canonical = backup ? raw.substring(0, raw.length() - 4) : raw;
      if (!directory && (raw.endsWith(".json") || (backup && !SD.exists(canonical)))) {
        DynamicJsonDocument item(2048);
        if (readItem(canonical, item) && knownReceipt(item)) {
          const String source = item["path"] | "";
          const String destination = item["moved_to"] | "";
          const String sha = item["sha256"] | "";
          const uint32_t size = item["size"] | 0U;
          if (!SD.exists(source) && destination.length() &&
              collectionMatchesFile(destination, sha, size) &&
              collectionArchiveObserve(destination, sha, size))
            moveToUploaded(destination, sha, size);
        }
      }
      record = journal.openNextFile();
    }
  }
  journal.close();
  CollectionFileList files;
  if (!collectionArchiveList(files, false)) return;
  for (uint32_t i = 0; i < files.count; ++i) {
    const String path = files.files[i].path;
    DynamicJsonDocument item(2048);
    if (itemLoad(path, item) && knownReceipt(item) &&
        (path.startsWith("/logs/") || !(item["handover_done"] | false)))
      moveToUploaded(path, String(item["sha256"] | ""), item["size"] | 0U);
  }
  collectionArchiveRefresh(true);
}

void collectionArchiveRefresh(bool verifyBodies) {
  if (!ready) return;
  CollectionFileList pending;
  if (!enumerate(&pending, true, false, verifyBodies) || !ready) { ready = false; return; }
  pendingAvailable = pending.count;
  historicalTotal = historicalPending = 0;
  File root = SD.open(journalDirectory);
  if (!root || !root.isDirectory()) { ready = false; root.close(); return; }
  File file = root.openNextFile();
  while (file) {
    const String rawName = normalizeSdPath(journalDirectory.c_str(), file.name());
    DynamicJsonDocument item(2048);
    const bool directory = file.isDirectory();
    file.close();
    const bool backup = rawName.endsWith(".json.bak");
    const String canonical = backup ? rawName.substring(0, rawName.length() - 4) : rawName;
    const bool metadata = !directory && (rawName.endsWith(".json") ||
        (backup && !SD.exists(canonical)));
    const bool parsed = metadata && readItem(canonical, item);
    const bool historical = parsed && (item["historical"] | false);
    if (metadata && !parsed) {
      ++historicalTotal; ++historicalPending;
    }
    if (historical) {
      ++historicalTotal;
      bool done = knownReceipt(item) && (item["handover_done"] | false);
      const String original = item["path"] | "";
      if (!done && !SD.exists(original)) {
        const String moved = item["moved_to"] | "";
        const String digest = item["sha256"] | "";
        DynamicJsonDocument target(2048);
        done = knownReceipt(item) && moved.length() && itemLoad(moved, target) &&
               target["sha256"] == digest && target["size"] == item["size"] &&
               knownReceipt(target) && (target["handover_done"] | false);
      }
      if (!done) ++historicalPending;
    }
    while (GPSSerial.available()) gps.encode(GPSSerial.read());
    delay(1); file = root.openNextFile();
  }
  root.close();
  const bool complete = inventoryComplete && historicalPending == 0 &&
                        (verifyBodies || migrationComplete);
  bodiesVerified = verifyBodies;
  if (complete != migrationComplete) {
    migrationComplete = complete;
    if (!stateSave()) ready = false;
  }
}

void collectionArchiveStatus(JsonObject status) {
  status["inventory_ready"] = ready;
  status["inventory_complete"] = inventoryComplete;
  status["migration_complete"] = migrationComplete;
  status["historical_total"] = historicalTotal;
  status["historical_pending"] = historicalPending;
  status["pending_files"] = pendingAvailable;
  status["body_verification_pending"] = !bodiesVerified;
}
