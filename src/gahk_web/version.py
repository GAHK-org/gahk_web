"""The running code version.

A release is unpacked to ~/gahk_web/releases/<sha>/ with this package at <release>/src/gahk_web and
a VERSION file (the git sha CI built from) at <release>/VERSION. A checkout has no VERSION file and
reports "dev".
"""

from functools import cache
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parents[2]


@cache
def code_version() -> str:
    try:
        version = (RELEASE_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"
    return version or "dev"
