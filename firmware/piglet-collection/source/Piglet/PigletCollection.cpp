#include "PigletCollection.h"
#include "CollectionReceipt.h"
#include "CollectionArchive.h"
#include "Globals.h"
#include "SDUtils.h"
#include <mbedtls/md.h>
#include <errno.h>
#include <lwip/sockets.h>
#include <esp_heap_caps.h>

namespace {
constexpr uint32_t DISCOVERY_MS = 10000;
constexpr uint32_t REQUEST_MS = 5000;
constexpr size_t CHUNK_MAX = 16384;
constexpr size_t RESPONSE_MAX = 8192;
constexpr uint32_t FILE_MAX = 2147483648UL;
constexpr unsigned RETRIES = 3;

struct Status {
  bool busy = false;
  String stage = "idle";
  String result;
  String file;
  uint32_t total = 0;
  uint32_t done = 0;
  uint32_t failed = 0;
  uint32_t offset = 0;
  uint32_t size = 0;
} status;

// Freeze pairing for this batch: WebUI changes take effect on the next boot.
String ssid, psk, token, collector, session;
IPAddress address;
uint16_t port = 8765;
size_t chunkLimit = CHUNK_MAX;
bool transportUnavailable = false;
bool previousAutoReconnect = true;
bool modeCaptured = false, bootUConsoleMode = false;
bool sourceItems = false;

void service() {
  while (GPSSerial.available()) gps.encode(GPSSerial.read());
  delay(1);
}

void waitServiced(uint32_t duration) {
  const uint32_t start = millis();
  while (millis() - start < duration) service();
}

String hexDigest(const unsigned char* bytes, size_t size) {
  static const char hex[] = "0123456789abcdef";
  String result;
  result.reserve(size * 2);
  for (size_t i = 0; i < size; ++i) {
    result += hex[bytes[i] >> 4];
    result += hex[bytes[i] & 15];
  }
  return result;
}

String bytesDigest(const uint8_t* data, size_t size) {
  unsigned char digest[32];
  const auto* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!info || mbedtls_md(info, data, size, digest) != 0) return "";
  return hexDigest(digest, sizeof(digest));
}

String fileDigest(File& file) {
  mbedtls_md_context_t context;
  mbedtls_md_init(&context);
  const auto* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  bool ok = info && mbedtls_md_setup(&context, info, 0) == 0 &&
            mbedtls_md_starts(&context) == 0 && file.seek(0);
  uint8_t buffer[1024];
  while (ok && file.available()) {
    const int count = file.read(buffer, sizeof(buffer));
    ok = count > 0 && mbedtls_md_update(&context, buffer, count) == 0;
    while (GPSSerial.available()) gps.encode(GPSSerial.read());
    yield();
  }
  unsigned char digest[32];
  ok = ok && mbedtls_md_finish(&context, digest) == 0;
  mbedtls_md_free(&context);
  return ok ? hexDigest(digest, sizeof(digest)) : String();
}

bool validPrivateAddress(const IPAddress& ip) {
  return ip[0] == 10 || (ip[0] == 192 && ip[1] == 168) ||
         (ip[0] == 172 && ip[1] >= 16 && ip[1] <= 31);
}

bool configured() {
  if (!address.fromString(cfg.collectionHost) || !validPrivateAddress(address))
    return false;
  if (cfg.collectionSsid.length() == 0 || cfg.collectionSsid.length() > 32 ||
      cfg.collectionPsk.length() < 8 || cfg.collectionPsk.length() > 63 ||
      cfg.collectionToken.length() < 32 || cfg.collectionToken.length() > 128)
    return false;
  // These fields enter HTTP headers or the plain key=value configuration.
  for (const String* value : {&cfg.collectionSsid, &cfg.collectionPsk,
                              &cfg.collectionToken})
    for (size_t i = 0; i < value->length(); ++i)
      if (static_cast<uint8_t>((*value)[i]) < 0x20 || (*value)[i] == 0x7f)
        return false;
  return cfg.collectionPort > 0;
}

