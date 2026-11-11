"""Execute the real CSV-upload C++ and WebUI handlers with offline host mocks.

Run with CXX=g++ or CXX='path/to/zig c++'. No firmware/USB/network is used.
The source is copied to a temporary build directory so only hardware headers
are replaced. Upload functions and HTTP handlers are compiled without edits.
"""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source" / "Piglet"


def function(text, signature):
    first = text.index(signature)
    opening = text.index("{", first)
    depth = 1
    end = opening + 1
    # These tested functions do not contain brace characters in strings/comments.
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[first:end]


MOCKS = r'''
#pragma once
#include <string>
#include <vector>
#include <map>
#include <cassert>
#include <cstring>
#include <cstdint>
#include <algorithm>
#include <iostream>
#include <cstdio>
struct String : std::string {
  using std::string::string;
  String() = default;
  String(const std::string& s) : std::string(s) {}
  String(int x) : std::string(std::to_string(x)) {}
  String(unsigned x) : std::string(std::to_string(x)) {}
  String(unsigned long x) : std::string(std::to_string(x)) {}
  int indexOf(char c, int p=0) const { auto n=find(c,p); return n==npos?-1:(int)n; }
  int indexOf(const char* s, int p=0) const { auto n=find(s,p); return n==npos?-1:(int)n; }
  String substring(int a) const { return substr(a); }
  String substring(int a, int b) const { return substr(a,b-a); }
  bool startsWith(const char* s) const { return rfind(s,0)==0; }
  bool endsWith(const char* s) const { size_t n=strlen(s); return size()>=n && compare(size()-n,n,s)==0; }
  int toInt() const { return std::atoi(c_str()); }
  void trim() { auto a=find_first_not_of(" \r\n\t"), b=find_last_not_of(" \r\n\t"); *this=a==npos?"":substr(a,b-a+1); }
  void toLowerCase() { std::transform(begin(),end(),begin(),[](unsigned char c){return std::tolower(c);}); }
};
inline int sdCalls=0, networkCalls=0, bleStops=0, oledCalls=0, httpCode=0, sends=0;
inline bool sdOk=true, scanningEnabled=true, uploading=false, uploadPausedScanWasEnabled=false;
inline bool apWindowActive=false, apClientSeen=false;
inline bool apExtended=false, apForceClose=false, userScanOverride=false;
inline uint32_t apStartMs=0, apExtendedStartMs=0, nowMs=0;
inline constexpr uint32_t AP_WINDOW_MS=60000, AP_EXTENDED_WINDOW_MS=300000;
inline int wifiStatus=3, wifiMode=0, disconnects=0, erasedCredentials=0, apDisconnects=0;
inline uint64_t totalSdBytes=8192, usedSdBytes=0;
inline bool allPathsExist=true;
inline int logCloses=0;
inline uint32_t uploadDoneFiles=7, uploadFailedFiles=8, uploadTotalFiles=9;
inline int wigleLastHttpCode=503, wigleTokenStatus=1;
inline String uploadLastResult, uploadCurrentFile="before.csv", uploadTargetName="before";
inline String currentCsvPath="/logs/current.csv";
inline bool modeCaptured=false, bootUConsoleMode=false;
inline struct Config { String operatingMode="normal", wigleBasicToken="saved-wigle", wdgwarsApiKey="saved-wars", meshModeOnBoot="none", deviceName=""; } cfg;
inline bool wigleConfigured() { return !cfg.wigleBasicToken.empty(); }
inline bool wdgwarsConfigured() { return !cfg.wdgwarsApiKey.empty(); }
bool collectionUConsoleMode();
bool externalLogUploadsAllowed();
bool externalLogUploadBlocked();
inline struct SerialMock {
  template<typename T> void print(const T&) {}
  template<typename T> void println(const T&) {}
  void println() {}
  template<typename... T> void printf(const char*, T...) {}
} Serial;
inline unsigned long millis() { return nowMs; }
inline void delay(unsigned long) {}
inline void yield() {}
inline uint32_t esp_random() { return 42; }
inline void closeLogFile() { ++logCloses; }
inline String sanitiseDeviceName(const String& s) { return s; }
inline void updateOLED(int) { ++oledCalls; }
inline void bleScannerStop() { ++bleStops; }
struct IPAddress { String toString() const { return "192.0.2.1"; } };
inline const char* WIGLE_HOST="api.wigle.net";
inline const uint16_t WIGLE_PORT=443;
inline constexpr int WL_CONNECTED=3, WIFI_AP=1, WIFI_AP_STA=2, WIFI_STA=3, FILE_READ=0;
using wifi_mode_t = int;
inline struct WiFiMock {
  int status() const { return wifiStatus; }
  int getMode() const { return wifiMode; }
  void mode(int mode) { wifiMode=mode; }
  void setAutoReconnect(bool) {}
  void persistent(bool) {}
  void disconnect(bool,bool erase) { ++disconnects; erasedCredentials+=erase; wifiStatus=0; }
  void softAPdisconnect(bool) { ++apDisconnects; }
  unsigned softAPgetStationNum() const { return 0; }
  int RSSI() const { return -42; }
  String SSID() const { return "synthetic"; }
  IPAddress localIP() const { return {}; }
  IPAddress gatewayIP() const { return {}; }
  IPAddress dnsIP() const { return {}; }
  bool hostByName(const char*, IPAddress&) { ++networkCalls; return true; }
} WiFi;
struct File {
  bool valid=false; int offset=0;
  explicit File(bool v=false) : valid(v) {}
  explicit operator bool() const { return valid; }
  void close() { valid=false; }
  size_t size() const { return 4; }
  int available() { return valid && offset<4; }
  int read(uint8_t* p,size_t n) { int count=std::min<int>(n,4-offset); memset(p,'x',count); offset+=count; return count; }
  String readStringUntil(char) { ++offset; return ""; }
  const char* name() const { return "/logs/test.csv"; }
  File openNextFile() { return File(false); }
};
inline struct SdMock {
  bool exists(const String&) { ++sdCalls; return allPathsExist; }
  uint64_t totalBytes() { ++sdCalls; return totalSdBytes; }
  uint64_t usedBytes() { ++sdCalls; return usedSdBytes; }
  bool mkdir(const char*) { ++sdCalls; return true; }
  File open(const String& p, int=0) { ++sdCalls; return File(p!="/logs"); }
  bool remove(const String&) { ++sdCalls; return true; }
} SD;
inline String pathBasename(const String& p) { return p; }
inline String normalizeSdPath(const char*,const char* p) { return p; }
inline bool collectionArchiveRetentionEligible(const String&) { return true; }
inline bool collectionArchiveCanDelete(const String&) { return true; }
inline bool moveToUploaded(const String&) { ++sdCalls; return true; }
struct WiFiClientSecure {
  bool live=false, wars=false; int line=0; String request;
  void setInsecure() {}
  void setTimeout(int) {}
  bool connect(const char* host, uint16_t) { ++networkCalls; live=true; wars=String(host)=="wdgwars.pl"; return true; }
  void print(const String& s) { ++networkCalls; request+=s; }
  size_t write(const uint8_t*,size_t n) { ++networkCalls; return n; }
  void flush() {}
  bool connected() const { return live; }
  int available() const { return live; }
  void stop() { live=false; }
  String readStringUntil(char) {
    ++line;
    bool poll=request.startsWith("GET /api/v2/upload-job/");
    if (line==1) return wars&&!poll?"HTTP/1.0 202 Accepted":"HTTP/1.0 200 OK";
    if (line==2) return "\r";
    live=false;
    return poll?"{\"status\":\"done\",\"imported\":1}":"{\"ok\":true,\"job_id\":42}";
  }
};
inline void tlsMaybeSetBufferSizes(WiFiClientSecure&,int,int) {}
struct DynamicJsonDocument {
  struct Slot {
    std::string text;
    Slot& operator=(bool x) { text=x?"true":"false"; return *this; }
    Slot& operator=(int x) { text=std::to_string(x); return *this; }
    Slot& operator=(unsigned x) { text=std::to_string(x); return *this; }
    Slot& operator=(const String& x) { text=x; return *this; }
  };
  std::map<std::string,Slot> values;
  explicit DynamicJsonDocument(size_t) {}
  Slot& operator[](const char* key) { return values[key]; }
};
inline void serializeJson(const DynamicJsonDocument& doc,String& out) {
  out="{"; for (auto& p:doc.values) out+=p.first+":"+p.second.text+";"; out+="}";
}
inline String httpBody;
inline struct ServerMock {
  void send(int c,const char*,const String& s) { ++sends; httpCode=c; httpBody=s; }
  bool hasArg(const char*) { return true; }
  String arg(const char*) { return "/logs/test.csv"; }
} server;
'''


