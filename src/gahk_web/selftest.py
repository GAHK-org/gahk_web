"""Self-test of a release: does this code run with this python and the packages installed for it?

The release's selftest.py puts only <release>/src and the packages (<release>/vendor, or the folder
the updater has just installed them into) on sys.path and calls main(). The updater runs it with the
webhost's python3 before it switches to a release, and CI runs it on Python 3.13 and 3.14 for every
bundle. It needs no database, no config file and no network: it imports every module and package,
checks that the packages are the ones installed for this python and that their compiled parts work
(MarkupSafe's speedups, psycopg's libpq, Pillow's WebP and JPEG codecs, nh3), renders a sample
snapshot with the release's templates into memory, and checks the static files the pages link to.
"""

import importlib
import io
import pkgutil
import re
import sys
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import gahk_web

MIN_PYTHON = (3, 13)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The runtime dependencies, by import name (pyproject.toml's [project] dependencies).
DEPENDENCIES = ("jinja2", "markupsafe", "nh3", "PIL", "psycopg", "psycopg_binary")


def sample_snapshot() -> dict[str, Any]:
    image = {
        "url": "/billeder/0123456789abcdef/gang.jpg",
        "sha256": "0123456789abcdef" * 4,
        "content_type": "image/jpeg",
        "size": 1,
    }
    return {
        "format": 1,
        "revision": "0" * 64,
        "published_at": "2026-11-11T12:00:00+01:00",
        "site": {"front_page_id": 1, "default_background": image["url"]},
        "legacy_images": {"/static/legacy/image/upload/images/gang.jpg": image["url"]},
        "pages": [
            {
                "id": 1,
                "path": "",
                "title": "Velkommen",
                "body": "<p>Forside</p>",
                "background": "",
                "description": "",
                "updated_at": "2026-11-01T10:00:00+01:00",
            },
            {
                "id": 2,
                "path": "faciliteter/kokken",
                "title": "Køkkenet <&>",
                "body": f'<p><img src="{image["url"]}" alt=""></p>',
                "background": image["url"],
                "description": "Køkkenet",
                "updated_at": "2026-11-01T10:00:00+01:00",
            },
        ],
        "news": [
            {"id": 1, "title": "Nyhed", "body": "<p>Nu</p>", "published_at": "2026-11-01T12:00:00+01:00"}
        ],
        "events": [{"id": 1, "title": "Julefrokost", "description": "", "starts_on": "2026-12-01"}],
        "menu": [{"label": "Hjem", "url": "/"}, {"label": "Begivenheder", "url": "/begivenheder/"}],
        "redirects": {"gammel": "faciliteter/kokken"},
        "images": [image],
    }


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise AssertionError(f"Python {sys.version.split()[0]} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")


def check_imports() -> None:
    for module in pkgutil.iter_modules(gahk_web.__path__, "gahk_web."):
        importlib.import_module(module.name)
    for name in DEPENDENCIES:
        importlib.import_module(name)
    import markupsafe

    if str(markupsafe.escape("<a&b>")) != "&lt;a&amp;b&gt;":
        raise AssertionError("markupsafe does not escape")


def check_packages(vendor: Path | None) -> None:
    """The packages come from vendor/, were installed for this python, and their compiled parts load."""
    importlib.import_module("markupsafe._speedups")  # MarkupSafe's C escaping, not the slow fallback
    import psycopg

    # The binary wheel brings libpq; without it psycopg would look for the host's libpq and fail.
    if psycopg.pq.__impl__ != "binary":
        raise AssertionError(f"psycopg uses its {psycopg.pq.__impl__} implementation, not binary")
    if vendor is None:
        return
    from .updater import ABI_FILE, interpreter_abi

    installed = (vendor / ABI_FILE).read_text(encoding="utf-8").strip()
    if installed != interpreter_abi():
        raise AssertionError(f"the packages are for {installed!r}, this python is {interpreter_abi()!r}")
    for name in DEPENDENCIES:
        file = getattr(sys.modules[name], "__file__", None) or ""
        if not Path(file).resolve().is_relative_to(vendor.resolve()):
            raise AssertionError(f"{name} was imported from {file}, not from {vendor}")


def check_images() -> None:
    """Pillow's compiled codecs: a tiny WebP and JPEG, written and read back in memory."""
    from PIL import Image, features

    if not features.check("webp"):
        raise AssertionError("Pillow was built without WebP")
    for kind in ("WEBP", "JPEG"):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), (0, 96, 64)).save(buffer, kind, quality=80)
        with Image.open(io.BytesIO(buffer.getvalue())) as image:
            image.load()
            if image.format != kind or image.size != (8, 6):
                raise AssertionError(f"a {kind} image did not survive a round trip")