bool writeServiced(WiFiClient& client, const uint8_t* bytes, size_t size,
                    uint32_t started, uint32_t timeout) {
  size_t sent = 0;
  while (sent < size && millis() - started < timeout) {
    // NetworkClient::write() has internal select/retry waits of its own.
    // Nonblocking sends let our deadline and GPS servicing remain in control.
    const int count = ::send(client.fd(), bytes + sent,
                             min(static_cast<size_t>(512), size - sent),
                             MSG_DONTWAIT);
    if (count > 0) sent += count;
    else if (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
      return false;
    service();
  }
  return sent == size;
}

// Small local HTTP client. No DNS, redirects, TLS allocation or unbounded
// getString()/readString() while the GPS UART needs regular draining.
bool request(const char* method, const String& path, const uint8_t* body,
             size_t size, DynamicJsonDocument& output, int& code,
             const String& chunkDigest = "", uint32_t timeout = REQUEST_MS) {
  code = 0;
  output.clear();
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiClient client;
  client.setTimeout(200);
  const uint32_t started = millis();
  if (!client.connect(address, port, 200)) { service(); return false; }
  String header = String(method) + " " + path + " HTTP/1.1\r\nHost: " +
      address.toString() + ":" + String(port) +
      "\r\nConnection: close\r\nAuthorization: Bearer " + token + "\r\n";
  if (session.length()) header += "X-Piglet-Session: " + session + "\r\n";
  if (chunkDigest.length()) header += "X-Chunk-SHA256: " + chunkDigest + "\r\n";
  header += "Content-Type: ";
  header += chunkDigest.length() ? "application/octet-stream" : "application/json";
  header += "\r\nContent-Length: " + String(size) + "\r\n\r\n";
  if (!writeServiced(client, reinterpret_cast<const uint8_t*>(header.c_str()),
                     header.length(), started, timeout) ||
      !writeServiced(client, body, size, started, timeout)) {
    client.stop(); return false;
  }
  String line;
  size_t headerBytes = 0;
  int contentLength = -1;
  bool headersDone = false, firstLine = true;
  while (!headersDone && millis() - started < timeout) {
    if (!client.available()) {
      if (!client.connected()) break;
      service(); continue;
    }
    const char c = static_cast<char>(client.read());
    if (++headerBytes > RESPONSE_MAX || line.length() >= 1024) break;
    if (c != '\n') { if (c != '\r') line += c; continue; }
    if (firstLine) {
      firstLine = false;
      if (!line.startsWith("HTTP/1.1 ") && !line.startsWith("HTTP/1.0 ")) break;
      code = line.substring(9, 12).toInt();
    } else if (line.length() == 0) {
      headersDone = true;
    } else {
      String lower = line; lower.toLowerCase();
      if (lower.startsWith("transfer-encoding:")) break;
      if (lower.startsWith("content-length:")) {
        String number = line.substring(15); number.trim();
        if (number.length() == 0 || number.length() > 5 || contentLength >= 0) break;
        bool digits = true;
        for (size_t i = 0; i < number.length(); ++i)
          digits = digits && number[i] >= '0' && number[i] <= '9';
        if (!digits) break;
        contentLength = number.toInt();
      }
    }
    line = "";
    service();
  }
  if (!headersDone || contentLength < 0 || contentLength > RESPONSE_MAX ||
      code < 200 || code >= 500 || (code >= 300 && code < 400)) {
    client.stop(); return false;
  }
  String response; response.reserve(contentLength);
  while (response.length() < static_cast<size_t>(contentLength) &&
         millis() - started < timeout) {
    size_t available = min(static_cast<size_t>(client.available()),
                           static_cast<size_t>(contentLength) - response.length());
    while (available--) response += static_cast<char>(client.read());
    service();
    if (!client.connected() && !client.available()) break;
  }
  client.stop();
  return response.length() == static_cast<size_t>(contentLength) &&
         !deserializeJson(output, response) && output.is<JsonObject>();
}

bool jsonRequest(const char* method, const String& path, const String& body,
                 DynamicJsonDocument& output, int& code,
                 uint32_t timeout = REQUEST_MS) {
  return request(method, path, reinterpret_cast<const uint8_t*>(body.c_str()),
                 body.length(), output, code, "", timeout);
}

bool unsignedField(JsonVariantConst value, uint64_t& result) {
  if (!value.is<uint64_t>()) return false;
  result = value.as<uint64_t>();
  return true;
}

bool validReceiptFor(JsonObjectConst receipt, const String& digest, uint32_t size,
                      const String& expectedCollector, const String& secret) {
  if (!receipt["protocol"].is<unsigned>() || receipt["protocol"].as<unsigned>() != 1)
    return false;
  const char* owner = receipt["collector_id"] | "";
  const char* sha = receipt["sha256"] | "";
  const char* importId = receipt["import_id"] | "";
  const char* signature = receipt["signature"] | "";
  uint64_t bytes, records, wifi, ble, duplicates;
  if (expectedCollector != owner || digest != sha ||
      !PigletCollection::lowerHex(signature, 64) ||
      !unsignedField(receipt["size"], bytes) || bytes != size ||
      !unsignedField(receipt["records"], records) ||
      !unsignedField(receipt["wifi"], wifi) ||
      !unsignedField(receipt["ble"], ble) ||
      !unsignedField(receipt["duplicates"], duplicates)) return false;
  char message[512];
  if (!PigletCollection::receiptMessage(message, sizeof(message), owner, sha,
                                       bytes, importId, records, wifi, ble,
                                       duplicates)) return false;
  unsigned char mac[32];
  const auto* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!info || mbedtls_md_hmac(info,
      reinterpret_cast<const unsigned char*>(secret.c_str()), secret.length(),
      reinterpret_cast<const unsigned char*>(message), strlen(message), mac) != 0)
    return false;
  return PigletCollection::constantEqual(hexDigest(mac, sizeof(mac)).c_str(),
                                         signature, 64);
}

