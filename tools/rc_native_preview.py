#!/usr/bin/env python3
"""Render the real update-window methods/font with synthetic state, no game init.

Run with SDL_VIDEODRIVER=offscreen SDL_AUDIODRIVER=dummy in Cloud.
This is not a physical uConsole screenshot or an installed game.
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import pyxel
from PIL import Image
import test_game_update_ui as fixture


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    notes = (ROOT / 'docs/UPDATE_PREVIEW.txt').read_text(encoding='utf-8')
    pyxel.init(640, 360, title='Cloud synthetic RC notes - no device', fps=30)
    font = pyxel.Font(str(ROOT / 'assets/spleen-5x8.bdf'))

    class Native:
        def __getattr__(self, name):
            return getattr(pyxel, name)

        def text(self, x, y, text, color, selected_font=None):
            pyxel.text(x, y, text, color, font if selected_font is None else selected_font)

    g = fixture.game()
    fixture.ENV['pyxel'] = Native()
    g._game_updates.source_info.return_value = dict(repository_id=1442123109,
        repository='KiloGramowy/WDG-Wars-Go-2.0-KG-Edition',
        url='https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition', verified=True)
    g._gameupd_avatar = pyxel.Image.from_image(str(ROOT / 'assets/kilogramowy-avatar-32.png'))
    g._game_installed_version = '0.9.21'
    g._game_update_release = None
    g._game_update_available = False
    g._game_update_message = 'PRIVATE RC / synthetic Cloud preview / no install'
    lines = g._gameupd_note_lines()
    pages = []
    starts = list(range(0, len(lines), 9))
    if starts[-1] != max(0, len(lines)-9):
        starts.append(max(0, len(lines)-9))
    palette = list(pyxel.colors)
    for start in starts:
        pyxel.cls(0)
        g._gameupd_scroll = start
        g._draw_gameupd_screen()
        pyxel.text(10, 340, 'CLOUD SYNTHETIC - NOT A PHYSICAL UCONSOLE', 10, font)
        im = Image.new('RGB', (640, 360))
        im.putdata([((palette[c] >> 16) & 255, (palette[c] >> 8) & 255, palette[c] & 255)
                    for y in range(360) for x in range(640)
                    for c in [pyxel.pget(x, y)]])
        pages.append(im)
    for name, index in [('first', 0), ('middle', len(pages)//2), ('last', -1)]:
        pages[index].save(out / ('notes-'+name+'.png'))
    contact = Image.new('RGB', (640*3, 360*((len(pages)+2)//3)))
    for i, page in enumerate(pages):
        contact.paste(page, ((i % 3)*640, (i // 3)*360))
    contact.save(out/'notes-all-pages.png')
    report = dict(status='PASS', evidence='Cloud offscreen native Pyxel, synthetic state only',
                  renderer_version=pyxel.VERSION, source_notes_sha256=hashlib.sha256(notes.encode()).hexdigest(),
                  lines=len(lines), max_columns=max(map(len, lines)), pages_rendered=len(pages),
                  glyph_font='assets/spleen-5x8.bdf', game_initialized=False, hardware_test=False)
    (out/'native-render.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
