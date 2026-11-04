"""Present shared release Markdown in the existing bitmap-font game window."""

import re
import textwrap
import unicodedata


_PUNCTUATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u2192": "->",
    "\u00a0": " ",
})


def _game_text(text):
    # The release source keeps its Markdown/emoji for GitHub and Discord. Only
    # its display projection uses the printable ASCII bitmap-font repertoire.
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = unicodedata.normalize("NFKD", text.translate(_PUNCTUATION))
    return "".join(char for char in text if " " <= char <= "~")


def game_note_lines(notes, width=83):
    """Derive wrapped WDG notes without modifying their canonical source.

    Markdown section titles become the numbered uppercase headings already
    used by the original window. Legacy RELEASE/numbered plain-text notes keep
    their existing layout. Version selection must happen before this projection.
    """
    lines = []
    heading_number = 0
    for paragraph in notes.splitlines():
        paragraph = paragraph.expandtabs(4)
        heading = re.fullmatch(r"\s*#{1,6}\s+(.+?)(?:\s+#+)?\s*", paragraph)
        if heading:
            heading_number += 1
            paragraph = f"{heading_number}. {_game_text(heading.group(1)).strip().upper()}"
            if lines and lines[-1]:
                lines.append("")
        else:
            paragraph = _game_text(paragraph)
        lines.extend(textwrap.wrap(paragraph, width=width, break_long_words=True,
                                   break_on_hyphens=False) or [""])
    return lines if any(lines) else ["No release notes provided."]