bool validReceipt(JsonObjectConst receipt, const String& digest, uint32_t size) {
  return validReceiptFor(receipt, digest, size, collector, token);
}

String receiptPath(const String& digest) {
  return "/collection_ack/" + collector + "/" + digest + ".json";
}

bool alreadyAcknowledged(const String& digest, uint32_t size) {
  return collectionReceiptKnown(digest, size, collector);
}

bool persistReceipt(JsonObjectConst receipt, const String& digest, uint32_t size) {
  if (!validReceipt(receipt, digest, size)) return false;
  if (!SD.exists("/collection_ack") && !SD.mkdir("/collection_ack")) return false;
  const String directory = "/collection_ack/" + collector;
  if (!SD.exists(directory) && !SD.mkdir(directory)) return false;
  const String finalPath = receiptPath(digest), temporary = finalPath + ".tmp";
  SD.remove(temporary);
  File file = SD.open(temporary, FILE_WRITE);
  if (!file) return false;
  const size_t expected = measureJson(receipt);
  const bool written = serializeJson(receipt, file) == expected;
  file.flush(); file.close();
  if (!written) return false;
  // Read back the exact ledger record before replacing our own receipt metadata.
  File check = SD.open(temporary, FILE_READ);
  DynamicJsonDocument copy(2048);
  const bool verified = check && !deserializeJson(copy, check) &&
                        validReceipt(copy.as<JsonObjectConst>(), digest, size);
  check.close();
  if (!verified) return false;
  if (SD.exists(finalPath) && !SD.remove(finalPath)) return false;
  return SD.rename(temporary, finalPath);
}

bool finalReceipt(DynamicJsonDocument& doc, const String& digest, uint32_t size) {
  return doc["state"] == "committed" && doc["receipt"].is<JsonObject>() &&
         persistReceipt(doc["receipt"].as<JsonObjectConst>(), digest, size);
}

bool fileStateValid(DynamicJsonDocument& doc, const String& digest,
                    uint32_t size, uint32_t& offset) {
  uint64_t bytes, position;
  if (doc["collector_id"] != collector || doc["sha256"] != digest ||
      !unsignedField(doc["size"], bytes) ||
      bytes != size || !unsignedField(doc["offset"], position) || position > size)
    return false;
  offset = static_cast<uint32_t>(position);
  return true;
}

