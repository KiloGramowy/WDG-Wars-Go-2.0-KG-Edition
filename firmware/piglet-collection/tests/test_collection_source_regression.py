"""Source regression evidence for task02; no SDK, serial port or hardware access.

The pinned v1.0.1 source is included in baseline/ with manifest hashes.
These checks deliberately do not claim hardware timing or successful RF scans.
"""
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source" / "Piglet"
BASE = "b6c706cf3a41e5df8946a87f0cfba05b7ac93d3a"
PREFIX = """  // Optional Collection prefix. The existing boot sequence below remains shared
  // by both modes, including its independent AP window and home Wi-Fi rules.
  if (collectionUConsoleMode()) collectionBoot();

"""
BOOT_UPLOAD_GUARD = """    if (externalLogUploadBlocked()) {
      // Collection success/failure and home STA never enable direct CSV uploads.
    } else if (staOk && sdOk && (hasWigle || hasWdg)) {"""


def without_boot_upload_guard(text):
    assert text.count(BOOT_UPLOAD_GUARD) == 1
    text = text.replace(BOOT_UPLOAD_GUARD, "    if (staOk && sdOk && (hasWigle || hasWdg)) {", 1)
    text = text.replace(" && !collectionUConsoleMode()) {", ") {", 1)
    return text.replace("  // Start the UConsole home-STA timer only once WebUI is ready. Normal mode\n"
                        "  // and saved Core/Node boot keep their original startup rules.\n"
                        "  startUConsoleStaWebWindow();\n", "", 1)


def without_uconsole_sta_window(text):
    text = text.replace('#include "PigletCollection.h"\n', "", 1)
    prefix = section(text, "// Separate access state:", "// ---- STA disconnect helper ----")
    text = text.replace(prefix, "", 1)
    guard = section(text, "  if (uConsoleStaWebWindow && WiFi.status()", "  // Decide whether the AP")
    text = text.replace(guard, "  if (!apWindowActive) return;\n\n", 1)
    text = text.replace('    if (uConsoleStaWebWindow) Serial.printf("[WIFI] Closing UConsole home STA WebUI (%s).\\n", reason);\n'
                        '    else Serial.printf', '    Serial.printf', 1)
    text = text.replace("    if (apWindowActive) WiFi.softAPdisconnect(true);\n"
                        "    const bool wasUConsoleSta = uConsoleStaWebWindow;\n"
                        "    uConsoleStaWebWindow = false;\n", "    WiFi.softAPdisconnect(true);\n", 1)
    return text.replace("    WiFi.disconnect(true, !wasUConsoleSta); // STA window never erases saved NVS credentials\n",
                        "    WiFi.disconnect(true, true);\n", 1)


def read(name):
    return (SOURCE / name).read_text(encoding="utf-8")


def baseline(name):
    return (ROOT / "baseline" / name).read_text(encoding="utf-8")


def section(text, start, end):
    first = text.index(start)
    return text[first:text.index(end, first)]


