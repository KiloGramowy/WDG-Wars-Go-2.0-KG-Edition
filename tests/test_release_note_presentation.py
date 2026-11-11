"""The real shared candidate description projected into WDG's bitmap font."""

from pathlib import Path
import re
import unittest

from watchdogs.game_updates import filter_release_notes
from watchdogs.release_notes import game_note_lines


ROOT = Path(__file__).resolve().parents[1]


class ReleaseNotePresentationTests(unittest.TestCase):
    def candidate(self):
        return (ROOT / "docs" / "UPDATE_PREVIEW.txt").read_text(encoding="utf-8")

    def test_real_candidate_is_readable_in_the_original_font_and_width(self):
        notes = self.candidate()
        lines = game_note_lines(notes)
        text = "\n".join(lines)
        glyphs = {int(value) for value in re.findall(
            r"^ENCODING (\d+)$", (ROOT / "assets" / "spleen-5x8.bdf").read_text(), re.M)}
        self.assertIn("## ", notes)  # Exercise the shared Markdown, not legacy fixtures.
        self.assertIn("**Before:", notes)
        self.assertNotIn("##", text)
        self.assertNotIn("**", text)
        self.assertTrue(all(len(line) <= 83 for line in lines))
        self.assertTrue(all(ord(char) in glyphs for line in lines for char in line))
        self.assertTrue(all(char.isascii() for char in text))
        self.assertEqual(self.candidate(), notes)
        # The display must retain the actual candidate's facts and instructions.
        words = " ".join(text.split())
        for fact in ("Before:", "Now:", "0.9.20", "720 minutes",
                     "110 ms", "1000 ms", "10,000", "6 MiB", "Enter or Esc",
                     "0.9.17, 0.9.18 or 0.9.19", "0.9.19.1 or later",
                     "Normal Piglet", "60-second", "5.0.0", "Keep all"):
            with self.subTest(fact=fact):
                self.assertIn(fact, words)

    def test_actual_candidate_headings_become_numbered_uppercase_titles(self):
        notes = self.candidate()
        titles = re.findall(r"^## (.+)$", notes, re.M)
        lines = game_note_lines(notes)
        projected = [line for line in lines if re.fullmatch(
            r"\d{1,2}\. [A-Z][A-Z0-9 /()+,.'&:_-]*", line)]
        self.assertEqual(len(projected), len(titles))
        for number, heading in enumerate(projected, 1):
            with self.subTest(heading=heading):
                self.assertTrue(heading.startswith(f"{number}. "))
                self.assertEqual(heading, heading.upper())
                self.assertRegex(heading, r"^\d{1,2}\. [A-Z][A-Z0-9 /()+,.'&:_-]*$")
                self.assertEqual(lines[lines.index(heading) - 1], "")

    def test_real_candidate_filter_keeps_only_missing_blocks_for_older_users(self):
        candidate = self.candidate() + "\n"
        older = (
            "RELEASE 0.9.19.2 - PREVIOUS RELEASE\nEarlier fixture: clean-map instructions.\n\n",
            "RELEASE 0.9.19.1 - FIRST MICROFIX\nEarlier fixture: microfix instructions.\n\n",
            "RELEASE 0.9.19 - MAP MICROFIX\nEarlier fixture: map instructions.\n\n",
            "RELEASE 0.9.18 - PREVIOUS FIXES\nEarlier fixture: player instructions.\n\n",
        )
        notes = candidate + "".join(older)
        for installed, kept in (("0.9.17", 4), ("0.9.18", 3), ("0.9.19", 2),
                                ("0.9.19.1", 1), ("0.9.19.2", 0), ("0.9.20", 0)):
            with self.subTest(installed=installed):
                selected = filter_release_notes(notes, installed, "0.9.20")
                self.assertEqual(selected, candidate + "".join(older[:kept]))
                text = "\n".join(game_note_lines(selected))
                self.assertIn(candidate.splitlines()[0], text)
                self.assertEqual(text.count("Earlier fixture:"), kept)
                self.assertNotIn("##", text)
                self.assertEqual(notes, candidate + "".join(older))

    def test_legacy_plain_text_keeps_its_layout(self):
        notes = ("RELEASE 0.9.19 - MAP MICROFIX\n\n1. XP AND PLAYER DATA\n"
                 "Before: old state.\nNow: saved state.\n\n---\n")
        self.assertEqual(game_note_lines(notes), notes.splitlines())
        self.assertEqual(game_note_lines(""), ["No release notes provided."])

    def test_typographic_punctuation_keeps_english_meaning_and_long_lines_wrap(self):
        notes = "## \U0001f4e1 MC / MT: this session\u2019s nodes\n**Before / Now:** \u201cR\u201d \u2014 5\u201310 minutes; 0.9.19.1 \u2192 0.9.19.2\u2026\n" + "x" * 200
        lines = game_note_lines(notes)
        self.assertEqual(lines[0], "1. MC / MT: THIS SESSION'S NODES")
        self.assertEqual(lines[1], 'Before / Now: "R" - 5-10 minutes; 0.9.19.1 -> 0.9.19.2...')
        self.assertTrue(all(len(line) <= 83 for line in lines))


if __name__ == "__main__":
    unittest.main()
