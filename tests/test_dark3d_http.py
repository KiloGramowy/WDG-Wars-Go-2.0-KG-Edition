"""Synthetic Dark3D files over the unchanged PC1 loopback transport; no hardware."""
import csv
import io
import time
import unittest

import test_piglet_http as fixtures
from test_piglet_collection import contents, digest, row
from watchdogs.piglet_http import signed_receipt


HEADER = ("MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
          "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type")
WIFI = ["02:00:00:00:10:01", "Synthetic, Dark3D", "[WPA2]", "2026-11-13 10:00:00",
        "36", "-62", "51.1", "17.2", "120", "2.5", "WIFI"]
BLE = ["02:00:00:00:10:02", "Synthetic BLE", "[LE Random]", "2026-11-13 10:00:01",
       "0", "-75", "51.1", "17.2", "120", "2.5", "BLE"]


def log_bytes(rows, release="synthetic-fixture"):
    output = io.StringIO(newline="")
    output.write("WigleWifi-1.4,appRelease=" + release + "\n")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class Dark3DHTTPTests(unittest.TestCase):
    # Reuse only the existing loopback fixture helpers, not its test methods.
    setUp = fixtures.PigletHTTPTests.setUp
    close_loot = fixtures.PigletHTTPTests.close_loot
    call = fixtures.PigletHTTPTests.call

    def begin(self, pending=1, device="dark3d-123456789abc"):
        status, body = self.call("POST", "/pc/v1/begin", dict(device_id=device, pending_files=pending))
        self.assertEqual(status, 200)
        return body["session_id"]

    def offer_and_send(self, session, name, data, *, retry_first_chunk=False):
        sha = digest(data)
        status, state = self.call("POST", "/pc/v1/file", dict(name=name, size=len(data), sha256=sha), session=session)
        self.assertEqual(status, 200)
        if state["state"] == "receiving":
            for offset in range(state["offset"], len(data), 16384):
                part = data[offset:offset+16384]
                headers = {"X-Chunk-SHA256": digest(part)}
                status, reply = self.call("PUT", f"/pc/v1/chunk?id={sha}&offset={offset}", part,
                                          session=session, headers=headers)
                self.assertEqual((status, reply["offset"]), (200, offset+len(part)))
                if retry_first_chunk and offset == 0:
                    status, retry = self.call("PUT", f"/pc/v1/chunk?id={sha}&offset=0", part,
                                             session=session, headers=headers)
                    self.assertEqual((status, retry["offset"]), (200, len(part)))
        return sha

    def settle(self, session, sha):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            result = self.store.process_pending(self.xp, self.wifi, self.ble)
            if result["committed"]:
                self.xp = result["xp_total"]
            self.assertTrue(self.store.acknowledge_events([e["event_id"] for e in self.store.pending_events(20)]))
            status, state = self.call("GET", "/pc/v1/status?id="+sha, session=session)
            self.assertEqual(status, 200)
            if state["state"] in ("committed", "error"):
                return state
            time.sleep(.005)
        self.fail("Loopback import did not settle")

    def test_log_ack_retries_and_repacked_duplicates_do_not_repeat_xp(self):
        session = self.begin(2)
        data = log_bytes([WIFI, BLE])
        sha = self.offer_and_send(session, "original.log", data, retry_first_chunk=True)
        status, pending = self.call("POST", "/pc/v1/commit", dict(sha256=sha), session=session)
        self.assertEqual(status, 202)
        self.assertNotIn("receipt", pending)
        self.assertEqual(self.loot.load_xp(), 100)
        committed = self.settle(session, sha)
        self.assertEqual(committed["state"], "committed")
        self.assertEqual(self.xp, 125)
        receipt = committed["receipt"]
        self.assertEqual(receipt["signature"], signed_receipt(receipt, fixtures.TOKEN)["signature"])
        self.assertEqual((receipt["records"], receipt["wifi"], receipt["ble"], receipt["duplicates"]), (2, 1, 1, 0))
        status, retry = self.call("POST", "/pc/v1/file", dict(name="renamed.log", size=len(data), sha256=sha), session=session)
        self.assertEqual(status, 200)
        self.assertEqual(retry["receipt"], receipt)
        self.assertEqual(self.xp, 125)
        repacked = log_bytes([BLE, WIFI], release="synthetic-repacked")
        repacked_sha = self.offer_and_send(session, "overlap.log", repacked)
        self.assertNotEqual(repacked_sha, sha)
        self.call("POST", "/pc/v1/commit", dict(sha256=repacked_sha), session=session)
        repeated = self.settle(session, repacked_sha)
        self.assertEqual(repeated["state"], "committed")
        self.assertEqual(repeated["receipt"]["duplicates"], 2)
        self.assertEqual((self.xp, self.loot.load_xp()), (125, 125))
        self.assertEqual(self.store._path(sha).suffix, ".log")
        self.assertEqual(self.store._path(sha).read_bytes(), data)

    def test_legacy_piglet_and_dark3d_use_same_unchanged_protocol(self):
        status, hello = self.call("GET", "/pc/v1/hello")
        self.assertEqual(status, 200)
        self.assertEqual(hello, dict(protocol=1, collector_id=self.store.collector_id,
                                     max_chunk=16384, source_items=1,
                                     device_types=["piglet-kg", "dark3d-c5"]))
        piglet = self.begin(device="piglet-123456789abc")
        piglet_data = contents([row()])
        piglet_sha = self.offer_and_send(piglet, "legacy.csv", piglet_data)
        self.call("POST", "/pc/v1/commit", dict(sha256=piglet_sha), session=piglet)
        piglet_state = self.settle(piglet, piglet_sha)
        self.assertEqual(piglet_state["state"], "committed")
        previous_xp = self.xp
        dark3d = self.begin()
        data = log_bytes([WIFI])
        sha = self.offer_and_send(dark3d, "dark3d.log", data)
        self.call("POST", "/pc/v1/commit", dict(sha256=sha), session=dark3d)
        state = self.settle(dark3d, sha)
        self.assertEqual(state["state"], "committed")
        self.assertEqual(self.xp, previous_xp+15)
        self.assertEqual(self.store._path(piglet_sha).suffix, ".csv")
        self.assertEqual(self.store._path(piglet_sha).read_bytes(), piglet_data)
        self.assertEqual(state["receipt"]["signature"], signed_receipt(state["receipt"], fixtures.TOKEN)["signature"])

    def test_malformed_log_retained_without_ack_or_xp(self):
        session = self.begin()
        data = log_bytes([WIFI[:-1]])
        sha = self.offer_and_send(session, "invalid.log", data)
        self.call("POST", "/pc/v1/commit", dict(sha256=sha), session=session)
        state = self.settle(session, sha)
        self.assertEqual(state["state"], "error")
        self.assertNotIn("receipt", state)
        self.assertEqual((self.xp, self.loot.load_xp()), (100, 100))
        self.assertEqual(self.store._path(sha).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
