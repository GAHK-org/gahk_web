"""Self-test of a release: does this code run with this python and the packages installed for it?

The release's selftest.py puts only <release>/src and the packages (<release>/vendor, or the folder
the updater has just installed them into) on sys.path and calls main(). The updater runs it with the
webhost's python3 before it switches to a release, and CI runs it on Python 3.13 and 3.14 for every
bundle. It needs no database, no config file and no network: it imports every module and package,
checks that the packages are the ones installed for this python and that their compiled parts work
(MarkupSafe's speedups, psycopg's libpq, Pillow's WebP and JPEG codecs and colour management, nh3),
loads the database migrations, makes the variants of a sample picture, renders a sample snapshot
with the release's templates into memory, checks the static files the pages link to, sends the
Flask app two requests the way Apache hands them over, and sets up the FastCGI server.
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
from typing import TYPE_CHECKING, Any

import gahk_web

if TYPE_CHECKING:
    from .config import Config

MIN_PYTHON = (3, 13)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The runtime dependencies, by import name (pyproject.toml's [project] dependencies).
DEPENDENCIES = (
    "flask",
    "werkzeug",
    "flup",
    "jinja2",
    "markupsafe",
    "nh3",
    "PIL",
    "psycopg",
    "psycopg_binary",
    "sqlalchemy",
    "flask_sqlalchemy",
    "alembic",
    "mako",
)


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
    """Pillow's compiled codecs: a tiny WebP and JPEG, written and read back in memory. Then the
    variants of a turned JPEG with a colour profile (littlecms, the JPEG decoder's scaling)."""
    from PIL import Image, ImageCms, features

    from .variants import make

    if not features.check("webp"):
        raise AssertionError("Pillow was built without WebP")
    for kind in ("WEBP", "JPEG"):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), (0, 96, 64)).save(buffer, kind, quality=80)
        with Image.open(io.BytesIO(buffer.getvalue())) as image:
            image.load()
            if image.format != kind or image.size != (8, 6):
                raise AssertionError(f"a {kind} image did not survive a round trip")
    exif = Image.Exif()
    exif[0x0112] = 6  # turned: stored lying down
    buffer = io.BytesIO()
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (1000, 500), (0, 96, 64)).save(buffer, "JPEG", exif=exif.tobytes(), icc_profile=srgb)
    made = make(buffer.getvalue())
    sizes = [(width, Image.open(io.BytesIO(data)).size) for width, data in made.files]
    if (made.width, made.height) != (500, 1000) or sizes != [(480, (480, 960)), (500, (500, 1000))]:
        raise AssertionError(f"unexpected variants: {made.width}x{made.height}, {sizes}")


def check_sanitizer() -> None:
    from .bodies import sanitize

    cleaned = sanitize(
        '<p onclick="x()">Hej <b>alle</b><script>alert(1)</script><a href="javascript:x()">x</a></p>'
    )
    if cleaned != '<p>Hej <b>alle</b><a rel="noopener noreferrer">x</a></p>':
        raise AssertionError(f"the sanitiser left {cleaned!r}")


def sample_config(scratch: Path) -> "Config":
    from .config import Config, DatabaseConfig

    return Config(
        database=DatabaseConfig("localhost", 5432, "selftest", "selftest", "", sslmode="disable"),
        sync_key="k" * 40,
        stats_secret="s" * 40,
        public_html=scratch,
        state_dir=scratch,
    )


def check_render() -> dict[str, tuple[str, str | None]]:
    from .images import Picture
    from .render import Site
    from .snapshot import validate

    snapshot = validate(sample_snapshot())
    with tempfile.TemporaryDirectory() as scratch:
        cfg = sample_config(Path(scratch))
        url = snapshot["images"][0]["url"]
        variants = tuple((width, f"/billeder/0123456789abcdef/_{width}w.webp") for width in (480, 960))
        pictures = {url: Picture(url, 960, 640, variants)}
        files = Site(cfg, snapshot, date(2026, 11, 12), pictures=pictures).files()
    home, kokken = files["index.html"][0], files["faciliteter/kokken/index.html"][0]
    if "Forside" not in home or "Julefrokost" not in home or "Køkkenet &lt;&amp;&gt;" not in kokken:
        raise AssertionError("the pages did not render the sample snapshot")
    if 'srcset="/billeder/0123456789abcdef/_480w.webp 480w' not in kokken:
        raise AssertionError("the pages do not link the picture variants")
    return files


def check_app() -> None:
    """Two requests through the whole Flask stack, rewritten to the stub as .htaccess does, that need
    no database: an old intranet address (redirect) and an unsigned API call (refused)."""
    from .app import create_app

    with tempfile.TemporaryDirectory() as scratch:
        client = create_app(sample_config(Path(scratch))).test_client()
        stub = {"SCRIPT_NAME": "/cgi-bin/gahk.fcgi", "REQUEST_URI": "/intern/den-hurtige/?side=2"}
        moved = client.get("/den-hurtige/", environ_overrides=stub)
        location = "https://intern.gahk.dk/den-hurtige/?side=2"
        if moved.status_code != 308 or moved.headers.get("Location") != location:
            raise AssertionError(f"/intern/den-hurtige/ answered {moved.status} {moved.headers}")
        refused = client.get("/api/status")
        if refused.status_code != 401 or refused.get_json() != {"error": "missing signature"}:
            raise AssertionError(f"an unsigned /api/status answered {refused.status} {refused.data!r}")
    # flup's FastCGI server, set up as cgi-bin/gahk.fcgi sets it up (it serves only under mod_fcgid).
    from .fcgi_entry import RestartPolicy, Server

    server = Server(client.application, RestartPolicy(()))
    if server.capability.get("FCGI_MAX_REQS") != 1 or server.multithreaded:
        raise AssertionError("flup's FastCGI server is not the single-threaded one")


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
        "public_html/cgi-bin/gahk.fcgi",
        "bin/updater.py",
        "requirements.lock",
    ):
        if not (release / required).is_file():
            raise AssertionError(f"the release has no {required}")


def check_migrations() -> None:
    """The release's Alembic revisions load (every file is imported) and form one line, and the
    models map onto tables."""
    from alembic.script import ScriptDirectory
    from sqlalchemy.orm import configure_mappers

    from .db import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    revisions = list(script.walk_revisions())
    if not revisions:
        raise AssertionError("no database migrations found")
    if len(script.get_heads()) != 1:
        raise AssertionError(f"the migrations have several heads: {script.get_heads()}")
    configure_mappers()


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
        ("app", check_app),
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