MAIN = r'''
void reset(const char* mode) {
  cfg.operatingMode=mode; modeCaptured=false;
  sdCalls=networkCalls=bleStops=oledCalls=sends=httpCode=0;
  sdOk=true; scanningEnabled=true; uploading=false; uploadPausedScanWasEnabled=false;
  uploadDoneFiles=7; uploadFailedFiles=8; uploadTotalFiles=9;
  uploadCurrentFile="before.csv"; uploadTargetName="before";
  uploadLastResult="before"; wigleLastHttpCode=503;
  cfg.meshModeOnBoot="none";
  uConsoleStaWebWindow=false; apWindowActive=false; apExtended=false; apForceClose=false;
  userScanOverride=false; apStartMs=apExtendedStartMs=0; nowMs=1000;
  wifiStatus=WL_CONNECTED; wifiMode=WIFI_STA; disconnects=erasedCredentials=apDisconnects=0;
  totalSdBytes=8192; usedSdBytes=0; allPathsExist=true; logCloses=0;
}
void blockedNoEffects() {
  assert(sdCalls==0 && networkCalls==0 && bleStops==0 && oledCalls==0);
  assert(scanningEnabled && !uploading && !uploadPausedScanWasEnabled);
  assert(uploadDoneFiles==7 && uploadFailedFiles==8 && uploadTotalFiles==9);
  assert(uploadCurrentFile=="before.csv" && uploadTargetName=="before");
  assert(wigleLastHttpCode==0);
  assert(uploadLastResult.indexOf("Collection-only")>=0);
  assert(cfg.wigleBasicToken=="saved-wigle" && cfg.wdgwarsApiKey=="saved-wars");
}
int main() {
  // The four real entry points refuse before file access, TLS/DNS or scan/OLED state.
  reset("uconsole"); assert(!uploadFileToWigle("/logs/test.csv")); blockedNoEffects();
  reset("uconsole"); assert(!uploadFileToWdgwars("/uploaded/test.csv")); blockedNoEffects();
  reset("uconsole"); assert(uploadAllCsvsToWigle(-1)==0); blockedNoEffects();
  reset("uconsole"); assert(uploadAllCsvsToWdgwars(-1)==0); blockedNoEffects();
  // The real HTTP handlers refuse even malformed/config-unavailable requests first.
  for (auto handler : {handleWigleUploadAll,handleWigleUploadOne,handleWdgwarsUploadAll}) {
    reset("uconsole"); sdOk=false; handler(); blockedNoEffects();
    assert(sends==1 && httpCode==403);
    assert(httpBody.indexOf("ok:false")>=0 && httpBody.indexOf("uploaded:0")>=0);
    assert(httpBody.indexOf("Collection-only")>=0);
  }
  // Editing the saved next-boot mode does not bypass an active UConsole boot.
  reset("uconsole"); assert(collectionUConsoleMode()); cfg.operatingMode="normal";
  assert(!uploadFileToWigle("/logs/test.csv")); blockedNoEffects();
  // Existing Normal uploads still reach the real networking/streaming success path.
  reset("normal"); assert(uploadFileToWigle("/logs/test.csv")); assert(networkCalls>0 && sdCalls>0);
  reset("normal"); assert(uploadFileToWdgwars("/logs/test.csv")); assert(networkCalls>0 && sdCalls>0);
  reset("normal"); sdOk=false; assert(uploadAllCsvsToWigle(-1)==0); assert(uploadLastResult=="SD not OK");
  reset("normal"); sdOk=false; assert(uploadAllCsvsToWdgwars(-1)==0); assert(uploadLastResult=="SD not OK");
  reset("normal"); assert(!collectionUConsoleMode()); cfg.operatingMode="uconsole";
  assert(externalLogUploadsAllowed());
  // A UConsole home STA is a real STA, with the full original 60-second budget.
  reset("uconsole"); scanningEnabled=false; startUConsoleStaWebWindow();
  assert(uConsoleStaWebWindowActive() && !apWindowActive);
  nowMs=apStartMs+60000; stopAPIfAllowed(); assert(disconnects==0);
  nowMs++; stopAPIfAllowed();
  assert(disconnects==1 && erasedCredentials==0 && apDisconnects==0);
  assert(!uConsoleStaWebWindowActive() && scanningEnabled && wifiMode==WIFI_STA);
  // Stay really extends for five minutes from the click, including repeated Stay.
  reset("uconsole"); startUConsoleStaWebWindow(); nowMs+=59000; handleExtend();
  assert(httpCode==200 && apExtended);
  nowMs=apExtendedStartMs+300000; stopAPIfAllowed(); assert(disconnects==0);
  handleExtend(); nowMs=apExtendedStartMs+300001; stopAPIfAllowed(); assert(disconnects==1);
  // Start uses the existing immediate close; explicit Stop remains respected.
  reset("uconsole"); startUConsoleStaWebWindow(); handleStart(); stopAPIfAllowed();
  assert(disconnects==1 && scanningEnabled && userScanOverride);
  reset("uconsole"); startUConsoleStaWebWindow(); handleStop();
  nowMs=apStartMs+60001; stopAPIfAllowed(); assert(disconnects==1 && !scanningEnabled);
  // An early STA loss never tears down a newly selected Core/Node radio.
  reset("uconsole"); startUConsoleStaWebWindow(); wifiStatus=0; stopAPIfAllowed();
  assert(!uConsoleStaWebWindowActive() && disconnects==0);
  // Normal home STA has no new timeout; it remains paused exactly as before.
  reset("normal"); startUConsoleStaWebWindow(); nowMs+=900000; stopAPIfAllowed();
  assert(!uConsoleStaWebWindowActive() && disconnects==0 && shouldPauseScanning());
  for (const char* mode : {"normal","uconsole"}) {
    reset(mode); wifiStatus=0; apWindowActive=true; wifiMode=WIFI_AP; apStartMs=nowMs;
    startUConsoleStaWebWindow(); assert(!uConsoleStaWebWindowActive());
    nowMs+=60000; stopAPIfAllowed(); assert(disconnects==0);
    nowMs++; stopAPIfAllowed(); assert(disconnects==1 && apDisconnects==1 && scanningEnabled);
  }
  for (const char* mesh : {"core","node","Core","Node"}) {
    reset("uconsole"); cfg.meshModeOnBoot=mesh; startUConsoleStaWebWindow();
    nowMs+=900000; stopAPIfAllowed(); assert(!uConsoleStaWebWindowActive() && disconnects==0);
  }
  // A full card or colliding fallback never authorizes truncating pending CSV.
  reset("uconsole"); usedSdBytes=totalSdBytes; assert(!uConsoleLogSpaceReady());
  assert(sdCalls==2 && logCloses==0);
  reset("uconsole"); usedSdBytes=totalSdBytes-512; assert(!uConsoleLogSpaceReady());
  reset("uconsole"); assert(uConsoleLogSpaceReady());
  reset("uconsole"); assert(newCsvFilename().empty()); assert(sdCalls==27);
  reset("normal"); usedSdBytes=totalSdBytes; assert(uConsoleLogSpaceReady() && sdCalls==0);
  assert(!newCsvFilename().empty()); // exact original Normal fallback
  reset("uconsole"); allPathsExist=false; assert(!newCsvFilename().empty());
  reset("uconsole"); assert(!uConsoleLogWriteComplete(1,100)); assert(!sdOk && logCloses==1);
  reset("uconsole"); assert(uConsoleLogWriteComplete(100,100) && sdOk && logCloses==0);
  reset("normal"); assert(uConsoleLogWriteComplete(1,100) && sdOk && logCloses==0);
  std::cout << "Real backend/HTTP policy, zero blocked side effects, Normal TLS mocks and boot latch: PASS\n";
  std::cout << "Real STA/AP lifecycle, 60s, Stay 5min, Start/Stop, Normal and Core/Node: PASS\n";
}
'''


