"""Keep the prepared Release/Discord, runtime and bitmap delivery in sync."""
from pathlib import Path
import unittest
from watchdogs.release_notes import game_note_lines

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'docs/ota-0.9.21'


class ReleaseCopyConsistencyTests(unittest.TestCase):
    def test_runtime_is_the_exact_canonical_release_discord_body(self):
        self.assertEqual((ROOT / 'docs/UPDATE_PREVIEW.txt').read_bytes(),
                         (PACK / 'RELEASE_AND_DISCORD_EN.md').read_bytes())

    def test_delivered_bitmap_draft_is_the_actual_formatter_projection(self):
        canonical = (PACK / 'RELEASE_AND_DISCORD_EN.md').read_text(encoding='utf-8')
        self.assertEqual((PACK / 'UPDATE_GAME_DRAFT.txt').read_text(encoding='utf-8'),
                         '\n'.join(game_note_lines(canonical))+'\n')