bool transferFile(const String& path, const String& name) {
  status.file = name; status.offset = 0; status.stage = "hashing";
  File file = SD.open(path, FILE_READ);
  String digest;
  bool retiredSnapshot = false;
  if (!file) {
    // A previous item may already have archived/coalesced this exact alias and
    // normal retention may have removed it. It still needs a source offer.
    retiredSnapshot = sourceItems && collectionArchiveDeliveredSnapshot(path, digest, status.size);
    if (!retiredSnapshot || status.size > FILE_MAX) { file.close(); return false; }
  } else {
    if (file.isDirectory() || file.size() > FILE_MAX) { file.close(); return false; }
    status.size = file.size();
    digest = fileDigest(file);
    if (!PigletCollection::lowerHex(digest.c_str(), 64) ||
        !collectionArchiveObserve(path, digest, status.size)) { file.close(); return false; }
  }
  if (!sourceItems && alreadyAcknowledged(digest, status.size)) {
    file.close();
    // A durable receipt is also the handover intent. A reset after receipt
    // persistence but before moving the CSV retries only this local step.
    status.stage = "archiving";
    return moveToUploaded(path, digest, status.size);
  }
  DynamicJsonDocument announce(1024), reply(12288);
  announce["sha256"] = digest; announce["size"] = status.size; announce["name"] = name;
  if (sourceItems) {
    const String sourceId = bytesDigest(reinterpret_cast<const uint8_t*>(path.c_str()), path.length());
    if (!PigletCollection::lowerHex(sourceId.c_str(), 64)) { file.close(); return false; }
    announce["source_id"] = sourceId;
  }
  String metadata; serializeJson(announce, metadata);
  unsigned failures = 0;
  bool complete = false;
  uint8_t* chunk = nullptr;
  while (failures < RETRIES && !complete) {
    if (WiFi.status() != WL_CONNECTED) {
      status.stage = "reconnecting";
      WiFi.begin(ssid.c_str(), psk.c_str());
      const uint32_t reconnectStarted = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - reconnectStarted < 3000)
        service();
      if (WiFi.status() != WL_CONNECTED) { ++failures; continue; }
    }
    status.stage = "resuming";
    int code = 0;
    uint32_t offset = 0;
    const bool offered = jsonRequest("POST", "/pc/v1/file", metadata, reply, code);
    if (offered && (code == 400 || code == 413 || code == 415)) break;
    if (!offered || code != 200 ||
        !fileStateValid(reply, digest, status.size, offset)) {
      ++failures; waitServiced(150); continue;
    }
    if (reply["state"] == "committed") {
      complete = finalReceipt(reply, digest, status.size); break;
    }
    // The saved receipt is not permission to fabricate a new import. If the
    // collector no longer confirms it and local bytes expired, stay pending.
    if (retiredSnapshot) break;
    const bool processing = reply["state"] == "processing";
    if (!processing && reply["state"] != "receiving") break;
    if (!processing && !chunk) {
      chunk = static_cast<uint8_t*>(malloc(chunkLimit));
      if (!chunk) break;
    }
    bool uploadOk = true;
    status.stage = processing ? "importing" : "transferring";
    while (!processing && offset < status.size) {
      const size_t count = min(chunkLimit, static_cast<size_t>(status.size - offset));
      if (!file.seek(offset) || file.read(chunk, count) != static_cast<int>(count)) {
        uploadOk = false; break;
      }
      const String chunkSha = bytesDigest(chunk, count);
      const String url = "/pc/v1/chunk?id=" + digest + "&offset=" + String(offset);
      if (!PigletCollection::lowerHex(chunkSha.c_str(), 64) ||
          !request("PUT", url, chunk, count, reply, code, chunkSha) || code != 200) {
        uploadOk = false; break;
      }
      uint32_t next;
      if (!fileStateValid(reply, digest, status.size, next) || next != offset + count) {
        uploadOk = false; break;
      }
      offset = static_cast<uint32_t>(next); status.offset = offset;
      service();
    }
    if (!uploadOk) { ++failures; waitServiced(150); continue; }
    if (!processing) {
      const String commit = "{\"sha256\":\"" + digest + "\"}";
      if (!jsonRequest("POST", "/pc/v1/commit", commit, reply, code) ||
          (code != 200 && code != 202)) {
        ++failures; waitServiced(150); continue;
      }
      if (reply["state"] == "committed") {
        complete = finalReceipt(reply, digest, status.size); break;
      }
      if (reply["state"] != "processing") break;
    }
    status.stage = "importing";
    // Large validated imports may legitimately take longer than two minutes.
    // Keep waiting while the authenticated collector reports processing; every
    // individual request, error, retry and disconnection remains bounded.
    while (WiFi.status() == WL_CONNECTED) {
      waitServiced(250);
      if (!jsonRequest("GET", "/pc/v1/status?id=" + digest, "", reply, code) || code != 200) {
        ++failures; break;
      }
      if (!fileStateValid(reply, digest, status.size, offset)) { failures = RETRIES; break; }
      if (reply["state"] == "committed") {
        complete = finalReceipt(reply, digest, status.size);
        if (!complete) failures = RETRIES;
        break;
      }
      if (reply["state"] != "processing") { failures = RETRIES; break; }
    }
    if (!complete && WiFi.status() != WL_CONNECTED) ++failures;
  }
  if (!complete && failures >= RETRIES) transportUnavailable = true;
  free(chunk); file.close();
  if (!complete) return false;
  if (retiredSnapshot) return true; // Already durably archived/expired; no new SD mutation.
  status.stage = "archiving";
  // finalReceipt() verified the HMAC and wrote/read back the durable ledger.
  // The source handle is closed before any rename or retention is attempted.
  return moveToUploaded(path, digest, status.size);
}

