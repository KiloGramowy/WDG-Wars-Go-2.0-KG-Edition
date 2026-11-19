"""A reported JanOS version never grants a KG/SOLO label by itself."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_flash_preflight import FlashEnvironment


class FirmwareDisplayTests(unittest.TestCase):
    def test_startup_reports_janos_without_claiming_solo_and_preserves_raw_log(self):
        for line in ("JanOS version: 1.7.0", "WatchDogsGo version: v1.7.0",
                     "APP_MAIN START (v1.7.0)", "pong v1.7.0"):
            with self.subTest(reply=line):
                env=FlashEnvironment(allow_upstream=False)
                self.addCleanup(env.close)
                game=env.game
                game._fw_version=""
                game._fw_update_available=False
                game._fw_remote_version=""
                game.loot=SimpleNamespace(log_serial=Mock())
                game._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,
                    handle_line=lambda reply:True)
                game._handle_serial_line(line)
                self.assertEqual(game._fw_version,"1.7.0")
                self.assertTrue(any("[FW] JanOS 1.7.0" in text for text in env.output))
                self.assertFalse(any("Firmware KG 1.7.0" in text for text in env.output))
                game.loot.log_serial.assert_called_once_with(line)
                self.assertFalse(game._fw_update_available)
                self.assertEqual(game._fw_remote_version,"")
                self.assertEqual(len(env.threads),1)
                self.assertTrue(env.threads[0].daemon)
                self.assertEqual(env.events,[('thread-start',)])
                self.assertEqual(env.kg_calls,[])


if __name__=="__main__":
    unittest.main()