class CollectionSourceRegression(unittest.TestCase):
    def test_normal_entire_sketch_is_unchanged_without_optional_prefix(self):
        current = without_boot_upload_guard(read("Piglet.ino"))
        self.assertEqual(current.count(PREFIX), 1)
        current = current.replace('#include "PigletCollection.h"\n', "", 1)
        self.assertEqual(current.replace(PREFIX, "", 1), baseline("Piglet.ino"))

    def test_prefix_precedes_shared_original_wifi_boot(self):
        current = without_boot_upload_guard(read("Piglet.ino"))
        prefix = current.index("if (collectionUConsoleMode()) collectionBoot();")
        original = current.index("  // WiFi setup:")
        self.assertLess(prefix, original)
        self.assertEqual(
            current[original:], baseline("Piglet.ino")[baseline("Piglet.ino").index("  // WiFi setup:"):]
        )
        self.assertEqual(current.count("  startWebServer();"), 1)
        self.assertNotIn("uConsoleBoot", current)

    def test_no_button_or_deep_sleep_gesture_changed(self):
        current, original = read("Piglet.ino"), baseline("Piglet.ino")
        self.assertEqual(section(current, "// ---------------- Deep Sleep", "static void drainBlePendingToLog"),
                         section(original, "// ---------------- Deep Sleep", "static void drainBlePendingToLog"))

    def test_normal_default_requires_explicit_mode_selection(self):
        self.assertIn('String operatingMode = "normal";', read("Config.h"))
        self.assertIn('cfg.operatingMode == "uconsole"', read("PigletCollection.cpp"))

    def test_original_home_sta_and_ap_lifecycle_are_unchanged(self):
        self.assertEqual(without_uconsole_sta_window(read("WiFiManager.cpp")), baseline("WiFiManager.cpp"))
        self.assertEqual(read("Globals.cpp"), baseline("Globals.cpp"))
        self.assertIn("const uint32_t AP_WINDOW_MS = 60000UL;", read("Globals.cpp"))
        self.assertIn("const uint32_t AP_EXTENDED_WINDOW_MS = 5UL * 60UL * 1000UL;", read("Globals.cpp"))

    def test_original_oled_ip_and_pause_views_are_unchanged(self):
        self.assertEqual(read("Display.cpp"), baseline("Display.cpp"))

    def test_stay_extend_and_start_handlers_are_unchanged(self):
        current, original = read("WebUI.cpp"), baseline("WebUI.cpp")
        current = current.replace("apWindowActive || uConsoleStaWebWindowActive()", "apWindowActive")
        current = current.replace("!apWindowActive && !uConsoleStaWebWindowActive()", "!apWindowActive")
        current = current.replace('  $(\'webWindowPrompt\').textContent=(j.uConsoleStaWebWindow?\'The home Wi-Fi WebUI window\':\'The AP\')+\' is about to close and the device will start scanning. Tap "Stay" to keep the WebUI open for another 5 minutes.\';\n', "", 1)
        self.assertEqual(section(current, "static void handleStart()", "static void handleSaveConfig()"),
                         section(original, "static void handleStart()", "static void handleSaveConfig()"))
        self.assertEqual(section(current, "async function apKeepAliveYes()", "/* ---- Upload progress poller"),
                         section(original, "async function apKeepAliveYes()", "/* ---- Upload progress poller"))

    def test_collection_never_starts_or_services_a_parallel_webui_ap(self):
        client = read("PigletCollection.cpp")
        for forbidden in ("startAP(", "startWebServer(", "server.handleClient(",
                          "softAP(", "apStartMs =", "apExtendedStartMs ="):
            self.assertNotIn(forbidden, client)
        self.assertIn("WiFi.mode(WIFI_OFF);", client)
        self.assertIn("WiFi.setAutoReconnect(previousAutoReconnect);", client)

    def test_discovery_budget_begins_before_association_and_is_recalculated(self):
        boot = section(read("PigletCollection.cpp"), "void collectionBoot()", '  status.stage = "listing";')
        self.assertLess(boot.index("const uint32_t started = millis();"), boot.index("WiFi.mode(WIFI_STA)"))
        self.assertIn("constexpr uint32_t DISCOVERY_MS = 10000;", read("PigletCollection.cpp"))
        self.assertGreaterEqual(boot.count("remainingBudget(started, millis(), DISCOVERY_MS)"), 4)
        self.assertNotIn("connectSTA(", boot)

    def test_collection_ack_handover_does_not_forge_external_upload_receipts(self):
        client = read("PigletCollection.cpp")
        self.assertIn("moveToUploaded(path, digest, status.size)", client)
        self.assertNotIn("deleteEmptyCsvs(", client)
        self.assertEqual(re.findall(r"SD\.remove\(([^)]+)\)", client),
                         ["temporary", "finalPath"])
        self.assertIn("validReceiptFor", client)
        cached = section(client, "if (!sourceItems && alreadyAcknowledged(digest, status.size))", "DynamicJsonDocument announce")
        self.assertLess(cached.index("file.close();"), cached.index("moveToUploaded("))
        self.assertNotIn("jsonRequest", cached)
        final = section(client, "free(chunk); file.close();", "void releaseForNormalBoot")
        self.assertLess(final.index("if (!complete) return false;"), final.index("moveToUploaded("))

    def test_inventory_precedes_unconfigured_early_exit_and_includes_archive(self):
        boot = read("PigletCollection.cpp")[read("PigletCollection.cpp").index("void collectionBoot()") :]
        self.assertLess(boot.index("collectionArchivePrepare()"), boot.index("!configured()"))
        archive = read("CollectionArchive.cpp")
        self.assertIn('for (const char* directory : {"/logs", "/uploaded"})', archive)
        self.assertIn('item["historical"] = historical || wasHistorical;', archive)
        self.assertIn('inventoryComplete && historicalPending == 0', archive)
        self.assertIn('cfg.collectionToken', archive)
        self.assertIn('if (!ready || activeNamespace != pairingNamespace()) return false;', archive)

    def test_legacy_mover_is_unchanged_after_uconsole_guard(self):
        current = section(read("SDUtils.cpp"), "bool moveToUploaded(const String& srcPath) {", "// ---- Collection receipt handover")
        original = section(baseline("SDUtils.cpp"), "bool moveToUploaded(const String& srcPath) {", "// ---- Log file")
        guard = '  if (collectionUConsoleMode()) return collectionArchiveLegacyMove(srcPath);\n'
        self.assertEqual(current.replace(guard, "", 1), original)

    def test_legacy_upload_api_and_schedule_are_unchanged_after_protection_guards(self):
        current = read("WigleUpload.cpp").replace('#include "CollectionArchive.h"\n', "", 1)
        current = current.replace('#include "PigletCollection.h"\n', "", 1)
        policy = section(current, "bool externalLogUploadsAllowed()", "// ---- Token test ----")
        current = current.replace(policy, "", 1)
        self.assertEqual(current.count("  if (externalLogUploadBlocked()) return false;\n"), 2)
        self.assertEqual(current.count("  if (externalLogUploadBlocked()) return 0;\n"), 2)
        current = current.replace("  if (externalLogUploadBlocked()) return false;\n", "")
        current = current.replace("  if (externalLogUploadBlocked()) return 0;\n", "")
        current = re.sub(r"^[ \t]*if \(!collectionArchive(?:CanDelete|RetentionEligible)\(path\)\) continue;\n", "", current, flags=re.M)
        guard = '      if (!collectionArchiveCanDelete(paths[i])) {\n        uploadDoneFiles++; updateOLED(0); continue;\n      }\n'
        self.assertEqual(current.count(guard), 2)
        self.assertEqual(current.replace(guard, ""), baseline("WigleUpload.cpp"))

    def test_collection_mover_never_overwrites_a_collision_or_uses_copy_fallback(self):
        mover = section(read("SDUtils.cpp"), "bool moveToUploaded(const String& srcPath, const String& expectedSha256", "bool collectionMatchesFile")
        self.assertNotIn("SD.remove(destination)", mover)
        self.assertNotIn("FILE_WRITE", mover)
        self.assertLess(mover.index("collectionArchivePlanMove(srcPath, destination"), mover.index("!SD.rename(srcPath, destination)"))
        self.assertIn("collectionArchiveMoved(srcPath, destination", mover)
        self.assertIn("enforceUploadedCsvRetention();", mover)

    def test_missing_collector_never_hashes_the_archive_before_normal_boot(self):
        client = read("PigletCollection.cpp")
        boot = client[client.index("void collectionBoot()") :]
        before_hello_result = boot[:boot.index("if (!paired)")]
        self.assertNotIn("collectionArchiveRecover()", before_hello_result)
        self.assertNotIn("collectionArchiveRefresh(true)", before_hello_result)
        self.assertLess(boot.index("if (!paired)"), boot.index("collectionArchiveRecover()"))
        prepare = section(read("CollectionArchive.cpp"), "bool collectionArchivePrepare()", "String collectionArchiveCollector()")
        self.assertNotIn("collectionMatchesFile", prepare)
        self.assertNotIn("collectionDigestFile", prepare)
        self.assertIn("collectionArchiveRefresh();", prepare)
        self.assertIn("bool verifyBodies = false", read("CollectionArchive.h"))

    def test_retention_only_hashes_a_file_selected_for_actual_deletion(self):
        sd = read("SDUtils.cpp")
        candidate = section(sd, "static bool isUploadedCsvRetentionCandidate", "void enforceUploadedCsvRetention()")
        self.assertIn("collectionArchiveRetentionEligible(path)", candidate)
        self.assertNotIn("collectionArchiveCanDelete(path)", candidate)
        retention = section(sd, "void enforceUploadedCsvRetention()", "// ---- Move to uploaded")
        self.assertLess(retention.index("collectionArchiveCanDelete(path)"), retention.index("SD.remove(path)"))
        self.assertLess(retention.index("for (size_t i = 0; i < deleteTarget"), retention.index("collectionArchiveCanDelete(path)"))

    def test_source_item_metadata_is_negotiated_and_does_not_bypass_offers(self):
        client = read("PigletCollection.cpp")
        self.assertIn('sourceItems = reply["source_items"].is<unsigned>()', client)
        self.assertIn('if (sourceItems) begin["source_items"] = 1;', client)
        self.assertIn('Collector did not confirm source-item metadata; data retained', client)
        transfer = section(client, "bool transferFile(", "void releaseForNormalBoot")
        self.assertIn("if (!sourceItems && alreadyAcknowledged(digest, status.size))", transfer)
        self.assertIn('announce["source_id"] = sourceId;', transfer)
        self.assertIn('bytesDigest(reinterpret_cast<const uint8_t*>(path.c_str()), path.length())', transfer)
        self.assertLess(transfer.index('jsonRequest("POST", "/pc/v1/file"'), transfer.index('if (retiredSnapshot) break;'))
        self.assertLess(transfer.index('reply["state"] == "committed"'), transfer.index('if (retiredSnapshot) break;'))

    def test_expired_source_metadata_requires_durable_receipt_and_handover(self):
        helper = section(read("CollectionArchive.cpp"), "bool collectionArchiveDeliveredSnapshot(", "bool collectionArchiveLegacyMove(")
        self.assertIn("SD.exists(path)", helper)
        self.assertIn("knownReceipt(item)", helper)
        self.assertIn('item["handover_done"]', helper)
        self.assertNotIn("SD.remove(", helper)
        self.assertNotIn("moveToUploaded(", helper)

    def test_announced_sources_survive_reset_and_only_clear_on_confirmed_finish(self):
        client = read("PigletCollection.cpp")
        boot = client[client.index("void collectionBoot()") :]
        self.assertIn("collectionArchiveList(files, true, sourceItems)", boot)
        self.assertLess(boot.index("collectionArchiveAnnounceSources(files)"), boot.index('jsonRequest("POST", "/pc/v1/begin"'))
        self.assertIn('code == 200 && reply["finished"].is<bool>() && reply["finished"].as<bool>()', boot)
        self.assertIn('!sourceItems || (finished && collectionArchiveFinishSources(files))', boot)
        archive = read("CollectionArchive.cpp")
        restore = section(archive, "bool sourceObligations(", "} // namespace")
        self.assertIn('item["source_obligation"]', restore)
        self.assertIn('item["source_collector"] == currentCollector', restore)
        self.assertNotIn("SD.exists(path)", restore)
        registration = section(archive, "bool registerPath(", "bool knownReceipt(")
        self.assertIn('item["source_obligation"] = true;', registration)
        self.assertIn('item["source_collector"] = sourceCollector;', registration)

    def test_large_active_import_retains_per_request_timeouts_without_total_processing_deadline(self):
        client = read("PigletCollection.cpp")
        self.assertNotIn("IMPORT_WAIT_MS", client)
        self.assertNotIn("waitStarted", client)
        self.assertIn("constexpr uint32_t REQUEST_MS = 5000;", client)
        self.assertIn("constexpr unsigned RETRIES = 3;", client)
        self.assertIn("while (WiFi.status() == WL_CONNECTED)", client)
        self.assertIn('jsonRequest("GET", "/pc/v1/status?id=" + digest', client)
        self.assertIn('if (reply["state"] != "processing")', client)


if __name__ == "__main__":
    unittest.main(verbosity=2)
