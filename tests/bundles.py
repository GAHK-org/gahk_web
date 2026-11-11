"""Fake release bundles for the updater and cgi-bin stub tests.

A fake release has the layout of a real one, but its gahk_web package only records which commands
the updater ran (in <home>/calls.log), so the tests need neither a database nor a real build.
"""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UPDATER_SOURCE = (ROOT / "src" / "gahk_web" / "updater.py").read_text(encoding="utf-8")
STUB = ROOT / "public_html" / "cgi-bin" / "gahk.cgi"

FAKE_MAIN = """\
import os
import sys
from pathlib import Path

VERSION = {version!r}
FAILING = {failing!r}


def main(argv):
    with (Path(os.environ["GAHK_WEB_HOME"]) / "calls.log").open("a") as log:
        log.write(VERSION[:7] + " " + " ".join(argv) + "\\n")
    if argv[0] in FAILING:
        print(argv[0] + " broke", file=sys.stderr)
        return 1
    return 0
"""


def version(n: int) -> str:
    return hashlib.sha1(f"release {n}".encode()).hexdigest()


def _file(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def css_name(sha: str) -> str:
    """The hashed stylesheet a fake release ships (different in every release)."""
    return f"css/site.{hashlib.sha256(sha.encode()).hexdigest()[:12]}.css"


def make_bundle(
    dist: Path,
    sha: str,
    *,
    failing: tuple[str, ...] = (),
    updater_source: str = UPDATER_SOURCE,
    extra: list[tarfile.TarInfo] | None = None,
) -> dict[str, Any]:
    """Write dist/gahk_web-<sha>.tar.gz and dist/manifest-<sha>.json; returns the manifest.

    failing may name "selftest", "migrate" or "rebuild" to make that step fail."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _file(tar, "VERSION", f"{sha}\n".encode())
        selftest = "import sys\nprint('selftest broke')\nsys.exit(1)\n" if "selftest" in failing else ""
        _file(tar, "selftest.py", (selftest or "print('ok')\n").encode(), 0o755)
        _file(tar, "src/gahk_web/__init__.py", b"")
        _file(tar, "src/gahk_web/__main__.py", FAKE_MAIN.format(version=sha, failing=failing).encode())
        _file(tar, "bin/updater.py", updater_source.encode(), 0o755)
        _file(tar, "public_html/.htaccess", f"# {sha}\n".encode())
        _file(tar, "public_html/cgi-bin/gahk.cgi", f"#!/usr/bin/env python3\n# {sha}\n".encode(), 0o755)
        _file(tar, f"public_html/static/{css_name(sha)}", f"/* {sha} */".encode())
        _file(tar, "public_html/static/fonts/ubuntu.woff2", b"font")
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
    data = buffer.getvalue()
    manifest = {
        "version": sha,
        "bundle": f"gahk_web-{sha}.tar.gz",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "built_at": "2026-11-12T00:00:00+00:00",
        "python": ">=3.11",
    }
    (dist / manifest["bundle"]).write_bytes(data)
    (dist / f"manifest-{sha}.json").write_text(json.dumps(manifest))
    return manifest


def publish(dist: Path, manifest: dict[str, Any]) -> None:
    """Make manifest the production one (what CI's release job does last)."""
    (dist / "manifest.json").write_text(json.dumps(manifest))


def write_config(home: Path, manifest_url: str, key: str, pin: str = "") -> Path:
    config = home / "gahk_web" / "config.ini"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"[sync]\nkey = {key}\n\n[update]\nmanifest_url = {manifest_url}\n"
        + (f"pin = {pin}\n" if pin else ""),
        encoding="utf-8",
    )
    return config
