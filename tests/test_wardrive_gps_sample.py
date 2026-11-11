"""Measured wardrive GPS samples, without hardware, cache, or player data.

Exercise the real NMEA parser with fake time. RF AUTO keeps its existing,
separate GGA freshness and quality rules.
"""

import importlib
import math
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    with patch.dict(sys.modules, {"serial": SimpleNamespace()}):
        gps_module = importlib.import_module("watchdogs.gps_manager")
else:
    gps_module = importlib.import_module("watchdogs.gps_manager")


def nmea(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


def gga(utc="120000.00", lat="3627.6000", lon="00612.0000", quality="1",
        sats="08", hdop="0.9", altitude="10.0", ns="N", ew="W", talker="GP"):
    return nmea(f"{talker}GGA,{utc},{lat},{ns},{lon},{ew},{quality},{sats},{hdop},{altitude},M,0.0,M,,")


def rmc(utc="120000.00", status="A", lat="3627.6000", lon="00612.0000",
        ns="N", ew="W", talker="GP"):
    return nmea(f"{talker}RMC,{utc},{status},{lat},{ns},{lon},{ew},0.0,0.0,071126,,,A")


class WardriveGpsSampleTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.gps._available = True
        self.clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def feed(self, *sentences):
        self.gps.process_sentences(list(sentences))

    def sample(self):
        return self.gps.wardrive_sample(self.now)

    def test_gga_exposes_raw_quality_metrics_and_receive_time(self):
        self.feed(gga(sats="03", hdop="10.0", altitude="42.5", talker="GN"))
        value = self.sample()
        self.assertEqual(set(value), {"lat", "lon", "altitude", "quality", "sats",
                                     "hdop", "observed_at", "utc_seconds", "sentence"})
        self.assertAlmostEqual(value["lat"], 36.46)
        self.assertAlmostEqual(value["lon"], -6.2)
        self.assertEqual((value["quality"], value["sats"], value["hdop"], value["altitude"]),
                         (1, 3, 10.0, 42.5))
        self.assertEqual((value["observed_at"], value["utc_seconds"], value["sentence"]),
                         (100.0, 43200.0, "GGA"))
        self.assertIsNone(self.gps.radio_fix(self.now))
        self.assertTrue(self.gps.fix.valid)

    def test_valid_location_is_not_filtered_by_piglet_cache_or_radio_thresholds(self):
        for quality in range(1, 9):
            with self.subTest(quality=quality):
                self.feed(gga(utc=f"12000{quality}.00", quality=str(quality),
                              sats="02", hdop="12.5"))
                self.assertEqual((self.sample()["quality"], self.sample()["sats"],
                                  self.sample()["hdop"]), (quality, 2, 12.5))
                self.assertIsNone(self.gps.radio_fix(self.now))

    def test_rmc_alone_has_location_but_cannot_invent_gga_quality(self):
        self.feed(rmc(talker="GN"))
        value = self.sample()
        self.assertEqual(value["sentence"], "RMC")
        self.assertEqual((value["quality"], value["sats"], value["hdop"], value["altitude"]),
                         (0, 0, 99.9, 0.0))
        self.assertEqual(value["observed_at"], 100.0)
        self.assertIsNone(self.gps.radio_fix(self.now))

    def test_fresh_rmc_uses_its_position_and_latest_gga_metrics(self):
        self.feed(gga(altitude="42.5", sats="03", hdop="10"))
        self.now = 101.5
        self.feed(rmc(utc="120001.50", lat="3627.6060"))
        value = self.sample()
        self.assertAlmostEqual(value["lat"], 36.4601)
        self.assertEqual((value["observed_at"], value["utc_seconds"], value["sentence"]),
                         (101.5, 43201.5, "RMC"))
        self.assertEqual((value["quality"], value["sats"], value["hdop"], value["altitude"]),
                         (1, 3, 10.0, 42.5))
        self.assertIsNone(self.gps.radio_fix(self.now))

    def test_freshness_is_strictly_less_than_two_seconds_without_poll_refresh(self):
        self.feed(gga())
        for now in (100.0, 100.5, 101.999):
            self.assertEqual(self.gps.wardrive_sample(now)["observed_at"], 100.0)
        for now in (102.0, 103.0, 99.999, math.nan, math.inf):
            with self.subTest(now=now):
                self.assertIsNone(self.gps.wardrive_sample(now))
        self.assertTrue(self.gps.fix.valid)
        self.assertEqual(self.gps.radio_fix(102.0)["observed_at"], 100.0)

    def test_snapshot_is_a_copy_and_does_not_change_fix_or_radio_snapshot(self):
        self.feed(gga())
        radio_before = self.gps.radio_fix(self.now)
        value = self.sample()
        value.update(lat=90, hdop=99, observed_at=1000)
        self.assertAlmostEqual(self.sample()["lat"], 36.46)
        self.assertEqual(self.gps.radio_fix(self.now), radio_before)
        self.assertEqual((self.gps.fix.latitude, self.gps.fix.hdop), (36.46, 0.9))

    def test_gsv_or_display_field_changes_do_not_refresh_measured_sample(self):
        self.feed(gga())
        self.now = 102
        self.feed(nmea("GPGSV,1,1,12"))
        self.assertIsNone(self.sample())
        self.assertEqual(self.gps.fix.satellites_visible, 12)
        self.gps.fix.latitude = 40.0
        self.assertIsNone(self.sample())

    def test_new_received_rmc_does_not_refresh_auto_gga_observation(self):
        self.feed(gga())
        self.now = 101
        self.feed(rmc(utc="120001.00"))
        self.assertEqual(self.sample()["observed_at"], 101)
        self.assertEqual(self.gps.radio_fix(self.now)["observed_at"], 100)
        self.now = 102
        self.feed(gga(utc="120002.00"))
        self.assertEqual(self.sample()["observed_at"], 102)
        self.assertEqual(self.gps.radio_fix(self.now)["observed_at"], 102)

    def test_repeated_received_epoch_keeps_existing_radio_freshness_rule(self):
        self.feed(gga())
        self.now = 101
        self.feed(gga())
        self.assertEqual(self.sample()["observed_at"], 101)
        self.assertEqual(self.gps.radio_fix(self.now)["observed_at"], 100)

    def test_zero_quality_and_rmc_void_clear_sample_then_new_fix_recovers(self):
        for invalid in (gga(utc="120001.00", quality="0"),
                        rmc(utc="120001.00", status="V", lat="", lon="", ns="", ew="")):
            with self.subTest(invalid=invalid):
                self.gps = gps_module.GpsManager(device="never-open-test-device")
                self.gps._available = True
                self.feed(gga())
                self.feed(invalid)
                self.assertIsNone(self.sample())
                self.assertIsNone(self.gps.radio_fix(self.now))
                self.assertFalse(self.gps.fix.valid)
                self.now += 1
                self.feed(gga(utc="120002.00"))
                self.assertEqual(self.sample()["observed_at"], self.now)
                self.assertIsNotNone(self.gps.radio_fix(self.now))

    def test_checksum_failure_and_malformed_positions_clear_sample(self):
        invalid = [gga()[:-2] + "ZZ", gga().split("*")[0],
                   rmc()[:-2] + "ZZ", gga(utc="120001.00", lat=""),
                   gga(utc="120001.00", hdop="NaN"),
                   gga(utc="120001.00", altitude="inf"),
                   rmc(utc="120001.00", lon=""), nmea("GPGGA,120000")]
        for sentence in invalid:
            with self.subTest(sentence=sentence):
                self.gps = gps_module.GpsManager(device="never-open-test-device")
                self.gps._available = True
                self.feed(gga(), sentence)
                self.assertIsNone(self.sample())
                self.assertFalse(self.gps.fix.valid)

    def test_conflicting_rmc_or_repeated_gga_cannot_create_a_sample(self):
        for invalid in (rmc(lat="4027.6000"), rmc(utc="130000.00"),
                        gga(lat="3727.6000")):
            with self.subTest(invalid=invalid):
                self.gps = gps_module.GpsManager(device="never-open-test-device")
                self.gps._available = True
                self.feed(gga(), invalid)
                self.assertIsNone(self.sample())
                self.assertIsNone(self.gps.radio_fix(self.now))

    def test_close_and_serial_failure_clear_measured_sample(self):
        self.feed(gga())
        self.gps._conn = Mock()
        self.gps.close()
        self.assertIsNone(self.sample())
        self.gps._available = True
        self.feed(gga())
        self.gps._conn = Mock()
        type(self.gps._conn).in_waiting = PropertyMock(side_effect=OSError("synthetic disconnect"))
        self.assertEqual(self.gps.read_available(), [])
        self.assertIsNone(self.sample())
        self.assertFalse(self.gps.available)

    def test_no_fix_or_unavailable_receiver_returns_no_sample(self):
        self.assertIsNone(self.sample())
        self.feed(gga())
        self.gps._available = False
        self.assertIsNone(self.sample())

    def test_equator_and_zero_longitude_are_not_discarded_by_visual_filter(self):
        self.feed(gga(lat="0000.0000", lon="00000.0000", ew="E"))
        self.assertEqual((self.sample()["lat"], self.sample()["lon"]), (0.0, 0.0))

    def test_partial_nmea_is_only_accepted_when_complete(self):
        raw = (gga() + "\r\n").encode("ascii")
        buffer = gps_module._LineBuffer()
        self.feed(*buffer.feed(raw[:25]))
        self.assertIsNone(self.sample())
        self.feed(*buffer.feed(raw[25:]))
        self.assertEqual(self.sample()["observed_at"], self.now)


if __name__ == "__main__":
    unittest.main()
