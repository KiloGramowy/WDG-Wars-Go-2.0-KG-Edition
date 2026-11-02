"""Versioned note presentation; never changes the approved install target."""
import unittest

from watchdogs.game_updates import filter_release_notes


PREAMBLE = "One confirmation installs every missing fix.\nRestart after installation.\n\n"
LATEST = "RELEASE 0.9.19 - MAP MICROFIX\n1. MAP VISIBILITY\nCurrent map fixes.\n\n"
PREVIOUS = "RELEASE 0.9.18 - PREVIOUS FIXES\n1. XP AND PLAYER DATA\nEarlier XP and BLE fixes.\n"
NOTES = PREAMBLE + LATEST + PREVIOUS


class ReleaseNoteFilterTests(unittest.TestCase):
    def test_017_sees_both_missing_releases_with_common_instructions(self):
        self.assertEqual(filter_release_notes(NOTES, "0.9.17", "0.9.19"), NOTES)

    def test_018_sees_only_019_with_common_instructions(self):
        self.assertEqual(filter_release_notes(NOTES, "0.9.18", "0.9.19"), PREAMBLE + LATEST)

    def test_current_version_can_still_read_latest_release(self):
        self.assertEqual(filter_release_notes(NOTES, "0.9.19", "0.9.19"), PREAMBLE + LATEST)
        self.assertEqual(filter_release_notes(NOTES, "0.9.19.0", "0.9.19"), PREAMBLE + LATEST)

    def test_versions_compare_numerically_and_keep_source_order(self):
        newest = "RELEASE 0.9.100 - LATER FIXES\nLatest changes.\n\n"
        notes = PREAMBLE + newest + LATEST + PREVIOUS
        self.assertEqual(filter_release_notes(notes, "0.9.19", "0.9.100"), PREAMBLE + newest)
        self.assertEqual(filter_release_notes(notes, "0.9.18", "0.9.100"), PREAMBLE + newest + LATEST)

    def test_four_component_microfix_shows_only_missing_release_sections(self):
        microfix = "RELEASE 0.9.19.1 - ADS-B SNAPSHOT MICROFIX\nUpdated aircraft lifetime.\n\n"
        notes = PREAMBLE + microfix + LATEST + PREVIOUS
        for installed, expected in (
                ("0.9.17", microfix + LATEST + PREVIOUS),
                ("0.9.18", microfix + LATEST),
                ("0.9.19", microfix),
                ("0.9.19.0", microfix),
                ("0.9.19.1", microfix)):
            with self.subTest(installed=installed):
                self.assertEqual(filter_release_notes(notes, installed, "0.9.19.1"),
                                 PREAMBLE + expected)

    def test_four_component_microfix_versions_compare_numerically(self):
        first = "RELEASE 0.9.19.1 - FIRST MICROFIX\nFirst change.\n\n"
        second = "RELEASE 0.9.19.2 - SECOND MICROFIX\nSecond change.\n\n"
        tenth = "RELEASE 0.9.19.10 - TENTH MICROFIX\nTenth change.\n\n"
        notes = PREAMBLE + tenth + second + first + LATEST
        self.assertEqual(filter_release_notes(notes, "0.9.19.2", "0.9.19.10"),
                         PREAMBLE + tenth)
        self.assertEqual(filter_release_notes(notes, "0.9.19.1", "0.9.19.10"),
                         PREAMBLE + tenth + second)

    def test_legacy_unstructured_notes_are_unchanged(self):
        notes = "1. XP AND PLAYER DATA\nBefore: old state.\nChanged: new state.\n"
        self.assertEqual(filter_release_notes(notes, "0.9.18", "0.9.19"), notes)

    def test_invalid_versions_preserve_all_information(self):
        for installed, latest in (("unknown", "0.9.19"), ("0.9.18", "bad"),
                                  (None, "0.9.19"), ("0.9.18", None), ("0.9.20", "0.9.19")):
            with self.subTest(installed=installed, latest=latest):
                self.assertEqual(filter_release_notes(NOTES, installed, latest), NOTES)

    def test_missing_latest_block_preserves_all_information(self):
        self.assertEqual(filter_release_notes(PREAMBLE + PREVIOUS, "0.9.18", "0.9.19"), PREAMBLE + PREVIOUS)

    def test_unknown_or_malformed_release_blocks_are_never_hidden(self):
        for header in ("RELEASE NEXT - UNKNOWN", "RELEASE0.9.17 - BROKEN",
                       "RELEASE 0.9.17 - lower case", "RELEASE 0.9.20 - FUTURE",
                       "RELEASE 0.9.18 - DUPLICATE", "RELEASE 0.9.18.0 - DUPLICATE"):
            notes = NOTES + "\n" + header + "\nImportant unrecognised information.\n"
            with self.subTest(header=header):
                self.assertEqual(filter_release_notes(notes, "0.9.18", "0.9.19"), notes)

    def test_section_content_and_line_endings_are_preserved(self):
        notes = NOTES.replace("\n", "\r\n")
        self.assertEqual(filter_release_notes(notes, "0.9.18", "0.9.19"),
                         (PREAMBLE + LATEST).replace("\n", "\r\n"))

    def test_oversized_numeric_heading_falls_back_to_complete_notes(self):
        notes = NOTES + "\nRELEASE 0.9." + "1" * 5000 + " - MALFORMED\nKeep this text.\n"
        self.assertEqual(filter_release_notes(notes, "0.9.18", "0.9.19"), notes)


if __name__ == "__main__":
    unittest.main()
