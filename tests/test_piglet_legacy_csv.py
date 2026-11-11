"""Lossless historical radio-name bytes and explicit unknown source times."""
import csv
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from watchdogs.piglet_csv import PIGLET_HEADER, native_row, parse_record, piglet_records


def fields(name="Synthetic", stamp="2026-11-08 12:34:56", kind="BLE"):
    return ["02:00:00:00:00:01", name, "[LE Random]" if kind == "BLE" else "[ESS]", stamp,
            "0" if kind == "BLE" else "6", "0" if kind == "BLE" else "2437", "-61",
            "12.5", "34.5", "100", "0.8", "", "76", kind]


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    stream.write("WigleWifi-1.6,appRelease=fixture\n")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(PIGLET_HEADER)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8", errors="surrogateescape")


class HistoricalCSVTests(unittest.TestCase):
    def parse_bytes(self, data):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.csv"
            source.write_bytes(data)
            records = list(piglet_records(source))
            self.assertEqual(source.read_bytes(), data)
            return records

    def test_invalid_calendar_date_is_unknown_without_replacing_original_time(self):
        for source_time in ("2000-00-00 00:00:00", "2026-02-31 12:34:56", "0000-01-01 00:00:00"):
            with self.subTest(source_time=source_time):
                source = fields(stamp=source_time)
                record = self.parse_bytes(csv_bytes([source]))[0]
                self.assertIsNone(record["captured_at"])
                self.assertIs(record["capture_time_known"], False)
                self.assertEqual(record["source_time"], source_time)
                self.assertEqual(record["source_fields"], source)
                self.assertEqual(record["position"]["source_time"], source_time)
                self.assertEqual(native_row(record)[3], source_time)

    def test_valid_times_and_existing_record_ids_remain_unchanged(self):
        for stamp in ("1970-01-01 00:09:12", "2026-11-08T12:34:56.123Z"):
            source = fields("Valid café", stamp)
            expected = hashlib.sha256(json.dumps(source, ensure_ascii=False,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            record = parse_record(source)
            self.assertEqual(record["record_id"], expected)
            self.assertNotIn("capture_time_known", record)
            self.assertNotIn("source_name_encoding", record)
            self.assertEqual(record["source_fields"], source)
            datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))

    def test_invalid_name_bytes_are_reversible_json_safe_and_replay_stable(self):
        raw_name = b'A\xff\xc3(,"\nB\\x00'
        source = fields(raw_name.decode("utf-8", errors="surrogateescape"))
        data = csv_bytes([source])
        record = self.parse_bytes(data)[0]
        self.assertEqual(record["source_name_encoding"], "raw-bytes")
        self.assertEqual(bytes.fromhex(record["source_name_bytes_hex"]), raw_name)
        self.assertEqual(record["name"], "".join("\\x%02x" % byte for byte in raw_name))
        self.assertEqual(record["source_fields"][1], record["name"])
        self.assertNotIn("\ufffd", record["name"])
        self.assertTrue(record["name"].isascii())
        self.assertEqual(json.loads(json.dumps(record, ensure_ascii=False).encode("utf-8")), record)
        self.assertEqual(self.parse_bytes(data)[0]["record_id"], record["record_id"])
        self.assertEqual(native_row(record)[1], record["name"])

    def test_literal_escape_name_never_collides_with_original_invalid_bytes(self):
        invalid = parse_record(fields(b"\xff".decode("utf-8", errors="surrogateescape")))
        literal = parse_record(fields(invalid["name"]))
        self.assertEqual(invalid["name"], literal["name"])
        self.assertNotEqual(invalid["record_id"], literal["record_id"])
        self.assertNotIn("source_name_bytes_hex", literal)

    def test_unknown_time_and_invalid_bytes_can_coexist_without_invented_metadata(self):
        source = fields(b"\xfe".decode("utf-8", errors="surrogateescape"), "2000-00-00 00:00:05")
        record = self.parse_bytes(csv_bytes([source]))[0]
        self.assertIsNone(record["captured_at"])
        self.assertEqual(record["source_name_bytes_hex"], "fe")
        self.assertEqual(record["source_fields"][3], source[3])
        self.assertEqual(record["rssi"], -61)

    def test_invalid_bytes_outside_name_and_malformed_timestamps_stay_rejected(self):
        bad = b"\xff".decode("utf-8", errors="surrogateescape")
        for index in range(14):
            if index == 1:
                continue
            source = fields()
            source[index] += bad
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.parse_bytes(csv_bytes([source]))
        for stamp in ("not a date", "2026-11-08", "2026-11-08 12:34:56+01:00"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                parse_record(fields(stamp=stamp))
        with self.assertRaises(ValueError):
            self.parse_bytes(csv_bytes([fields()]).replace(b"appRelease=fixture", b"appRelease=bad\xff"))

    def test_malformed_csv_remains_rejected_without_salvaging_partial_rows(self):
        data = csv_bytes([fields()])
        for broken in (data + b'"unterminated\n', data.replace(b'"[LE Random]"', b'"[LE Random]"x')):
            if broken == data:
                continue
            with self.assertRaises((ValueError, csv.Error)):
                self.parse_bytes(broken)


class OptionalArchivedCSVTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("WDG_PIGLET_LEGACY_ARCHIVE"), "No external CSV archive fixture configured")
    def test_every_archived_row_roundtrips_source_fields_and_original_bytes(self):
        root = Path(os.environ["WDG_PIGLET_LEGACY_ARCHIVE"])
        total = unknown = raw_names = 0
        paths = sorted(root.rglob("*.csv"))
        self.assertTrue(paths)
        for path in paths:
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with path.open("r", encoding="utf-8-sig", errors="surrogateescape", newline="") as stream:
                reader = csv.reader(stream, strict=True)
                next(reader)
                self.assertEqual(tuple(next(reader)), PIGLET_HEADER)
                originals = (row for row in reader if row)
                parsed = iter(piglet_records(path))
                for original in originals:
                    record = next(parsed)
                    restored = list(record["source_fields"])
                    if record.get("source_name_encoding") == "raw-bytes":
                        restored[1] = bytes.fromhex(record["source_name_bytes_hex"]).decode("utf-8", errors="surrogateescape")
                        raw_names += 1
                    self.assertEqual(restored, original)
                    self.assertIsNone(re.search(r"[\ud800-\udfff]", json.dumps(record, ensure_ascii=False)))
                    json.dumps(record, ensure_ascii=False).encode("utf-8")
                    if record["captured_at"] is None:
                        self.assertIs(record["capture_time_known"], False)
                        unknown += 1
                    total += 1
                self.assertIsNone(next(parsed, None), "Parser created an observation not present in the source")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        expected = os.environ.get("WDG_PIGLET_LEGACY_EXPECTED_COUNTS")
        if expected:
            self.assertEqual(dict(files=len(paths), rows=total, unknown_times=unknown, raw_names=raw_names), json.loads(expected))


if __name__ == "__main__":
    unittest.main()
