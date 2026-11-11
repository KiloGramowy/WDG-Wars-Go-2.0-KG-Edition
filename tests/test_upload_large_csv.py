"""Native CSV capacity regressions with temporary data and fake HTTP only.

The small case runs normally. Explicit SSD capacity checks:
WDG_UPLOAD_CSV_CAPACITY_MIB=30,100 WDG_UPLOAD_CSV_TMPDIR=/isolated/ssd/stage
python3 -B -m unittest discover -s tests -p test_upload_large_csv.py -v

These are fixture sizes, not production file limits. The current parser returns
the entire record list; this suite makes no bounded-memory claim.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plugins import wardrive_upload as upload
from watchdogs.piglet_csv import NATIVE_HEADER, PREAMBLE


MIB = 1024 * 1024


def _selected_capacity(mib):
    return str(mib) in {
        value.strip() for value in os.environ.get("WDG_UPLOAD_CSV_CAPACITY_MIB", "").split(",")
    }


def _expected_record(sequence):
    # A valid 32-byte SSID, including commas, quotes and non-ASCII characters.
    # The sequence is observable independently of CSV quoting and batch layout.
    ssid = f'{sequence:08x},"' + "📶" * 5 + 'Q"'
    assert len(ssid.encode("utf-8")) == 32
    return dict(
        bssid="02:" + ":".join(f"{(sequence >> shift) & 255:02X}" for shift in (32, 24, 16, 8, 0)),
        ssid=ssid, auth="WPA2", first_seen=f"2026-11-08T12:00:00.{sequence % 1000000:06d}Z",
        channel=sequence % 11 + 1, rssi=-30 - sequence % 60,
        lat=40.123456, lon=-3.234567, alt=123.4, type="WIFI",
    )


def _update_digest(digest, record):
    digest.update(json.dumps(record, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii"))
    digest.update(b"\n")


def _write_native_csv(path, minimum_bytes):
    expected = hashlib.sha256()
    count = 0
    with path.open("wb") as binary:
        class Utf8Writer:
            size = 0

            def write(self, text):
                encoded = text.encode("utf-8")
                self.size += binary.write(encoded)
                return len(text)

        stream = Utf8Writer()
        stream.write(PREAMBLE + "\n")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(NATIVE_HEADER)
        while stream.size < minimum_bytes:
            record = _expected_record(count)
            writer.writerow((record["bssid"], record["ssid"], "[WPA2-PSK]",
                             record["first_seen"], record["channel"], record["rssi"],
                             record["lat"], record["lon"], record["alt"], "1.0", "WIFI"))
            _update_digest(expected, record)
            count += 1
    return count, expected.hexdigest()


class LargeCsvUploadTests(unittest.TestCase):
    def _plugin(self, state_path):
        config = dict(WARDRIVE_API_URL="https://example.invalid/api/upload/",
                      WARDRIVE_API_KEY="synthetic-capacity-account")
        with patch.object(upload.WardriveUpload, "_state_file", return_value=state_path), \
                patch.object(upload, "_load_secrets_conf", return_value=config):
            plugin = upload.WardriveUpload()
        plugin._state_file = lambda: state_path
        plugin._sync_log = lambda *_args, **_kwargs: None
        plugin._handle_upload_badges = lambda _result: None
        # Exercise the HTTP/signing implementation without real rate-limit waits.
        plugin._wait_upload_slot = lambda current: bool(current())
        return plugin

    def _capacity(self, minimum_bytes, *, require_ssd_directory=False):
        temporary_parent = os.environ.get("WDG_UPLOAD_CSV_TMPDIR")
        if require_ssd_directory:
            self.assertTrue(temporary_parent, "Set WDG_UPLOAD_CSV_TMPDIR to an isolated SSD directory")
            self.assertTrue(Path(temporary_parent).is_dir(), "The isolated SSD directory must exist")
        with tempfile.TemporaryDirectory(dir=temporary_parent) as temporary:
            root = Path(temporary)
            source = root / "wardriving.csv"
            count, expected_digest = _write_native_csv(source, minimum_bytes)
            source_bytes = source.stat().st_size
            self.assertGreaterEqual(source_bytes, minimum_bytes)
            plugin = self._plugin(root / "upload-state.json")
            rows = plugin._parse_csv(source)
            self.assertEqual(len(rows), count)
            data = dict(networks=rows, aircraft=[], meshcore_nodes=[])

            parsed_digest = hashlib.sha256()
            for sequence, record in enumerate(rows):
                self.assertEqual(int(record["ssid"][:8], 16), sequence)
                _update_digest(parsed_digest, record)
            self.assertEqual(parsed_digest.hexdigest(), expected_digest)

            # Reassemble only a rolling digest, not a second full payload/list.
            planned_digest = hashlib.sha256()
            seen = batches = maximum_wire = 0
            for start, end in plugin._plan_upload_batches(data):
                self.assertEqual(start, seen)
                self.assertGreater(end, start)
                part = plugin._payload_slice(data, start, end)
                part_count = sum(len(value) for value in part.values())
                self.assertEqual(part_count, end - start)
                self.assertLessEqual(part_count, upload.UPLOAD_BATCH_RECORDS)
                wire = plugin._sign_payload(part)
                maximum_wire = max(maximum_wire, len(wire))
                self.assertLessEqual(len(wire), upload.UPLOAD_BATCH_BYTES)
                for record in part["networks"]:
                    self.assertEqual(int(record["ssid"][:8], 16), seen)
                    _update_digest(planned_digest, record)
                    seen += 1
                batches += 1
            self.assertEqual(seen, count)
            self.assertEqual(planned_digest.hexdigest(), expected_digest)
            self.assertGreater(batches, 0)
            if minimum_bytes >= 30 * MIB:
                self.assertGreater(batches, 1)

            sent_digest = hashlib.sha256()
            posted_rows = posted_batches = 0
            account = (plugin._api_url, plugin._api_key)

            def fake_http(request, **_kwargs):
                nonlocal posted_rows, posted_batches
                self.assertLessEqual(len(request.data), upload.UPLOAD_BATCH_BYTES)
                envelope = json.loads(request.data)
                encoded = envelope["data"].encode("ascii")
                signature = hmac.new(account[1].encode("utf-8"),
                                     envelope["nonce"].encode("ascii") + encoded,
                                     hashlib.sha256).hexdigest()
                self.assertTrue(hmac.compare_digest(signature, envelope["sig"]))
                body = json.loads(base64.b64decode(encoded, validate=True))
                amount = sum(len(body[key]) for key in ("networks", "aircraft", "meshcore_nodes"))
                self.assertGreater(amount, 0)
                self.assertLessEqual(amount, upload.UPLOAD_BATCH_RECORDS)
                for record in body["networks"]:
                    self.assertEqual(int(record["ssid"][:8], 16), posted_rows)
                    _update_digest(sent_digest, record)
                    posted_rows += 1
                posted_batches += 1
                receipt = dict(ok=True, imported=0, updated=0,
                               duplicates=len(body["networks"]), cooldown=0, rejected=0)
                return io.BytesIO(json.dumps(receipt).encode("utf-8"))

            # Simulate interruption after every real child ACK has checkpointed,
            # but before the complete local coverage manifest can be saved.
            with patch.object(upload, "_open", side_effect=fake_http), \
                    patch.object(plugin, "_remember_coverage", return_value=False):
                interrupted = plugin._upload_batched(data, account, lambda: True)
            self.assertFalse(interrupted["confirmed"])
            self.assertEqual(posted_rows, count)
            self.assertEqual(posted_batches, batches)
            self.assertEqual(sent_digest.hexdigest(), expected_digest)
            self.assertIsNone(plugin._cached_coverage(data))
            self.assertTrue((root / "upload-state.json").is_file())

            restarted = self._plugin(root / "upload-state.json")
            with patch.object(upload, "_open", side_effect=AssertionError("Confirmed child payload must not be resent")):
                resumed = restarted._upload_batched(data, account, lambda: True)
            self.assertTrue(resumed["confirmed"])
            self.assertTrue(resumed["reused"])
            proof = restarted._cached_coverage(data)
            self.assertIsNotNone(proof)
            self.assertEqual(proof["counts"], dict(networks=count, aircraft=0, meshcore_nodes=0))
            self.assertEqual(source.stat().st_size, source_bytes)
            report = dict(target_bytes=minimum_bytes, source_bytes=source_bytes, rows=count,
                          batches=batches, maximum_signed_bytes=maximum_wire,
                          record_limit=upload.UPLOAD_BATCH_RECORDS, wire_limit=upload.UPLOAD_BATCH_BYTES,
                          all_row_sha256=expected_digest, resumed_without_network=True)
            print("UPLOAD_CSV_CAPACITY " + json.dumps(report, sort_keys=True))

    def test_small_native_csv_exercises_parser_signing_ack_and_restart(self):
        self._capacity(MIB)

    @unittest.skipUnless(_selected_capacity(30), "30 MiB capacity run is explicit SSD opt-in")
    def test_30_mib_native_csv_has_lossless_bounded_upload_and_checkpoint_resume(self):
        self._capacity(30 * MIB, require_ssd_directory=True)

    @unittest.skipUnless(_selected_capacity(100), "100 MiB capacity run is explicit SSD opt-in")
    def test_100_mib_native_csv_has_lossless_bounded_upload_and_checkpoint_resume(self):
        self._capacity(100 * MIB, require_ssd_directory=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