void releaseForNormalBoot() {
  // Collection is a setup-only prefix, before the first original Wi-Fi init.
  // Never erase the saved STA credentials or reset the original AP timers.
  WiFi.setAutoReconnect(false);
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_OFF);
  // The pinned core defaults persistent to true; setup has not changed it before
  // this prefix. Collection used RAM storage to avoid writing its pairing to NVS.
  WiFi.persistent(true);
  WiFi.setAutoReconnect(previousAutoReconnect);
  status.busy = false;
  status.stage = "finished";
}

}  // namespace

String collectionDigestFile(const String& path) {
  File file = SD.open(path, FILE_READ);
  if (!file || file.isDirectory()) { file.close(); return ""; }
  const String digest = fileDigest(file);
  file.close(); return digest;
}

bool collectionReceiptKnown(const String& digest, uint32_t size, const String& collectorId) {
  if (!PigletCollection::lowerHex(digest.c_str(), 64) ||
      !PigletCollection::lowerHex(collectorId.c_str(), 32) ||
      cfg.collectionToken.length() == 0) return false;
  File receipt = SD.open("/collection_ack/" + collectorId + "/" + digest + ".json", FILE_READ);
  if (!receipt) return false;
  DynamicJsonDocument doc(2048);
  const bool ok = receipt.size() <= 4096 && !deserializeJson(doc, receipt) &&
      validReceiptFor(doc.as<JsonObjectConst>(), digest, size, collectorId, cfg.collectionToken);
  receipt.close(); return ok;
}

bool collectionUConsoleMode() {
  if (!modeCaptured) {
    bootUConsoleMode = cfg.operatingMode == "uconsole";
    modeCaptured = true;
  }
  return bootUConsoleMode;
}
bool collectionBusy() { return status.busy; }

void collectionAddStatus(JsonObject object) {
  collectionArchiveStatus(object);
  object["busy"] = status.busy;
  object["stage"] = status.stage;
  object["result"] = status.result;
  object["file"] = status.file;
  object["total"] = status.total;
  object["done"] = status.done;
  object["failed"] = status.failed;
  object["offset"] = status.offset;
  object["size"] = status.size;
}

