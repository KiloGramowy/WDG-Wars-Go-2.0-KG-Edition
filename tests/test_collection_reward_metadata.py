"""Atomic Collection accounting provenance, with temporary player data only."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_piglet_collection import row
from watchdogs.loot_manager import LootManager
from watchdogs.piglet_csv import parse_record


class CollectionRewardMetadataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            self.loot = LootManager(self.folder.name)
        self.addCleanup(self.close_loot)
        self.assertTrue(self.loot.save_xp(100))
        self.wifi, self.ble = set(), set()

    def close_loot(self):
        with patch.object(os, "sync", lambda: None, create=True):
            self.loot.close()

    def commit(self, entries, **kwargs):
        return self.loot.commit_collection_batch("a" * 32, "b" * 64, entries,
                                                 100, self.wifi, self.ble, **kwargs)

    def test_new_identity_and_each_base_reward_are_in_the_durable_recovery_marker(self):
        first = parse_record(row())
        repeat = parse_record(row(rssi="-50"))
        ble = parse_record(row("02:00:00:00:00:02", "BLE"))
        records = [first, repeat, ble]
        entries = [dict(ordinal=i, record=r) for i, r in enumerate(records, 1)]
        original = copy.deepcopy(entries)
        result = self.commit(entries, source_job_id="c" * 32)
        self.assertEqual(result["xp_total"], 126)
        marker = self.loot.collection_pending_batches()["a" * 32]
        self.assertEqual([r["new_identity"] for r in marker["entries"]], [True, False, True])
        self.assertEqual([r["base_xp"] for r in marker["entries"]], [15, 1, 10])
        self.assertEqual(marker["source_job_id"], "c" * 32)
        events = self.loot.collection_pending_events()
        self.assertEqual([r["source_file_sha256"] for r in events], ["b" * 64] * 3)
        self.assertEqual([r["source_job_id"] for r in events], ["c" * 32] * 3)
        self.assertEqual([r["record"] for r in events], records)
        self.assertEqual(entries, original)
        self.assertEqual(self.loot.load_xp(), 126)
        self.assertEqual((len(self.wifi), len(self.ble)), (1, 1))

    def test_existing_identity_gets_only_unchanged_repeat_xp_and_is_not_new(self):
        record = parse_record(row())
        self.wifi.add(record["mac"])
        result = self.commit([dict(ordinal=1, record=record)])
        self.assertEqual(result["xp_total"], 101)
        item = self.loot.collection_pending_batches()["a" * 32]["entries"][0]
        self.assertEqual((item["new_identity"], item["base_xp"]), (False, 1))
        self.assertNotIn("source_job_id", self.loot.collection_pending_events()[0])

    def test_failed_commit_changes_neither_canonical_data_nor_known_identities(self):
        record = parse_record(row())
        before = copy.deepcopy(self.loot._db)
        with patch.object(self.loot, "_save_db", return_value=False):
            with self.assertRaises(OSError):
                self.commit([dict(ordinal=1, record=record)], source_job_id="c" * 32)
        self.assertEqual(self.loot._db, before)
        self.assertEqual((self.wifi, self.ble), (set(), set()))
        self.assertEqual(self.loot.collection_pending_batches(), {})
        self.assertEqual(self.loot.collection_pending_events(), [])

    def test_recovery_marker_and_outbox_provenance_survive_a_real_database_reload(self):
        record = parse_record(row())
        self.commit([dict(ordinal=1, record=record)], source_job_id="c" * 32)
        with patch.object(LootManager, "_periodic_backup_loop", lambda _self: None):
            restored = LootManager(self.folder.name)
        try:
            self.assertEqual(restored.collection_pending_events(), self.loot.collection_pending_events())
            self.assertEqual(restored.collection_pending_batches(), self.loot.collection_pending_batches())
            self.assertEqual(restored.load_xp(), 115)
        finally:
            with patch.object(os, "sync", lambda: None, create=True):
                restored.close()

    def test_unsupported_job_identifiers_are_rejected_before_any_change(self):
        before = copy.deepcopy(self.loot._db)
        for value in ("", "../not-a-job", True, "c" * 31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.commit([dict(ordinal=1, record=parse_record(row()))], source_job_id=value)
        self.assertEqual(self.loot._db, before)


if __name__ == "__main__":
    unittest.main()
