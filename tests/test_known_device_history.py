"""Read actual current/legacy identity columns without rewriting player files."""
from pathlib import Path
import tempfile
import unittest

from watchdogs.loot_manager import LootManager


FIRST, SECOND = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
STAMP = "2020-01-01T12:34:56"


class KnownDeviceHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.loot = LootManager.__new__(LootManager)
        self.loot._base = self.base

    def session(self, name="2020-01-01_12-34-56", **files):
        directory = self.base / name
        directory.mkdir()
        for filename, contents in files.items():
            (directory / filename).write_text(contents, encoding="utf-8")

    def read_unchanged(self):
        original = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        result = self.loot.get_known_devices()
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(set(self.base.rglob("*")),
                         set(original) | {path.parent for path in original})
        return result

    def test_current_timestamp_first_header_reads_mac_not_timestamp_or_device_name(self):
        self.session(**{"bt_devices.csv": "timestamp,mac,rssi,name,airtag,smarttag,lat,lon\n"
            f'{STAMP},{FIRST.lower()},-70,"mac",False,False,12.5,34.5\n'
            f'{STAMP},{SECOND},-80,"Quoted, name",False,False,12.5,34.5\n'
            f'{STAMP},invalid,-70,{FIRST},False,False,12.5,34.5\n'})
        self.assertEqual(self.read_unchanged(), ({FIRST, SECOND}, set()))

    def test_mac_first_legacy_header_and_headerless_rows_remain_supported(self):
        # The older documented layout starts mac,name,type,rssi,lat,lon;
        # save_bt_device's legacy duplicate check also accepts MAC-first files.
        self.session(**{"bt_devices.csv": "mac,name,type,rssi,lat,lon\n"
            f" {FIRST.lower()} ,Legacy,BLE,-70,12.5,34.5\n"})
        self.session("2021-01-01_12-34-56", **{"bt_devices.csv":
            f"{SECOND},Legacy,BLE,-80,12.5,34.5\n"})
        self.assertEqual(self.read_unchanged(), ({FIRST, SECOND}, set()))

    def test_headerless_timestamp_first_rows_use_the_real_second_column(self):
        self.session(**{"bt_devices.csv":
            f"{STAMP},{FIRST},-70,Legacy,False,False,12.5,34.5\n"})
        self.assertEqual(self.read_unchanged(), ({FIRST}, set()))

    def test_colons_alone_do_not_make_addresses_in_either_file(self):
        malformed = (STAMP, "12:34:56", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:GG",
                     "AA:BB:CC:DD:EE:001", "AA-BB-CC-DD-EE-01", "prefix:" + FIRST)
        self.session(**{"bt_devices.csv": "mac,name,type,rssi,lat,lon\n" + "".join(
            f"{mac},Invalid,BLE,-70,12.5,34.5\n" for mac in malformed),
            "wardriving.csv": "".join(f"{mac},Invalid\n" for mac in malformed)})
        self.assertEqual(self.read_unchanged(), (set(), set()))

    def test_wigle_ble_and_wifi_have_separate_identity_namespaces(self):
        pre_header = LootManager._WIGLE_PRE_HEADER + LootManager._WIGLE_HEADER
        self.session(**{"wardriving.csv": pre_header +
            f'{FIRST.lower()},"WiFi, name",[ESS],{STAMP},6,-70,12.5,34.5,20,1,WIFI\n'
            f'{SECOND},BLE,[BLE],{STAMP},0,-70,12.5,34.5,20,1,BLE\n'})
        # Existing readers also accept Type-less WiGLE records as Wi-Fi.
        self.session("2021-01-01_12-34-56", **{"wardriving.csv":
            f"{FIRST},Older WiFi,[ESS],{STAMP},6,-70,12.5,34.5\n"})
        self.assertEqual(self.read_unchanged(), ({SECOND}, {FIRST}))

    def test_native_wigle_names_with_commas_and_unmatched_quotes_use_the_type_tail(self):
        pre_header = LootManager._WIGLE_PRE_HEADER + LootManager._WIGLE_HEADER
        self.session(**{"wardriving.csv": pre_header +
            f'{FIRST},"BLE, kitchen,[BLE],{STAMP},,-70,12.5,34.5,20,1,BLE\n'
            f'{SECOND},"Guest, BLE,[ESS],{STAMP},6,-70,12.5,34.5,20,1,WIFI\n'})
        self.assertEqual(self.read_unchanged(), ({FIRST}, {SECOND}))

    def test_unmatched_quote_in_native_ble_name_does_not_consume_the_next_record(self):
        self.session(**{"bt_devices.csv": "timestamp,mac,rssi,name,airtag,smarttag,lat,lon\n"
            f'{STAMP},{FIRST},-70,"Kitchen,False,False,12.5,34.5\n'
            f'{STAMP},{SECOND},-80,Next device,False,False,12.5,34.5\n'})
        self.assertEqual(self.read_unchanged(), ({FIRST, SECOND}, set()))

    def test_legacy_mac_first_history_points_keep_names_positions_and_following_rows(self):
        self.session(**{"bt_devices.csv": "mac,name,type,rssi,lat,lon\n"
            f'{FIRST},"Legacy, beacon,BLE,-71,12.5,34.5\n'
            f'{SECOND},Following beacon,BLE,-80,13.5,35.5\n'})
        original = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        points = self.loot.get_gps_points()
        self.assertEqual({(point["type"], point["bssid"], point["label"], point["lat"],
                           point["lon"], point["rssi"]) for point in points},
                         {("bt", FIRST, '"Legacy, beacon', 12.5, 34.5, "-71"),
                          ("bt", SECOND, "Following beacon", 13.5, 35.5, "-80")})
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(self.read_unchanged(), ({FIRST, SECOND}, set()))

    def test_typeless_wigle_eight_nine_and_ten_column_history_keeps_its_positions(self):
        expected = set()
        addresses = (FIRST, SECOND, "AA:BB:CC:DD:EE:03")
        header = LootManager._WIGLE_HEADER.strip().split(",")
        for index, count in enumerate((8, 9, 10)):
            address = addresses[index]
            label = f"Old, WiFi {count}"
            columns = [address, f'"{label}"', "OPEN" if count == 9 else "[ESS]", STAMP, "6", "-70",
                       str(12.5 + index), str(34.5 + index), "20", "1"]
            self.session(f"202{index}-01-01_12-34-56", **{"wardriving.csv":
                LootManager._WIGLE_PRE_HEADER + ",".join(header[:count]) + "\n" +
                ",".join(columns[:count]) + "\n"})
            expected.add(("wifi", address, label, 12.5 + index, 34.5 + index, "-70"))
        original = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        points = self.loot.get_gps_points()
        self.assertEqual({(point["type"], point["bssid"], point["label"], point["lat"],
                           point["lon"], point["rssi"]) for point in points}, expected)
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(self.read_unchanged(), (set(), set(addresses)))


if __name__ == "__main__":
    unittest.main()