void collectionBoot() {
  if (!collectionUConsoleMode()) return;
  status.busy = true; status.stage = "discovering";
  sourceItems = false;
  transportUnavailable = false;
  previousAutoReconnect = WiFi.getAutoReconnect();
  token = cfg.collectionToken;
  status.stage = "inventory";
  if (!collectionArchivePrepare()) {
    status.result = "Inventory unavailable; unconfirmed CSV deletion is protected";
    status.busy = false; status.stage = "finished"; return;
  }
  collector = collectionArchiveCollector();
  if (!sdOk || !configured()) {
    status.result = !sdOk ? "SD unavailable; continuing normal boot" :
                          "Collection pairing is incomplete; continuing normal boot";
    status.busy = false; status.stage = "finished";
    return;
  }
  ssid = cfg.collectionSsid; psk = cfg.collectionPsk;
  token = cfg.collectionToken; port = cfg.collectionPort;
  session = "";
  status.stage = "discovering";
  const uint32_t started = millis();
  WiFi.persistent(false); WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_STA); WiFi.begin(ssid.c_str(), psk.c_str());
  DynamicJsonDocument reply(12288);
  bool paired = false;
  int code = 0;
  while (PigletCollection::remainingBudget(started, millis(), DISCOVERY_MS)) {
    service();
    if (WiFi.status() != WL_CONNECTED) continue;
    const uint32_t remaining = PigletCollection::remainingBudget(started, millis(), DISCOVERY_MS);
    if (remaining < 250) break;
    if (jsonRequest("GET", "/pc/v1/hello", "", reply, code,
                    min(remaining, REQUEST_MS)) && code == 200 &&
        PigletCollection::remainingBudget(started, millis(), DISCOVERY_MS) > 0 &&
        reply["protocol"].is<unsigned>() && reply["protocol"].as<unsigned>() == 1) {
      collector = reply["collector_id"].as<String>();
      uint64_t maximum;
      paired = PigletCollection::lowerHex(collector.c_str(), 32) &&
               unsignedField(reply["max_chunk"], maximum) && maximum > 0;
      if (paired) {
        chunkLimit = static_cast<size_t>(min(static_cast<uint64_t>(CHUNK_MAX), maximum));
        sourceItems = reply["source_items"].is<unsigned>() &&
                      reply["source_items"].as<unsigned>() == 1;
        break;
      }
    }
    if (code == 401 || code == 403) break;
    waitServiced(min(static_cast<uint32_t>(150),
                     PigletCollection::remainingBudget(started, millis(), DISCOVERY_MS)));
  }
  if (!paired) {
    status.result = "Collector unavailable or pairing failed; continuing normal boot";
    releaseForNormalBoot(); return;
  }
  if (!collectionArchiveSetCollector(collector)) {
    status.result = "Cannot save paired collector identity; data retained";
    releaseForNormalBoot(); return;
  }
  collectionArchiveRecover();
  status.stage = "listing";
  CollectionFileList files;
  if (!collectionArchiveList(files, true, sourceItems)) {
    status.result = "Cannot list pending CSV safely; continuing normal boot";
    releaseForNormalBoot(); return;
  }
  status.total = files.count;
  char deviceId[32];
  snprintf(deviceId, sizeof(deviceId), "piglet-%012llx",
           static_cast<unsigned long long>(ESP.getEfuseMac() & 0xffffffffffffULL));
  DynamicJsonDocument begin(512);
  begin["device_id"] = deviceId; begin["pending_files"] = status.total;
  if (sourceItems) begin["source_items"] = 1;
  if (sourceItems && !collectionArchiveAnnounceSources(files)) {
    status.result = "Cannot save source obligations; data retained";
    releaseForNormalBoot(); return;
  }
  String body; serializeJson(begin, body);
  if (!jsonRequest("POST", "/pc/v1/begin", body, reply, code) || code != 200 ||
      reply["protocol"] != 1 || reply["collector_id"] != collector) {
    status.result = "Collector did not begin a session; data retained";
    releaseForNormalBoot(); return;
  }
  if (sourceItems && (!reply["source_items"].is<unsigned>() ||
                     reply["source_items"].as<unsigned>() != 1)) {
    status.result = "Collector did not confirm source-item metadata; data retained";
    releaseForNormalBoot(); return;
  }
  session = reply["session_id"].as<String>();
  if (!PigletCollection::lowerHex(session.c_str(), 32)) {
    status.result = "Invalid collector session; data retained";
    releaseForNormalBoot(); return;
  }
  for (uint32_t index = 0; index < files.count; ++index) {
    const String path = files.files[index].path;
    const String name = pathBasename(path);
    if (transferFile(path, name)) ++status.done;
    else ++status.failed;
    if (transportUnavailable) {
      status.failed = status.total - status.done;
      break;
    }
    service();
  }
  collectionArchiveRefresh(true);
  status.stage = "finishing";
  const bool finished = jsonRequest("POST", "/pc/v1/finish", "{}", reply, code) &&
      code == 200 && reply["finished"].is<bool>() && reply["finished"].as<bool>();
  const bool sourcesCleared = !sourceItems || (finished && collectionArchiveFinishSources(files));
  status.result = String(status.done) + " acknowledged, " + String(status.failed) +
                  " deferred; continuing normal boot";
  if (sourceItems && !sourcesCleared) status.result += "; source obligations kept for retry";
  Serial.printf("[COLLECTION] %s\n", status.result.c_str());
  releaseForNormalBoot();
}
