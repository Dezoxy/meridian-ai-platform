"""The only place this spike opens a file, so that a test can record every
path that is read. Nothing is ever written."""

from pathlib import Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
