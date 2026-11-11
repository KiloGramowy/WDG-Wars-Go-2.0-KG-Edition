"""Retention ledger regressions; temporary files only, no actual logs or HTTP."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from watchdogs.upload_retention import UploadedLogRetention


NOW = 1_700_000_000
SCOPE, RECEIPT, ACK = "a" * 64, "b" * 64, "c" * 64
FILE = {"session": "2026-11-06_12-00-00", "filename": "wardriving.csv", "digest": "d" * 64}


class UploadedLogRetentionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".uploaded_log_retention.json"
        self.now = NOW
        self.store = self.new()

    def new(self):
        return UploadedLogRetention(self.path, clock=lambda: self.now)

    def record(self, files=None, **extra):
        kwargs = dict(scope=SCOPE, receipt_key=RECEIPT, receipt_digest=ACK)
        kwargs.update(extra)
        return self.store.record_many([dict(FILE)] if files is None else files, **kwargs)

    def due(self):
        return self.store.due(now=self.now + 86400)

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_missing_state_defaults_keep_all_and_does_not_write(self):
        self.assertEqual(self.store.days, 0)
        self.assertEqual(self.store.due(), [])
        self.assertFalse(self.path.exists())

    def test_policy_and_proof_survive_restart_with_explicit_schema(self):
        self.assertTrue(self.store.set_days(5))
        self.assertTrue(self.record())
        raw = self.state()
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["policy"], {"days": 5})
        self.assertEqual(len(raw["proofs"]), 1)
        fresh = self.new()
        self.assertEqual(fresh.days, 5)
        self.assertEqual(fresh.due(now=NOW + 5 * 86400)[0]["digest"], FILE["digest"])

    def test_presets_custom_and_keep_all_are_valid(self):
        for value in [0, 1, 5, 15, 31, 2, 366, 999999, 0]:
            with self.subTest(days=value):
                self.assertTrue(self.store.set_days(value))
                self.assertEqual(self.store.days, value)
                self.assertEqual(self.new().days, value)

    def test_invalid_policies_never_create_state(self):
        for value in [-1, 1000000, True, False, 1.0, "5", None, float("nan")]:
            with self.subTest(days=value):
                self.assertFalse(self.store.set_days(value))
                self.assertEqual(self.store.days, 0)
                self.assertFalse(self.path.exists())

    def test_keep_all_still_records_proofs_without_making_them_due(self):
        self.assertTrue(self.record())
        self.assertEqual(self.store.days, 0)
        self.assertEqual(self.store.due(now=NOW + 100 * 86400), [])
        self.assertTrue(self.store.set_days(5))
        self.assertEqual(len(self.store.due(now=NOW + 5 * 86400)), 1)

    def test_due_boundary_and_clock_rollback(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        self.assertEqual(self.store.due(now=NOW + 86400 - 0.001), [])
        self.assertEqual(len(self.store.due(now=NOW + 86400)), 1)
        self.assertEqual(self.store.due(now=NOW - 100), [])
        self.now = NOW - 100
        self.assertEqual(self.store.due(), [])

    def test_repeated_ack_does_not_refresh_first_confirmation(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        original = self.path.read_bytes()
        self.now += 1000
        self.assertTrue(self.record())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.due()[0]["confirmed_at"], NOW)

    def test_new_receipt_key_and_ack_hash_preserve_file_confirmation_age(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        self.now += 100
        self.assertTrue(self.record(receipt_key="e" * 64, receipt_digest="f" * 64))
        proof = self.due()[0]
        self.assertEqual(proof["confirmed_at"], NOW)
        self.assertEqual(proof["receipt_key"], "e" * 64)
        self.assertEqual(proof["receipt_digest"], "f" * 64)

    def test_changed_content_or_scope_starts_new_confirmation_age(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        self.now += 100
        self.assertTrue(self.record([dict(FILE, digest="e" * 64)]))
        self.assertEqual(self.store.due(now=NOW + 86400), [])
        self.assertEqual(self.state()["proofs"][0]["confirmed_at"], NOW + 100)
        self.now += 100
        self.assertTrue(self.record([dict(FILE, digest="e" * 64)], scope="f" * 64))
        self.assertEqual(self.state()["proofs"][0]["confirmed_at"], NOW + 200)

    def test_multiple_files_and_protocols_are_independent(self):
        files = [dict(FILE, filename=name) for name in
                 ["wardriving.csv", "adsb_aircraft.csv", "meshcore_nodes.csv", "meshtastic_nodes.csv"]]
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record(files))
        self.assertEqual({p["filename"] for p in self.due()}, {f["filename"] for f in files})

    def test_invalid_file_paths_and_filenames_never_create_state(self):
        sessions = ["", ".", "..", "../x", "/tmp/x", "x/y", "x\\y", "C:foo", " x", "x\x00"]
        for session in sessions:
            with self.subTest(session=session):
                self.assertFalse(self.record([dict(FILE, session=session)]))
                self.assertFalse(self.path.exists())
        for filename in ["loot_db.json", "../wardriving.csv", "Wardriving.csv", None]:
            self.assertFalse(self.record([dict(FILE, filename=filename)]))
            self.assertFalse(self.path.exists())

    def test_invalid_fingerprints_and_clock_values_are_rejected(self):
        for field in ["scope", "receipt_key", "receipt_digest"]:
            for value in ["", "a" * 63, "x" * 64, None, True]:
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.record(**{field: value}))
        self.assertFalse(self.record([dict(FILE, digest="x" * 64)]))
        for value in [0, -1, True, float("nan"), float("inf"), "1700000000"]:
            self.now = value
            self.assertFalse(self.record())
            self.assertFalse(self.path.exists())

    def test_batch_validation_is_atomic_and_rejects_duplicate_identity(self):
        self.assertTrue(self.record())
        before = self.path.read_bytes()
        self.assertFalse(self.record([dict(FILE, digest="e" * 64), dict(FILE, session="../bad")]))
        self.assertFalse(self.record([dict(FILE), dict(FILE)]))
        self.assertEqual(self.path.read_bytes(), before)

    def test_input_and_due_records_are_independent_copies(self):
        files = [dict(FILE)]
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record(files))
        files[0]["digest"] = "e" * 64
        proof = self.due()[0]
        proof["digest"] = "f" * 64
        self.assertEqual(self.due()[0]["digest"], FILE["digest"])

    def test_forget_is_durable_and_does_not_remove_a_newer_proof(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        old = self.due()[0]
        self.now += 1
        self.assertTrue(self.record([dict(FILE, digest="e" * 64)]))
        self.assertFalse(self.store.forget(old))
        current = self.store.due(now=self.now + 86400)[0]
        self.assertTrue(self.store.forget(current))
        self.assertEqual(self.new().due(now=self.now + 100 * 86400), [])
        self.assertFalse(self.store.forget(current))

    def test_corrupt_or_unsupported_state_is_kept_and_never_authorizes_deletion(self):
        for raw in ["{", "null", '[]', '{"version":2,"policy":{"days":1},"proofs":[]}',
                    '{"version":true,"policy":{"days":1},"proofs":[]}',
                    '{"version":1,"policy":{"days":true},"proofs":[]}']:
            self.path.write_text(raw, encoding="utf-8")
            store = self.new()
            self.assertEqual(store.days, 0)
            self.assertEqual(store.due(now=NOW + 86400), [])
            self.assertFalse(store.set_days(1))
            self.assertFalse(store.record_many([FILE], scope=SCOPE, receipt_key=RECEIPT, receipt_digest=ACK))
            self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_corruption_after_loading_is_detected_before_deletion_or_write(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        self.path.write_text("broken ledger", encoding="utf-8")
        self.assertEqual(self.due(), [])
        self.assertFalse(self.record())
        self.assertFalse(self.store.set_days(5))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "broken ledger")

    def test_future_confirmation_is_retained_but_not_due_during_clock_rollback(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        self.now = NOW - 100000
        fresh = self.new()
        self.assertEqual(fresh.days, 1)
        self.assertEqual(fresh.due(), [])
        self.assertEqual(len(fresh.due(now=NOW + 86400)), 1)

    def test_invalid_loaded_proof_values_fail_closed(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        valid = self.state()
        for field, value in [("confirmed_at", True), ("confirmed_at", 0), ("confirmed_at", float("nan")),
                             ("confirmed_at", float("inf")), ("scope", "bad"),
                             ("receipt_digest", "bad"), ("digest", "bad"), ("session", "../bad")]:
            state = copy.deepcopy(valid)
            state["proofs"][0][field] = value
            self.path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(self.new().due(now=NOW + 86400), [], (field, value))
        state = copy.deepcopy(valid)
        state["proofs"].append(dict(state["proofs"][0]))
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.new().due(now=NOW + 86400), [])

    def test_failed_save_preserves_memory_and_previous_bytes(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        before = self.path.read_bytes()
        for target in ["os.fsync", "os.replace"]:
            with self.subTest(failure=target), patch("watchdogs.upload_retention." + target, side_effect=OSError("synthetic")):
                self.assertFalse(self.store.set_days(15))
                self.assertFalse(self.record([dict(FILE, digest="e" * 64)]))
                self.assertFalse(self.store.forget(self.due()[0]))
            self.assertEqual(self.store.days, 1)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.due()[0]["digest"], FILE["digest"])
            self.assertEqual(list(self.path.parent.glob(".uploaded-log-retention-*")), [])

    def test_external_valid_replacement_is_loaded_before_write(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        other = self.new()
        self.assertTrue(other.set_days(31))
        self.assertTrue(self.record([dict(FILE, filename="adsb_aircraft.csv")]))
        self.assertEqual(self.store.days, 31)
        self.assertEqual(len(self.state()["proofs"]), 2)

    def test_deleted_state_resets_to_keep_all_without_resurrecting_proofs(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        self.path.unlink()
        self.assertEqual(self.due(), [])
        self.assertEqual(self.store.days, 0)
        self.assertTrue(self.record([dict(FILE, filename="adsb_aircraft.csv")]))
        self.assertEqual(len(self.state()["proofs"]), 1)

    def test_policy_getter_is_memory_only(self):
        self.assertTrue(self.store.set_days(15))
        with patch.object(Path, "lstat", side_effect=AssertionError("UI must not stat per frame")), \
             patch.object(Path, "read_text", side_effect=AssertionError("UI must not read per frame")):
            self.assertEqual(self.store.days, 15)

    def test_invalid_due_times_never_authorize_deletion(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        for value in [True, 0, -1, float("nan"), float("inf"), "tomorrow"]:
            self.assertEqual(self.store.due(now=value), [])

    def test_clock_failure_does_not_record_or_authorize_deletion(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        before = self.path.read_bytes()
        with patch.object(self.store, "_clock", side_effect=RuntimeError("unavailable clock")):
            self.assertFalse(self.record([dict(FILE, digest="e" * 64)]))
            self.assertEqual(self.store.due(), [])
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX mode and owner contract")
    def test_written_state_is_private_and_preserves_existing_owner(self):
        self.assertTrue(self.store.set_days(1))
        owner = self.path.stat().st_uid, self.path.stat().st_gid
        os.chmod(self.path, 0o644)
        self.assertTrue(self.record())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path.stat().st_uid, self.path.stat().st_gid), owner)

    @unittest.skipIf(os.name == "nt", "POSIX read-only mode including root")
    def test_read_only_file_and_parent_never_authorize_deletion(self):
        self.assertTrue(self.store.set_days(1))
        self.assertTrue(self.record())
        proof = self.due()[0]
        os.chmod(self.path, 0o400)
        self.addCleanup(os.chmod, self.path, 0o600)
        self.assertEqual(self.due(), [])
        self.assertFalse(self.store.set_days(5))
        self.assertFalse(self.record())
        self.assertFalse(self.store.forget(proof))
        os.chmod(self.path, 0o600)
        os.chmod(self.path.parent, 0o500)
        try:
            self.assertEqual(self.due(), [])
            self.assertFalse(self.store.set_days(5))
        finally:
            os.chmod(self.path.parent, 0o700)

    def test_symlink_state_is_never_followed_or_replaced(self):
        target = self.path.parent / "unrelated.json"
        target.write_text("untouched", encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        store = self.new()
        self.assertEqual(store.due(now=NOW + 86400), [])
        self.assertFalse(store.set_days(1))
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