def check_sanitizer() -> None:
    from .bodies import sanitize

    cleaned = sanitize(
        '<p onclick="x()">Hej <b>alle</b><script>alert(1)</script><a href="javascript:x()">x</a></p>'
    )
    if cleaned != '<p>Hej <b>alle</b><a rel="noopener noreferrer">x</a></p>':
        raise AssertionError(f"the sanitiser left {cleaned!r}")


def check_render() -> dict[str, tuple[str, str | None]]:
    from .config import Config, DatabaseConfig
    from .render import Site
    from .snapshot import validate

    snapshot = validate(sample_snapshot())
    with tempfile.TemporaryDirectory() as scratch:
        cfg = Config(
            database=DatabaseConfig("localhost", 5432, "selftest", "selftest", "", sslmode="disable"),
            sync_key="k" * 40,
            stats_secret="s" * 40,
            public_html=Path(scratch),
            state_dir=Path(scratch),
        )
        files = Site(cfg, snapshot, date(2026, 11, 12)).files()
    home, kokken = files["index.html"][0], files["faciliteter/kokken/index.html"][0]
    if "Forside" not in home or "Julefrokost" not in home or "Køkkenet &lt;&amp;&gt;" not in kokken:
        raise AssertionError("the pages did not render the sample snapshot")
    return files


def check_static(release: Path | None, files: dict[str, tuple[str, str | None]]) -> None:
    """Every /static/ file the rendered pages link to is in the release's public_html/static."""
    if release is None:
        return
    static_root = release / "public_html" / "static"
    linked = set()
    for content, _url in files.values():
        linked.update(re.findall(r"""["'(]/static/([^"')?#]+)""", content))
    missing = sorted(path for path in linked if not (static_root / path).is_file())
    if missing:
        raise AssertionError(f"pages link to static files the release lacks: {', '.join(missing)}")


def check_release(release: Path | None) -> None:
    if release is None:
        return
    version = (release / "VERSION").read_text(encoding="utf-8").strip()
    if not SHA_RE.match(version):
        raise AssertionError(f"VERSION is not a git sha: {version!r}")
    for required in (
        "public_html/.htaccess",
        "public_html/cgi-bin/gahk.cgi",
        "bin/updater.py",
        "requirements.lock",
    ):
        if not (release / required).is_file():
            raise AssertionError(f"the release has no {required}")


def check_migrations() -> None:
    from .db import migration_files

    if not migration_files():
        raise AssertionError("no database migrations found")


def main(release: Path | None = None, vendor: Path | None = None) -> int:
    rendered: dict[str, tuple[str, str | None]] = {}

    def render() -> None:
        rendered.update(check_render())

    checks: list[tuple[str, Callable[[], None]]] = [
        ("python", check_python),
        ("imports", check_imports),
        ("packages", lambda: check_packages(vendor)),
        ("images", check_images),
        ("sanitizer", check_sanitizer),
        ("release files", lambda: check_release(release)),
        ("migrations", check_migrations),
        ("render", render),
        ("static files", lambda: check_static(release, rendered)),
    ]
    failed = 0
    for name, check in checks:
        try:
            check()
        except Exception as exc:  # report every failing check, not only the first
            failed += 1
            print(f"selftest: {name}: FAILED: {exc!r}", file=sys.stderr)
    if failed:
        return 1
    from .version import code_version

    print(f"selftest ok: gahk_web {code_version()} on Python {sys.version.split()[0]}")
    return 0
