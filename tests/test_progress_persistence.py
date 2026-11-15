"""Large progress documents round-trip; failed commits preserve the old save."""
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from watchdogs import loot_manager


class ProgressPersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="wdg-progress-test-")
        self.addCleanup(directory.cleanup)
        self.manager = loot_manager.LootManager.__new__(loot_manager.LootManager)
        self.manager._db_lock = threading.RLock()
        self.manager._db_path = Path(directory.name) / "loot_db.json"
        self.old = b'{"xp":12345,"extension":{"keep":"original"}}\n'
        self.manager._db_path.write_bytes(self.old)

    def test_large_extended_save_roundtrips_without_losing_player_fields(self):
        data = dict(xp=1234567, badges=["existing", "synthetic"],
                    extension={f"synthetic-{n}": dict(value=n, name="Żółw 🐷", enabled=True)
                               for n in range(20000)},
                    settings=dict(unknown=None, nested=[1, False, {"preserve": 1.25}]))
        before_mode = stat.S_IMODE(self.manager._db_path.stat().st_mode)
        self.assertTrue(self.manager._save_db(data))
        stored = self.manager._db_path.read_bytes()
        self.assertEqual(json.loads(stored), data)
        self.assertLess(len(stored), len(json.dumps(data, indent=2).encode()))
        self.assertEqual(stat.S_IMODE(self.manager._db_path.stat().st_mode), before_mode)
        self.assertFalse(self.manager._db_path.with_suffix(".tmp").exists())

    def test_failed_file_sync_does_not_replace_or_acknowledge_old_progress(self):
        with patch.object(loot_manager.os, "fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertLogs(loot_manager.log, level="ERROR"):
                self.assertFalse(self.manager._save_db(dict(xp=12399)))
        self.assertEqual(self.manager._db_path.read_bytes(), self.old)
        self.assertTrue(self.manager._save_db(dict(xp=12399)))
        self.assertEqual(json.loads(self.manager._db_path.read_bytes()), dict(xp=12399))

    def test_failed_atomic_replace_keeps_previous_bytes_and_retry_works(self):
        with patch.object(Path, "replace", side_effect=OSError("synthetic rename failure")):
            with self.assertLogs(loot_manager.log, level="ERROR"):
                self.assertFalse(self.manager._save_db(dict(xp=12400, preserve=[True, None])))
        self.assertEqual(self.manager._db_path.read_bytes(), self.old)
        self.assertTrue(self.manager._save_db(dict(xp=12400, preserve=[True, None])))
        self.assertEqual(json.loads(self.manager._db_path.read_bytes()),
                         dict(xp=12400, preserve=[True, None]))


if __name__ == "__main__":
    unittest.main()
