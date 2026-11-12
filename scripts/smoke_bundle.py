#!/usr/bin/env python3
"""Smoke-test a built bundle the way the webhost will use it: python scripts/smoke_bundle.py

    --python 3.11 --python 3.13   interpreters to test with (versions uv can find, or paths)
    --database postgres://...     also do a full bootstrap install against this (scratch!) database

For each python it unpacks dist/gahk_web-<sha>.tar.gz into a temporary folder and runs
`python -I selftest.py` and `python -I bin/updater.py --selftest` there. With --database it then
plays the webhost: a fresh home with config.ini and the three bootstrap files from dist/
(updater.py, gahk.cgi, htaccess), a signed POST /api/update through the CGI stub (installs the
bundle from a file:// manifest), GET /api/health, a signed GET /api/status, a signed publish of a
small snapshot (renders the pages with the release's templates), a page fetched through the stub's
fallback, and a second POST /api/update that must change nothing. The database's tables are
dropped first: never point it at anything but a throwaway database.
"""

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


class SmokeError(Exception):
    pass


def find_python(version: str) -> str:
    if os.sep in version:
        return version
    done = subprocess.run(  # noqa: S603
        ["uv", "python", "find", "--system", version],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise SmokeError(f"no Python {version}: {done.stderr.strip()}")
    return done.stdout.strip()


def load_updater(path: Path) -> ModuleType:
    sys.dont_write_bytecode = True  # leave no __pycache__ in dist/
    spec = importlib.util.spec_from_file_location("smoke_updater", path)
    if spec is None or spec.loader is None:
        raise SmokeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check(python: str, *args: str, cwd: Path) -> str:
    done = subprocess.run([python, *args], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        raise SmokeError(f"{python} {' '.join(args)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def selftests(python: str, bundle: Path, updater: ModuleType, scratch: Path) -> None:
    release = scratch / "unpacked"
    updater.unpack(bundle, release)
    print(f"  {check(python, '-I', 'selftest.py', cwd=release)}")
    print(f"  {check(python, '-I', 'bin/updater.py', '--selftest', cwd=release)}")


# --- the bootstrap install, through the CGI stub ---


def config_ini(database: str, key: str, manifest_url: str) -> str:
    url = urlsplit(database)
    return f"""[database]
host = {url.hostname or "localhost"}
port = {url.port or 5432}
name = {url.path.lstrip("/")}
user = {url.username or ""}
password = {url.password or ""}
sslmode = disable

[sync]
key = {key}

[stats]
secret = {secrets.token_urlsafe(48)}

[site]
base_url = https://gahk.dk

[update]
manifest_url = {manifest_url}
"""


def signed_headers(key: str, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = f"{timestamp}\n{method}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    signature = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"X-Gahk-Timestamp": timestamp, "X-Gahk-Signature": signature}


def cgi(
    python: str, home: Path, method: str, path: str, body: bytes = b"", key: str = ""
) -> tuple[int, bytes]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GAHK_WEB_HOME": str(home),
        "REQUEST_METHOD": method,
        "REQUEST_URI": path,
        "PATH_INFO": path,
        "SCRIPT_NAME": "/cgi-bin/gahk.cgi",
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/json",
        "SERVER_NAME": "gahk.dk",
        "SERVER_PORT": "443",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTPS": "on",
        "REMOTE_ADDR": "192.0.2.1",
    }
    if key:
        for name, value in signed_headers(key, method, path, body).items():
            env["HTTP_" + name.upper().replace("-", "_")] = value
    stub = home / "public_html" / "cgi-bin" / "gahk.cgi"
    done = subprocess.run(  # noqa: S603 - as Apache runs it: the stub's own shebang interpreter
        [python, str(stub)], input=body, env=env, capture_output=True, timeout=120, check=False
    )
    head, _, content = done.stdout.partition(b"\r\n\r\n")
    status_line = next((line for line in head.decode().splitlines() if line.startswith("Status: ")), "")
    if not status_line:
        raise SmokeError(f"{method} {path}: no CGI status line\n{done.stdout!r}\n{done.stderr.decode()}")
    return int(status_line.split()[1]), content


def expect_json(result: tuple[int, bytes], status: int, what: str) -> Any:  # noqa: ANN401
    code, body = result
    if code != status:
        raise SmokeError(f"{what}: HTTP {code}, expected {status}: {body.decode(errors='replace')}")
    return json.loads(body)


SNAPSHOT: dict[str, Any] = {
    "format": 1,
    "revision": "5" * 64,
    "published_at": "2026-11-12T12:00:00+01:00",
    "site": {"front_page_id": 1, "default_background": ""},
    "legacy_images": {},
    "pages": [
        {
            "id": 1,
            "path": "",
            "title": "Velkommen",
            "body": "<p>Smoke</p>",
            "background": "",
            "description": "",
            "updated_at": "2026-11-12T10:00:00+01:00",
        },
        {
            "id": 2,
            "path": "faciliteter",
            "title": "Faciliteter",
            "body": "<p>Røgtest</p>",
            "background": "",
            "description": "",
            "updated_at": "2026-11-12T10:00:00+01:00",
        },
    ],
    "news": [],
    "events": [],
    "menu": [{"label": "Hjem", "url": "/"}, {"label": "Faciliteter", "url": "/faciliteter/"}],
    "redirects": {},
    "images": [],
}


def install(python: str, dist: Path, version: str, database: str, scratch: Path) -> None:
    home = scratch / "home"
    key = secrets.token_urlsafe(48)
    (home / "gahk_web" / "bin").mkdir(parents=True)
    (home / "public_html" / "cgi-bin").mkdir(parents=True)
    (home / "gahk_web" / "config.ini").write_text(
        config_ini(database, key, (dist / "manifest.json").resolve().as_uri()), encoding="utf-8"
    )
    # The one-time bootstrap upload (DEPLOY.md): three files from the release.
    shutil.copyfile(dist / "updater.py", home / "gahk_web" / "bin" / "updater.py")
    shutil.copyfile(dist / "gahk.cgi", home / "public_html" / "cgi-bin" / "gahk.cgi")
    (home / "public_html" / "cgi-bin" / "gahk.cgi").chmod(0o755)
    shutil.copyfile(dist / "htaccess", home / "public_html" / ".htaccess")

    code, _ = cgi(python, home, "GET", "/")
    if code != 503:
        raise SmokeError(f"before the install / answered {code}, expected 503")
    result = expect_json(cgi(python, home, "POST", "/api/update", b"{}", key), 200, "first update")
    if not result.get("updated") or result.get("version") != version:
        raise SmokeError(f"first update did not install {version}: {result}")
    print(f"  bootstrap: {result['detail']}")

    health = expect_json(cgi(python, home, "GET", "/api/health"), 200, "health")
    if health != {"ok": True, "version": version, "revision": None}:
        raise SmokeError(f"unexpected health: {health}")
    status = expect_json(cgi(python, home, "GET", "/api/status", key=key), 200, "status")
    if status["update"]["last_result"] != "updated" or status["version"] != version:
        raise SmokeError(f"unexpected status: {status}")

    body = json.dumps(SNAPSHOT).encode()
    published = expect_json(cgi(python, home, "POST", "/api/publish", body, key), 200, "publish")
    page = (home / "public_html" / "faciliteter" / "index.html").read_text(encoding="utf-8")
    if "Røgtest" not in page:
        raise SmokeError("the published page was not rendered")
    css = [line for line in page.splitlines() if 'rel="stylesheet"' in line]
    if (
        not css
        or "/static/css/site." not in css[0]
        or not any((home / "public_html" / "static" / "css").glob("site.*.css"))
    ):
        raise SmokeError(f"the page does not link the hashed stylesheet: {css}")
    print(f"  publish: {published['pages_written']} files written")
    code, content = cgi(python, home, "GET", "/faciliteter/")
    if code != 200 or "Røgtest".encode() not in content:
        raise SmokeError(f"the stub's fallback did not serve the page: {code}")

    again = expect_json(cgi(python, home, "POST", "/api/update", b"{}", key), 200, "second update")
    if again.get("updated") or not str(again.get("detail", "")).startswith("already running"):
        raise SmokeError(f"second update was not a no-op: {again}")
    print(f"  second update: {again['detail']}")


def reset_database(python: str, database: str, release: Path) -> None:
    """Drop every table, so the bootstrap starts from an empty database (as on a new webhost)."""
    url = urlsplit(database)
    code = (
        "import sys;sys.path[:0]=[sys.argv[1]+'/src',sys.argv[1]+'/vendor'];import pg8000.native as p;"
        "c=p.Connection(user=sys.argv[2],password=sys.argv[3] or None,host=sys.argv[4],"
        "port=int(sys.argv[5]),database=sys.argv[6]);"
        "c.run('DROP SCHEMA public CASCADE');c.run('CREATE SCHEMA public');c.close()"
    )
    check(
        python,
        "-I",
        "-c",
        code,
        str(release),
        url.username or "",
        url.password or "",
        url.hostname or "localhost",
        str(url.port or 5432),
        url.path.lstrip("/"),
        cwd=release,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--python", action="append", default=[], help="repeat for each interpreter")
    parser.add_argument("--database", default="", help="a THROWAWAY database for the install test")
    args = parser.parse_args(argv)
    dist: Path = args.dist
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    bundle = dist / manifest["bundle"]
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest["sha256"]:
        raise SmokeError("the bundle does not match manifest.json")
    updater = load_updater(dist / "updater.py")
    for version in args.python or ["3.11"]:
        python = find_python(version)
        print(f"{python}:")
        with tempfile.TemporaryDirectory(prefix="gahk_web-smoke-") as tmp:
            scratch = Path(tmp)
            selftests(python, bundle, updater, scratch)
            if args.database:
                reset_database(python, args.database, scratch / "unpacked")
                install(python, dist, manifest["version"], args.database, scratch)
    print("smoke test ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SmokeError as exc:
        print(f"smoke_bundle: {exc}", file=sys.stderr)
        sys.exit(1)