class ExternalUploadPolicy(unittest.TestCase):
    def test_real_upload_backend_and_http_handlers_offline(self):
        configured = os.environ.get("CXX")
        compiler = shlex.split(configured, posix=os.name != "nt") if configured else []
        compiler = [part.strip('"') for part in compiler]
        if not compiler:
            available = shutil.which("g++") or shutil.which("clang++")
            if available:
                compiler = [available]
        if not compiler:
            self.skipTest("Host C++ compiler unavailable; set CXX (including 'zig c++' if applicable)")
        web = (SOURCE / "WebUI.cpp").read_text(encoding="utf-8")
        collection = (SOURCE / "PigletCollection.cpp").read_text(encoding="utf-8")
        wifi = (SOURCE / "WiFiManager.cpp").read_text(encoding="utf-8")
        sd = (SOURCE / "SDUtils.cpp").read_text(encoding="utf-8")
        pieces = [function(collection, "bool collectionUConsoleMode()")]
        pieces.append("static bool uConsoleStaWebWindow = false;")
        for signature in ("bool uConsoleStaWebWindowActive()", "void startUConsoleStaWebWindow()",
                          "void stopAPIfAllowed()", "bool shouldPauseScanning()"):
            pieces.append(function(wifi, signature))
        for signature in ("static bool uConsoleLogSpaceReady()", "static bool uConsoleLogWriteComplete(",
                          "static String newCsvFilename()"):
            pieces.append(function(sd, signature))
        for signature in ("static bool rejectExternalLogUpload()", "static void handleWdgwarsUploadAll()",
                          "static void handleWigleUploadAll()", "static void handleWigleUploadOne()",
                          "static void handleStart()", "static void handleExtend()", "static void handleStop()"):
            pieces.append(function(web, signature))
        with tempfile.TemporaryDirectory(prefix="piglet-policy-") as temp:
            path = Path(temp)
            (path / "Mocks.h").write_text(MOCKS, encoding="utf-8")
            for name in ("Arduino.h", "CollectionArchive.h", "PigletCollection.h", "Globals.h",
                         "SDUtils.h", "Display.h", "WiFiClientSecure.h"):
                (path / name).write_text('#include "Mocks.h"\n', encoding="utf-8")
            for name in ("WigleUpload.cpp", "WigleUpload.h"):
                shutil.copyfile(SOURCE / name, path / name)
            (path / "main.cpp").write_text('#include "WigleUpload.cpp"\n' + "\n".join(pieces) + MAIN,
                                            encoding="utf-8")
            executable = path / ("policy.exe" if os.name == "nt" else "policy")
            built = subprocess.run(compiler + ["-std=c++17", "-I", str(path), str(path / "main.cpp"),
                                               "-o", str(executable)], capture_output=True, text=True, timeout=180)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS", result.stdout)

    def test_no_external_csv_upload_entry_point_bypasses_policy(self):
        sketch = (SOURCE / "Piglet.ino").read_text(encoding="utf-8")
        upload = (SOURCE / "WigleUpload.cpp").read_text(encoding="utf-8")
        self.assertLess(sketch.index("if (externalLogUploadBlocked())"),
                        sketch.index("uint32_t uploaded = uploadAllCsvsToWigle(limit)"))
        for name in ("uploadFileToWigle", "uploadFileToWdgwars", "uploadAllCsvsToWigle", "uploadAllCsvsToWdgwars"):
            body = upload[upload.index(name + "("):]
            body = body[body.index("{") + 1:].lstrip()
            self.assertTrue(body.startswith("if (externalLogUploadBlocked()) return "), name)
        # Diagnostic credential/profile requests remain deliberately separate.
        for name in ("wigleTestToken", "wdgwarsTestKey"):
            self.assertNotIn("externalLogUploadBlocked", function(upload, "bool " + name + "()"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
