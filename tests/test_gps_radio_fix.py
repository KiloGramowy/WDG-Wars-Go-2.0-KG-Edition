"""Credible GPS snapshots for RF selection, using NMEA bytes and fake time only.

No serial device, player database, network or radio is opened. pyserial is an
existing application dependency; a scoped import stub allows these pure parser
tests to run on a workstation where that hardware dependency is not installed.
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


def gga(utc="120000.00", lat="3627.6000", ns="N", lon="00612.0000", ew="W",
        quality="1", sats="08", hdop="0.9", altitude="10.0", talker="GP"):
    return nmea(f"{talker}GGA,{utc},{lat},{ns},{lon},{ew},{quality},{sats},{hdop},{altitude},M,0.0,M,,")


def rmc(utc="120000.00", status="A", lat="3627.6000", ns="N", lon="00612.0000",
        ew="W", speed="0.0", talker="GP"):
    return nmea(f"{talker}RMC,{utc},{status},{lat},{ns},{lon},{ew},{speed},0.0,291026,,,A")


class GpsRadioFixTests(unittest.TestCase):
    def setUp(self):
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.gps._available = True
        self.now = 100.0
        self.clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def feed(self, *sentences):
        self.gps.process_sentences(list(sentences))

    def fix(self):
        return self.gps.radio_fix(self.now)

    def test_complete_gga_alone_is_sufficient_and_snapshot_is_a_copy(self):
        self.feed(gga())
        value = self.fix()
        self.assertEqual(set(value), {"lat", "lon", "observed_at", "quality", "sats", "hdop"})
        self.assertAlmostEqual(value["lat"], 36.46)
        self.assertAlmostEqual(value["lon"], -6.2)
        self.assertEqual((value["observed_at"], value["quality"], value["sats"], value["hdop"]),
                         (100.0, 1, 8, 0.9))
        value["lat"] = 0
        self.assertAlmostEqual(self.fix()["lat"], 36.46)
        self.assertTrue(self.gps.fix.valid)

    def test_measured_quality_types_and_gn_talker(self):
        for quality in (1, 2, 4, 5):
            with self.subTest(quality=quality):
                self.now += 1
                self.feed(gga(utc=f"12000{quality}.00", quality=str(quality), talker="GN"))
                self.assertEqual(self.fix()["quality"], quality)

    def test_quality_satellites_and_hdop_gate_radio_only(self):
        cases = [(dict(quality=str(q)), "quality") for q in (0, 3, 6, 7, 8)]
        cases += [(dict(sats="03"), "satellites"), (dict(hdop="3.01"), "hdop")]
        for changes, reason in cases:
            with self.subTest(reason=reason, changes=changes):
                self.feed(gga(utc="120010.00", **changes))
                self.assertIsNone(self.fix())
        self.feed(gga(utc="120011.00", sats="04", hdop="3.0"))
        self.assertIsNotNone(self.fix())

    def test_stale_and_future_observations_are_rejected_without_refresh(self):
        self.feed(gga())
        self.assertEqual(self.gps.radio_fix(110)["observed_at"], 100)
        self.assertIsNone(self.gps.radio_fix(110.001))
        self.assertIsNone(self.gps.radio_fix(99.999))
        self.assertIsNone(self.gps.radio_fix(math.nan))
        self.assertIsNone(self.gps.radio_fix(math.inf))

    def test_gsv_rmc_and_repeated_epoch_do_not_refresh_observation(self):
        self.feed(gga())
        self.now = 105
        self.feed(nmea("GPGSV,1,1,10"), rmc(), gga())
        self.assertEqual(self.fix()["observed_at"], 100)
        self.now = 111
        self.feed(nmea("GPGSV,1,1,11"), gga())
        self.assertIsNone(self.fix())
        self.assertEqual(self.gps.fix.satellites_visible, 11)

    def test_next_complete_gga_epoch_refreshes_observation(self):
        self.feed(gga())
        self.now = 101
        self.feed(gga(utc="120001.00"))
        self.assertEqual(self.fix()["observed_at"], 101)

    def test_different_coordinates_in_same_gga_epoch_are_not_a_second_fix(self):
        self.feed(gga())
        self.now += 0.1
        self.feed(gga(lat="3727.6000"))
        self.assertIsNone(self.fix())
        self.assertFalse(self.gps.fix.valid)
        self.assertAlmostEqual(self.gps.fix.latitude, 36.46)

    def test_rmc_alone_never_provides_quality_for_radio_selection(self):
        self.feed(rmc())
        self.assertTrue(self.gps.fix.valid)
        self.assertIsNone(self.fix())

    def test_rmc_void_immediately_invalidates_gga_and_does_not_resurrect(self):
        self.feed(gga())
        self.now += 0.1
        self.feed(rmc(status="V", lat="", ns="", lon="", ew=""))
        self.assertFalse(self.gps.fix.valid)
        self.assertIsNone(self.fix())
        self.feed(rmc())
        self.assertIsNone(self.fix())
        self.now += 1
        self.feed(gga(utc="120001.00"))
        self.assertIsNotNone(self.fix())

    def test_consistent_rmc_before_or_after_gga_preserves_original_time(self):
        for first, second in ((rmc(), gga()), (gga(), rmc())):
            with self.subTest(first=first[:6]):
                self.gps = gps_module.GpsManager(device="never-open-test-device")
                self.gps._available = True
                self.now = 100
                self.feed(first)
                self.now = 100.2
                self.feed(second)
                self.assertIsNotNone(self.fix())
                self.assertEqual(self.fix()["observed_at"], 100.2 if first.startswith("$GPRMC") else 100)

    def test_conflicting_rmc_position_or_time_invalidates_either_order(self):
        for bad_rmc in (rmc(lat="4027.6000"), rmc(utc="130000.00")):
            for first, second in ((gga(), bad_rmc), (bad_rmc, gga())):
                with self.subTest(first=first, second=second):
                    self.gps = gps_module.GpsManager(device="never-open-test-device")
                    self.gps._available = True
                    self.feed(first, second)
                    self.assertIsNone(self.fix())
                    self.assertFalse(self.gps.fix.valid)

    def test_expired_rmc_does_not_block_new_complete_gga(self):
        self.feed(rmc(lat="4027.6000"))
        self.now += gps_module.RADIO_FIX_RMC_MAX_AGE + 0.01
        self.feed(gga(utc="120003.00"))
        self.assertIsNotNone(self.fix())

    def test_midnight_utc_rollover_is_consistent(self):
        self.feed(gga(utc="235959.50"))
        self.now += 0.5
        self.feed(rmc(utc="000000.00"))
        self.assertIsNotNone(self.fix())

    def test_checksum_failure_or_absence_invalidates_without_coordinate_update(self):
        for bad in (gga()[:-2] + "00", gga().split("*")[0], gga()[:-2] + "ZZ"):
            with self.subTest(bad=bad):
                self.gps._last_gga = None
                self.feed(gga())
                previous = (self.gps.fix.latitude, self.gps.fix.longitude)
                self.feed(bad)
                self.assertIsNone(self.fix())
                self.assertFalse(self.gps.fix.valid)
                self.assertEqual((self.gps.fix.latitude, self.gps.fix.longitude), previous)

    def test_invalid_or_incomplete_coordinates_never_mix_old_and_new_fields(self):
        malformed = [dict(lat=""), dict(lon=""), dict(ns=""), dict(ew="N"),
                     dict(lat="NaN"), dict(lon="inf"), dict(lat="3660.0000"),
                     dict(lat="9100.0000"), dict(lon="18100.0000"),
                     dict(lat="9000.0001"), dict(lon="18000.0001"),
                     dict(lat="-3627.6000"), dict(lon="0620.0000"),
                     dict(hdop="NaN"), dict(hdop="-1"), dict(sats="-1"),
                     dict(altitude="inf"), dict(quality="-1"), dict(quality="9")]
        for changes in malformed:
            with self.subTest(changes=changes):
                self.gps._last_gga = None
                self.feed(gga())
                previous = (self.gps.fix.latitude, self.gps.fix.longitude)
                self.feed(gga(utc="120001.00", **changes))
                self.assertIsNone(self.fix())
                self.assertFalse(self.gps.fix.valid)
                self.assertEqual((self.gps.fix.latitude, self.gps.fix.longitude), previous)

    def test_incomplete_rmc_does_not_mix_coordinates_or_reuse_old_quality(self):
        self.feed(gga())
        self.feed(rmc(utc="120001.00", lat="3727.6000", lon=""))
        self.assertIsNone(self.fix())
        self.assertFalse(self.gps.fix.valid)
        self.assertAlmostEqual(self.gps.fix.latitude, 36.46)

    def test_bad_utc_and_truncated_position_sentences_invalidate(self):
        for bad in (gga(utc=""), gga(utc="240000"), gga(utc="126000"),
                    gga(utc="120060"), gga(utc="12:00:00"),
                    nmea("GPGGA,120000"), nmea("GPRMC,120000")):
            with self.subTest(bad=bad):
                self.gps._last_gga = None
                self.feed(gga(), bad)
                self.assertIsNone(self.fix())
                self.assertFalse(self.gps.fix.valid)

    def test_zero_longitude_equator_and_exact_coordinate_bounds_are_valid(self):
        for lat, lon, ns, ew in (("0000.0000", "00000.0000", "N", "E"),
                                 ("9000.0000", "18000.0000", "S", "W")):
            with self.subTest(lat=lat, lon=lon):
                self.gps._last_gga = None
                self.feed(gga(lat=lat, lon=lon, ns=ns, ew=ew))
                self.assertIsNotNone(self.fix())

    def test_close_or_serial_disconnect_invalidates_immediately(self):
        self.feed(gga())
        self.gps._conn = Mock()
        self.gps.close()
        self.assertFalse(self.gps.available)
        self.assertFalse(self.gps.fix.valid)
        self.assertIsNone(self.fix())
        self.gps._available = True
        self.feed(gga())
        self.gps._conn = Mock()
        type(self.gps._conn).in_waiting = PropertyMock(side_effect=OSError("disconnected"))
        self.assertEqual(self.gps.read_available(), [])
        self.assertFalse(self.gps.available)
        self.assertIsNone(self.fix())

    def test_partial_serial_bytes_are_parsed_only_as_complete_lines(self):
        line = (gga() + "\r\n").encode("ascii")
        buffer = gps_module._LineBuffer()
        self.assertEqual(buffer.feed(line[:30]), [])
        self.assertIsNone(self.fix())
        self.feed(*buffer.feed(line[30:]))
        self.assertIsNotNone(self.fix())


if __name__ == "__main__":
    unittest.main()
