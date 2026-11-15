"""Synthetic historical inventories over real loopback PC1, never a C5 scan.

The source labels below model distinct inventory entries. They do not assert
Dark3D directory names, filesystem metadata, firmware inventory behavior or a
physical migration. Original Piglet CSV and Dark3D LOG schemas stay separate.
"""
import time
import unittest
from unittest.mock import patch

import test_dark3d_http as dark_http
from test_piglet_collection import contents as piglet_bytes, digest, row as piglet_row
from watchdogs.loot_manager import LootManager
from watchdogs.piglet_collection import MAX_CHUNK, PigletCollectionStore
from watchdogs.piglet_http import PigletHTTPService, signed_receipt


DEVICE = "synthetic-history-dark3d-001"
TOKEN = dark_http.fixtures.TOKEN


def source_id(label):
    """Stable synthetic identities, not discovered physical source paths."""
    return digest(("synthetic-history-inventory:" + label).encode("ascii"))


class Dark3DHistoryTests(unittest.TestCase):
    def setUp(self):
        # Composition reuses setup/transport helpers without discovering or
        # inheriting any of the existing fixture's test methods a second time.
        self.transport = dark_http.Dark3DHTTPTests()
        self.transport.setUp()
        self.addCleanup(self.transport.doCleanups)

    def call(self, *args, **kwargs):
        return self.transport.call(*args, **kwargs)

    def begin(self, pending):
        status, result = self.call("POST", "/pc/v1/begin", dict(
            device_id=DEVICE, pending_files=pending, source_items=1))
        self.assertEqual(status, 200)
        self.assertEqual(result["source_items"], 1)
        return result["session_id"]

    def overview(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.transport.store.status_overview()
            if not result.get("busy"):
                return result["import"]
            time.sleep(.005)
        self.fail("Synthetic historical inventory overview remained busy")

    def offer(self, session, label, name, data):
        sha = digest(data)
        status, result = self.call("POST", "/pc/v1/file", dict(
            name=name, size=len(data), sha256=sha, source_id=source_id(label)), session=session)
        self.assertEqual(status, 200)
        self.assertEqual(result["sha256"], sha)
        return sha, result

    def send(self, session, label, name, data):
        sha, offered = self.offer(session, label, name, data)
        if offered["state"] == "receiving":
            for offset in range(offered["offset"], len(data), MAX_CHUNK):
                part = data[offset:offset + MAX_CHUNK]
                status, result = self.call("PUT", f"/pc/v1/chunk?id={sha}&offset={offset}", part,
                    session=session, headers={"X-Chunk-SHA256": digest(part)})
                self.assertEqual((status, result["offset"]), (200, offset + len(part)))
        return sha

    def settle(self, session, sha):
        status, result = self.call("POST", "/pc/v1/commit", dict(sha256=sha), session=session)
        self.assertIn(status, (200, 202))
        result = self.transport.settle(session, sha)
        self.assertEqual(result["state"], "committed")
        self.assertEqual(result["receipt"], signed_receipt(result["receipt"], TOKEN))
        return result["receipt"]

    def finish(self, session):
        status, result = self.call("POST", "/pc/v1/finish", {}, session=session)
        self.assertEqual(status, 200)
        return result

    def assert_unfinished(self, session):
        result = self.finish(session)
        self.assertFalse(result["finished"])
        self.assertFalse(result["shutdown_allowed"])
        self.assertLess(self.overview()["percent"], 100)
        return result

    def restart_receiver_and_loot(self):
        fixture = self.transport
        self.assertTrue(fixture.service.stop())
        self.assertTrue(fixture.store.close())
        fixture.close_loot()
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            fixture.loot = LootManager(fixture.folder.name)
        fixture.store = PigletCollectionStore(fixture.loot)
        fixture.addCleanup(fixture.store.close)
        fixture.service = PigletHTTPService(fixture.store, TOKEN, "127.0.0.1", 0,
            on_finish=fixture.notifications.append, allow_loopback=True,
            max_clients=2, request_timeout=1.0)
        fixture.service.start()
        fixture.addCleanup(fixture.service.stop)
        fixture.xp = fixture.loot.load_xp()
        fixture.ble, fixture.wifi = fixture.loot.get_known_devices()

    def test_external_upload_label_does_not_authorize_collection_ack_or_completion(self):
        session = self.begin(1)
        data = dark_http.log_bytes([dark_http.WIFI], release="synthetic-historical-file")
        self.assert_unfinished(session)
        sha, offered = self.offer(session, "previous-service-upload", "synthetic-already-externally-uploaded.log", data)
        self.assertEqual(offered["state"], "receiving")
        self.assertNotIn("receipt", offered)
        self.assertEqual(self.transport.loot.load_xp(), 100)
        self.assert_unfinished(session)
        self.send(session, "previous-service-upload", "synthetic-already-externally-uploaded.log", data)
        status, pending = self.call("POST", "/pc/v1/commit", dict(sha256=sha), session=session)
        self.assertEqual(status, 202)
        self.assertNotIn("receipt", pending)
        self.assert_unfinished(session)
        receipt = self.settle(session, sha)
        self.assertEqual((receipt["records"], self.transport.xp), (1, 115))
        self.assertTrue(self.finish(session)["shutdown_allowed"])
        self.assertEqual(self.transport.store._path(sha).read_bytes(), data)

    def test_complete_inventory_counts_aliases_and_mixed_original_schemas_without_repaying(self):
        session = self.begin(4)
        dark_a = dark_http.log_bytes([dark_http.WIFI], release="synthetic-old-a")
        dark_b = dark_http.log_bytes([dark_http.BLE], release="synthetic-old-b")
        piglet = piglet_bytes([piglet_row(stamp="2020-01-02 03:04:05")])
        sha_a = self.send(session, "historical-a", "synthetic-old.log", dark_a)
        self.settle(session, sha_a)
        self.assertEqual((self.overview()["percent"], self.transport.xp), (25, 115))
        self.assert_unfinished(session)
        alias_sha, alias = self.offer(session, "previous-service-upload-alias", "synthetic-previously-uploaded.log", dark_a)
        self.assertEqual((alias_sha, alias["state"]), (sha_a, "committed"))
        self.assertEqual((self.overview()["percent"], self.transport.xp), (50, 115))
        self.assert_unfinished(session)
        sha_b = self.send(session, "historical-b", "synthetic-older.log", dark_b)
        self.settle(session, sha_b)
        self.assertEqual((self.overview()["percent"], self.transport.xp), (75, 125))
        self.assert_unfinished(session)
        sha_p = self.send(session, "piglet-original", "synthetic-old-piglet.csv", piglet)
        self.settle(session, sha_p)
        summary = self.overview()
        self.assertEqual((summary["expected_files"], summary["offered_files"], summary["completed_files"]), (4, 4, 4))
        self.assertEqual((summary["percent"], summary["base_xp"], self.transport.xp), (100, 40, 140))
        self.assertEqual(summary["duplicates"], 1)
        self.assertTrue(self.finish(session)["shutdown_allowed"])
        for sha, original in ((sha_a, dark_a), (sha_b, dark_b), (sha_p, piglet)):
            self.assertEqual(self.transport.store._path(sha).read_bytes(), original)

    def test_interrupted_batch_restarts_with_durable_prefix_and_all_remaining_obligations(self):
        first = self.begin(3)
        data_a = dark_http.log_bytes([dark_http.WIFI], release="synthetic-first-history")
        data_b = dark_http.log_bytes([dark_http.BLE], release="synthetic-interrupted-history")
        data_c = piglet_bytes([piglet_row(stamp="2020-03-04 05:06:07")])
        sha_a = self.send(first, "first", "synthetic-first.log", data_a)
        receipt_a = self.settle(first, sha_a)
        job_id = self.overview()["job_id"]
        sha_b, _ = self.offer(first, "second", "synthetic-second.log", data_b)
        prefix = data_b[:100]
        status, stored = self.call("PUT", f"/pc/v1/chunk?id={sha_b}&offset=0", prefix,
            session=first, headers={"X-Chunk-SHA256": digest(prefix)})
        self.assertEqual((status, stored["offset"]), (200, len(prefix)))
        self.assert_unfinished(first)
        self.restart_receiver_and_loot()
        self.assertEqual((self.overview()["job_id"], self.transport.xp), (job_id, 115))
        status, _ = self.call("POST", "/pc/v1/begin", dict(device_id=DEVICE, pending_files=0, source_items=1))
        self.assertEqual(status, 409)
        resumed = self.begin(2)
        _, offered = self.offer(resumed, "second", "synthetic-second.log", data_b)
        self.assertEqual(offered["offset"], len(prefix))
        self.assertNotIn("receipt", offered)
        self.assertEqual(self.transport.store._path(sha_b).read_bytes(), prefix)
        self.send(resumed, "second", "synthetic-second.log", data_b)
        self.settle(resumed, sha_b)
        summary = self.overview()
        self.assertEqual((summary["expected_files"], summary["completed_files"], summary["percent"]), (3, 2, 66))
        self.assert_unfinished(resumed)
        sha_c = self.send(resumed, "third", "synthetic-third.csv", data_c)
        self.settle(resumed, sha_c)
        self.assertEqual((self.overview()["job_id"], self.overview()["percent"], self.transport.xp), (job_id, 100, 140))
        status, old = self.call("GET", "/pc/v1/status?id=" + sha_a, session=first)
        self.assertEqual((status, old["receipt"]), (200, receipt_a))
        self.assertTrue(self.finish(resumed)["shutdown_allowed"])
        self.assertEqual(self.transport.store._path(sha_b).read_bytes(), data_b)

    def test_lost_ack_reoffers_and_later_history_job_never_pay_exact_observations_again(self):
        first = self.begin(2)
        data_a = dark_http.log_bytes([dark_http.WIFI], release="synthetic-lost-ack")
        data_b = dark_http.log_bytes([dark_http.WIFI, dark_http.BLE], release="synthetic-overlapping-history")
        sha_a = self.send(first, "a", "synthetic-a.log", data_a)
        saved_receipt = self.settle(first, sha_a)
        # Model a sender losing this final response, without changing durable
        # receiver state or claiming any physical radio/disconnection test.
        self.restart_receiver_and_loot()
        resumed = self.begin(2)
        _, replay = self.offer(resumed, "a", "synthetic-a.log", data_a)
        self.assertEqual(replay["receipt"], saved_receipt)
        self.assertEqual((self.overview()["offered_files"], self.overview()["percent"], self.transport.xp), (1, 50, 115))
        self.assert_unfinished(resumed)
        sha_b = self.send(resumed, "b", "synthetic-b.log", data_b)
        receipt_b = self.settle(resumed, sha_b)
        self.assertEqual((receipt_b["wifi"], receipt_b["ble"], receipt_b["duplicates"], self.transport.xp), (0, 1, 1, 125))
        self.assertTrue(self.finish(resumed)["shutdown_allowed"])
        previous_job = self.overview()["job_id"]
        next_job = self.begin(2)
        self.assertNotEqual(self.overview()["job_id"], previous_job)
        for label, name, data, receipt in (("a", "synthetic-a.log", data_a, saved_receipt),
                                           ("b", "synthetic-b.log", data_b, receipt_b)):
            _, result = self.offer(next_job, label, name, data)
            self.assertEqual(result["receipt"], receipt)
        summary = self.overview()
        self.assertEqual((summary["percent"], summary["new_wifi"], summary["new_ble"], summary["base_xp"], self.transport.xp),
                         (100, 0, 0, 0, 125))
        self.assertEqual(self.transport.store.job_file_shas(summary["job_id"]), [])
        self.assertTrue(self.finish(next_job)["shutdown_allowed"])

    def test_malformed_historical_file_remains_pending_and_cannot_complete_batch(self):
        session = self.begin(2)
        valid = dark_http.log_bytes([dark_http.WIFI], release="synthetic-valid-history")
        invalid = dark_http.log_bytes([dark_http.BLE[:-1]], release="synthetic-malformed-history")
        sha_a = self.send(session, "valid", "synthetic-valid.log", valid)
        self.settle(session, sha_a)
        sha_b = self.send(session, "invalid", "synthetic-previously-uploaded-invalid.log", invalid)
        self.call("POST", "/pc/v1/commit", dict(sha256=sha_b), session=session)
        state = self.transport.settle(session, sha_b)
        self.assertEqual(state["state"], "error")
        self.assertNotIn("receipt", state)
        self.assertEqual(self.transport.xp, 115)
        self.assertEqual(self.overview()["status"], "error")
        self.assert_unfinished(session)
        self.assertEqual(self.transport.store._path(sha_b).read_bytes(), invalid)

    def test_known_mac_keeps_repeat_rule_but_exact_paid_history_replay_scores_zero(self):
        self.transport.loot._db["known_device_identities"] = {"wifi": [dark_http.WIFI[0]], "ble": []}
        self.assertTrue(self.transport.loot._save_db(self.transport.loot._db))
        session = self.begin(2)
        first = dark_http.log_bytes([dark_http.WIFI], release="synthetic-known-identity")
        second = dark_http.log_bytes([dark_http.WIFI], release="synthetic-repacked-history")
        sha_a = self.send(session, "known-a", "synthetic-known-a.log", first)
        self.settle(session, sha_a)
        self.assertEqual((self.transport.xp, self.overview()["new_wifi"]), (101, 0))
        sha_b = self.send(session, "known-b", "synthetic-known-b.log", second)
        receipt = self.settle(session, sha_b)
        self.assertEqual((receipt["wifi"], receipt["duplicates"], self.transport.xp), (0, 1, 101))
        self.assertEqual(self.overview()["base_xp"], 1)
        self.assertTrue(self.finish(session)["shutdown_allowed"])

    def test_last_file_cannot_report_complete_before_durable_completion_receipt(self):
        session = self.begin(2)
        first = dark_http.log_bytes([dark_http.WIFI], release="synthetic-first-durable")
        last = dark_http.log_bytes([dark_http.BLE], release="synthetic-last-durable")
        sha_a = self.send(session, "durable-a", "synthetic-durable-a.log", first)
        self.settle(session, sha_a)
        sha_b = self.send(session, "durable-b", "synthetic-durable-b.log", last)
        self.call("POST", "/pc/v1/commit", dict(sha256=sha_b), session=session)
        with patch.object(self.transport.loot, "complete_collection_file", return_value=False):
            deadline = time.monotonic() + 5
            while self.transport.xp < 125 and time.monotonic() < deadline:
                result = self.transport.store.process_pending(self.transport.xp, self.transport.wifi, self.transport.ble)
                if result["committed"]:
                    self.transport.xp = result["xp_total"]
                time.sleep(.005)
            self.assertEqual(self.transport.xp, 125)
            status, state = self.call("GET", "/pc/v1/status?id=" + sha_b, session=session)
            self.assertEqual((status, state["state"]), (200, "processing"))
            self.assertNotIn("receipt", state)
            self.assert_unfinished(session)
        self.settle(session, sha_b)
        self.assertEqual((self.overview()["percent"], self.transport.xp, self.transport.loot.load_xp()), (100, 125, 125))
        self.assertTrue(self.finish(session)["shutdown_allowed"])


if __name__ == "__main__":
    unittest.main()
