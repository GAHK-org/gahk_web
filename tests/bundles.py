"""Fake release bundles for the updater and cgi-bin stub tests.

A fake release has the layout of a real one, but its gahk_web package only records which commands
the updater ran (in <home>/calls.log), so the tests need neither a database nor a real build. Its
requirements.lock names one tiny wheel, gahkfake, which real pip installs from a local folder
(write_wheelhouse; the updater's $GAHK_WEB_PIP_FIND_LINKS points pip there), so no test needs PyPI.
"""

import base64
import hashlib
import io
import json
import tarfile
import zipfile
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
    import gahkfake  # the release's package, from its vendor/

    with (Path(os.environ["GAHK_WEB_HOME"]) / "calls.log").open("a") as log:
        log.write(VERSION[:7] + " " + " ".join(argv) + "\\n")
    if argv[0] in FAILING:
        print(argv[0] + " broke", file=sys.stderr)
        return 1
    return 0
"""

# The self-test: the packages it is given (--vendor) must import.
FAKE_SELFTEST = """\
import sys
from pathlib import Path

vendor = sys.argv[2] if sys.argv[1:2] == ["--vendor"] else str(Path(__file__).resolve().parent / "vendor")
sys.path.insert(0, vendor)
import gahkfake

print("selftest ok with", gahkfake.__file__)
"""

# What the cgi-bin stub runs for a page: it reports the sys.path the release got.
FAKE_CGI_ENTRY = """\
import json
import sys


def main():
    import gahkfake

    body = json.dumps({"sys_path": sys.path, "gahkfake": gahkfake.__file__})
    sys.stdout.write("Status: 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n" + body)
"""


def _wheel(name: str, version: str) -> bytes:
    """A minimal pure-Python wheel, byte for byte the same on every run (so its hash is fixed)."""
    files = {
        f"{name}/__init__.py": b"VALUE = 1\n",
        f"{name}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{name}-{version}.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(
        f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
        for path, data in files.items()
    )
    files[f"{name}-{version}.dist-info/RECORD"] = (record + f"{name}-{version}.dist-info/RECORD,,\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        for path, data in files.items():
            wheel.writestr(zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


WHEELS = {"gahkfake-1.0-py3-none-any.whl": _wheel("gahkfake", "1.0")}
LOCK = f"gahkfake==1.0 \\\n    --hash=sha256:{hashlib.sha256(WHEELS['gahkfake-1.0-py3-none-any.whl']).hexdigest()}\n"
# A lock pip refuses: the hash does not match the wheel.
BAD_LOCK = f"gahkfake==1.0 \\\n    --hash=sha256:{'0' * 64}\n"


def write_wheelhouse(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in WHEELS.items():
        (folder / name).write_bytes(data)
    return folder


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
    htaccess: str = "",
    requirements: str = LOCK,
    extra: list[tarfile.TarInfo] | None = None,
) -> dict[str, Any]:
    """Write dist/gahk_web-<sha>.tar.gz and dist/manifest-<sha>.json; returns the manifest.

    failing may name "selftest", "migrate" or "rebuild" to make that step fail."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _file(tar, "VERSION", f"{sha}\n".encode())
        _file(tar, "requirements.lock", requirements.encode())
        selftest = "import sys\nprint('selftest broke')\nsys.exit(1)\n" if "selftest" in failing else ""
        _file(tar, "selftest.py", (selftest or FAKE_SELFTEST).encode(), 0o755)
        _file(tar, "src/gahk_web/__init__.py", b"")
        _file(tar, "src/gahk_web/__main__.py", FAKE_MAIN.format(version=sha, failing=failing).encode())
        _file(tar, "src/gahk_web/cgi_entry.py", FAKE_CGI_ENTRY.encode())
        _file(tar, "bin/updater.py", updater_source.encode(), 0o755)
        _file(tar, "public_html/.htaccess", (htaccess or f"# {sha}\n").encode())
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
        "python": ">=3.13",
    }
    (dist / manifest["bundle"]).write_bytes(data)
    (dist / f"manifest-{sha}.json").write_text(json.dumps(manifest))
    return manifest


def publish(dist: Path, manifest: dict[str, Any]) -> None:
    """Make manifest the production one (what CI's release job does last)."""
    (dist / "manifest.json").write_text(json.dumps(manifest))


# Nothing listens on port 1, so the updater's check of the site after a new .htaccess finds no site
# (and leaves the .htaccess alone) instead of asking the real gahk.dk.
NO_SITE = "http://127.0.0.1:1"


def write_config(home: Path, manifest_url: str, key: str, pin: str = "", base_url: str = NO_SITE) -> Path:
    config = home / "gahk_web" / "config.ini"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"[sync]\nkey = {key}\n\n[site]\nbase_url = {base_url}\n\n[update]\nmanifest_url = {manifest_url}\n"
        + (f"pin = {pin}\n" if pin else ""),
        encoding="utf-8",
    )
    return config
