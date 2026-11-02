"""Replace a successfully updated, already quiescent game process."""
import os
from pathlib import Path
import sys


def restart_game(root, arguments=None, executable=None):
    """Use the same interpreter and inherited environment, without a launcher.

    Callers must first finish saving and release every device. The application
    changes its working directory to its package while running; module startup
    instead needs the installation root. On failure, restore the old directory
    so the stopped game's retry/manual-relaunch screen remains usable.
    """
    root = Path(root).resolve(strict=True)
    if not (root / "watchdogs" / "__main__.py").is_file():
        raise RuntimeError("The installed game entry point is missing.")
    executable = sys.executable if executable is None else executable
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    previous = Path.cwd()
    os.chdir(root)
    try:
        os.execv(executable, [executable, "-B", "-m", "watchdogs", *arguments])
    except BaseException:
        os.chdir(previous)
        raise
    # Real exec never returns. A platform wrapper returning is also a failure.
    os.chdir(previous)
    raise RuntimeError("The game process was not restarted.")
