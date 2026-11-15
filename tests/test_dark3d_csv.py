"""Synthetic Dark3D schema fixtures; no captured device log or hardware claim."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from watchdogs.piglet_csv import (
    DARK3D_FORMAT, DARK3D_HEADER, MAX_PHYSICAL_LINE, NATIVE_HEADER, PIGLET_HEADER,
    collection_records, dark3d_records, native_row, parse_dark3d_record,
    parse_record, piglet_records,
)


def dark3d_row(*, mac="02:00:00:00:00:01", name="Synthetic Dark3D", kind="WIFI",
               stamp="2026-11-13 12:34:56", channel="36", rssi="-61",
               lat="12.5", lon="34.5", altitude="100.25", accuracy="0.8"):
    return [mac, name, "[WPA2-PSK-CCMP][ESS]" if kind.upper() == "WIFI" else "[LE Random]",
            stamp, channel, rssi, lat, lon, altitude, accuracy, kind]


def csv_bytes(rows, *, preamble="WigleWifi-1.4,appRelease=synthetic-dark3d-audit",
              header=DARK3D_HEADER):
    out = io.StringIO(newline="")
    out.write(preamble + "\r\n")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8", errors="surrogateescape")


class Dark3DCSVTests(unittest.TestCase):
    def parse_bytes(self, data, parser=collection_records, *, name="synthetic.log"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_bytes(data)
            before = hashlib.sha256(data).hexdigest()
            records = list(parser(path))
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            return records

    def test_exact_reference_header_and_original_eleven_fields_are_retained(self):
        self.assertEqual(DARK3D_HEADER, (
            "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI", "CurrentLatitude",
            "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type"))
        original = dark3d_row()
        record = self.parse_bytes(csv_bytes([original]))[0]
        self.assertEqual(record["source_fields"], original)
        self.assertEqual(len(record["source_fields"]), 11)
        self.assertEqual(record["source_format"], DARK3D_FORMAT)
        self.assertEqual(record["source_preamble"], "WigleWifi-1.4,appRelease=synthetic-dark3d-audit")
        self.assertEqual(native_row(record), original)
        for absent in ("frequency", "rcois", "mfgr_id", "RCOIs", "MfgrId"):
            self.assertNotIn(absent, record)

    def test_naive_source_time_has_no_invented_timezone_or_absolute_timestamp(self):
        for stamp in ("2026-11-13 12:34:56", "2026-5-1 3:4:5", " 2026-05-01 03:04:05 "):
            with self.subTest(stamp=stamp):
                record = parse_dark3d_record(dark3d_row(stamp=stamp))
                self.assertEqual(record["source_time"], stamp)
                self.assertIsNone(record["captured_at"])
                self.assertFalse(record["capture_time_known"])
                self.assertFalse(record["capture_timezone_known"])

    def test_explicit_timezone_is_used_only_when_present_in_source(self):
        for stamp, expected in (
                ("2026-11-13 12:34:56Z", "2026-11-13T12:34:56Z"),
                ("2026-11-13 12:34:56+02:00", "2026-11-13T12:34:56+02:00"),
                ("2026-5-1 3:4:5.125-01:30", "2026-05-01T03:04:05.125000-01:30")):
            with self.subTest(stamp=stamp):
                record = parse_dark3d_record(dark3d_row(stamp=stamp))
                self.assertEqual(record["captured_at"], expected)
                self.assertTrue(record["capture_time_known"])
                self.assertTrue(record["capture_timezone_known"])
                self.assertEqual(record["source_fields"][3], stamp)

    def test_unknown_invalid_calendar_and_blank_time_remain_exact_source_values(self):
        for stamp in ("", "2000-0-0 0:0:0", "2026-02-30 12:34:56", "2026-11-13 25:00:00Z"):
            with self.subTest(stamp=stamp):
                record = parse_dark3d_record(dark3d_row(stamp=stamp))
                self.assertEqual(record["source_time"], stamp)
                self.assertIsNone(record["captured_at"])
                self.assertFalse(record["capture_time_known"])

    def test_source_gps_is_retained_without_host_position_or_optional_zero_values(self):
        original = dark3d_row(lat="-12.125", lon="130.75", altitude="-3.25", accuracy="0.0")
        record = parse_dark3d_record(original)
        self.assertEqual(record["position"], dict(
            lat=-12.125, lon=130.75, altitude=-3.25, accuracy=0.0,
            source="dark3d", source_time=original[3]))
        missing = parse_dark3d_record(dark3d_row(altitude="", accuracy=""))
        self.assertIsNone(missing["position"]["altitude"])
        self.assertIsNone(missing["position"]["accuracy"])
        self.assertEqual(missing["source_fields"][8:10], ["", ""])

    def test_missing_or_zero_pair_gps_does_not_create_a_position(self):
        for lat, lon in (("", ""), ("0", "0"), ("0.0", "-0.0")):
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(parse_dark3d_record(dark3d_row(lat=lat, lon=lon))["position"])
        self.assertEqual(parse_dark3d_record(dark3d_row(lat="0", lon="10"))["position"]["lat"], 0)

    def test_wifi_and_ble_export_use_correct_eleven_column_indices(self):
        for channel in ("1", "36", "177"):
            original = dark3d_row(channel=channel)
            self.assertEqual(native_row(parse_dark3d_record(original)), original)
        original = dark3d_row(kind="BLE", channel="0", name="Synthetic BLE")
        record = parse_dark3d_record(original)
        self.assertEqual(record["protocol"], "ble")
        self.assertEqual(record["name"], original[1])
        self.assertEqual(record["source_fields"], original)
        expected = list(original)
        expected[2] = "[BLE]"
        self.assertEqual(native_row(record), expected)

    def test_quoted_commas_multiline_and_nonascii_names_roundtrip(self):
        name = 'Synthetic, "quotes"\r\nsecond\nżółw'
        original = dark3d_row(name=name)
        record = self.parse_bytes(csv_bytes([original]))[0]
        self.assertEqual(record["ssid"], name)
        self.assertEqual(record["source_fields"], original)
        self.assertEqual(native_row(record)[1], name)

    def test_raw_radio_name_bytes_are_reversible_and_json_safe(self):
        raw = b"\xff\xfeSynthetic\x80"
        name = raw.decode("utf-8", errors="surrogateescape")
        record = self.parse_bytes(csv_bytes([dark3d_row(name=name)]))[0]
        self.assertEqual(bytes.fromhex(record["source_name_bytes_hex"]), raw)
        self.assertEqual(record["source_name_encoding"], "raw-bytes")
        json.dumps(record, ensure_ascii=False).encode("utf-8")
        self.assertEqual(len(record["source_fields"]), 11)
        literal = parse_dark3d_record(dark3d_row(name=record["ssid"]))
        self.assertNotEqual(literal["record_id"], record["record_id"])

    def test_ids_survive_redelivery_filename_and_preamble_changes(self):
        rows = [dark3d_row(), dark3d_row(kind="BLE", channel="0")]
        first = self.parse_bytes(csv_bytes(rows), name="first.log")
        second = self.parse_bytes(csv_bytes(rows, preamble="WigleWifi-1.4,appRelease=other,metadata=changed"),
                                  name="renamed.log")
        self.assertEqual([item["record_id"] for item in first], [item["record_id"] for item in second])
        self.assertNotEqual(first[0]["record_id"], first[1]["record_id"])
        changed = dark3d_row(stamp="2026-11-13 12:34:57")
        self.assertNotEqual(parse_dark3d_record(changed)["record_id"], first[0]["record_id"])

    def test_mac_and_type_normalization_have_stable_record_ids(self):
        lower = dark3d_row(mac="aa:bb:cc:dd:ee:ff", kind="wifi")
        upper = dark3d_row(mac="AA:BB:CC:DD:EE:FF", kind="WIFI")
        record = parse_dark3d_record(lower)
        self.assertEqual(record["record_id"], parse_dark3d_record(upper)["record_id"])
        self.assertEqual(record["source_fields"], lower)
        self.assertEqual(record["mac"], upper[0])

    def test_dispatch_uses_content_and_does_not_relax_original_piglet_reader(self):
        data = csv_bytes([dark3d_row()])
        self.assertEqual(self.parse_bytes(data, name="opaque.csv"), self.parse_bytes(data, dark3d_records))
        with self.assertRaisesRegex(ValueError, "Expected Piglet WiGLE 1.6"):
            self.parse_bytes(data, piglet_records)

    def test_only_genuinely_zero_byte_log_is_an_empty_dark3d_source(self):
        for name in ("zero.log", "zero.LOG"):
            with self.subTest(name=name):
                self.assertEqual(self.parse_bytes(b"", name=name), [])
        for name in ("zero.csv", "zero.bin", "zero"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.parse_bytes(b"", name=name)
        for data in (b"\n", b" ", b"\xef\xbb\xbf", b"\x00", b"not csv"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.parse_bytes(data, name="invalid.log")
        with self.assertRaises(ValueError):
            self.parse_bytes(b"", piglet_records, name="zero.log")
        with self.assertRaises(ValueError):
            self.parse_bytes(b"", dark3d_records, name="zero.log")

    def test_legacy_piglet_dispatch_and_native_export_remain_identical(self):
        source = ["02:00:00:00:00:01", "Synthetic Piglet", "[ESS]", "2026-11-13 12:34:56",
                  "6", "2437", "-61", "12.5", "34.5", "100", "0.8", "", "76", "WIFI"]
        data = csv_bytes([source], preamble="WigleWifi-1.6,appRelease=synthetic-piglet", header=PIGLET_HEADER)
        legacy = self.parse_bytes(data, piglet_records, name="piglet.csv")
        dispatched = self.parse_bytes(data, name="piglet.csv")
        self.assertEqual(dispatched, legacy)
        self.assertEqual(dispatched[0], parse_record(source))
        self.assertNotIn("source_format", dispatched[0])
        self.assertEqual(dispatched[0]["captured_at"], "2026-11-13T12:34:56Z")
        self.assertEqual(native_row(dispatched[0]), [source[0], source[1], source[2], source[3],
                                                   source[4], source[6], *source[7:11], source[13]])

    def test_piglet_and_dark3d_record_namespaces_do_not_collide(self):
        dark = dark3d_row()
        piglet = [*dark[:5], "5180", *dark[5:10], "", "", dark[10]]
        self.assertNotEqual(parse_dark3d_record(dark)["record_id"], parse_record(piglet)["record_id"])

    def test_wrong_headers_versions_and_cross_schema_rows_are_rejected(self):
        for data in (
                csv_bytes([dark3d_row()], preamble="WigleWifi-1.5,appRelease=synthetic"),
                csv_bytes([dark3d_row()], header=PIGLET_HEADER),
                csv_bytes([dark3d_row()], header=tuple(reversed(DARK3D_HEADER))),
                csv_bytes([dark3d_row() + ["extra"]]),
                csv_bytes([dark3d_row()[:-1]]),
                b"WigleWifi-1.4,fixture\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.parse_bytes(data)

    def test_invalid_radio_position_and_metadata_values_are_rejected(self):
        changes = ((0, "not-a-mac"), (1, "embedded\x00NUL"), (2, "bad\udcff"),
                   (3, "2026-11-13"), (3, "now"), (4, "197"), (4, "nan"),
                   (5, "-129"), (5, "128"), (6, "91"), (7, "181"),
                   (8, "nan"), (9, "-1"), (10, "BT"))
        for index, value in changes:
            source = dark3d_row()
            source[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                parse_dark3d_record(source)
        with self.assertRaises(ValueError):
            parse_dark3d_record(dark3d_row(lat="", lon="12.5"))

    def test_malformed_csv_is_rejected_without_guessing_unquoted_name_boundaries(self):
        prefix = csv_bytes([])
        for line in (b'02:00:00:00:00:01,"unterminated\n',
                     b'02:00:00:00:00:01,unquoted,comma,[ESS],2026-11-13 12:00:00,6,-61,0,0,0,1,WIFI\n'):
            with self.subTest(line=line), self.assertRaises((ValueError, csv.Error)):
                self.parse_bytes(prefix + line)

    def test_preamble_header_row_and_nonname_bytes_are_bounded_and_strict(self):
        for data in (
                b"WigleWifi-1.4," + b"x" * (MAX_PHYSICAL_LINE + 1),
                b"WigleWifi-1.4,synthetic\n" + b"MAC," + b"x" * (MAX_PHYSICAL_LINE + 1),
                csv_bytes([dark3d_row(name="x" * (MAX_PHYSICAL_LINE + 1))]),
                csv_bytes([dark3d_row()], preamble="WigleWifi-1.4,bad\x00metadata"),
                b"WigleWifi-1.4,bad\xffmetadata\n" + csv_bytes([]).split(b"\n", 1)[1]):
            with self.subTest(prefix=data[:40]), self.assertRaises(ValueError):
                self.parse_bytes(data)

    def test_utf8_bom_blank_rows_and_windows_line_endings_are_supported(self):
        data = b"\xef\xbb\xbf" + csv_bytes([dark3d_row(), [], dark3d_row(kind="BLE", channel="0")])
        records = self.parse_bytes(data)
        self.assertEqual([item["protocol"] for item in records], ["wifi", "ble"])
        self.assertEqual(NATIVE_HEADER, DARK3D_HEADER)


if __name__ == "__main__":
    unittest.main()
