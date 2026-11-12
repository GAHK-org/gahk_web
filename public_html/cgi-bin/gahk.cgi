#!/usr/bin/env python3
"""gahk.dk's CGI entry point: hands each request to the active release of gahk_web.

Installed into ~/public_html/cgi-bin/ from each release (mode 755), and uploaded once by hand for
the first install. It only finds the release and starts it; all the logic lives in the release, so
this stub rarely changes:

    ~/gahk_web/CURRENT                 the active release (a git sha)
    ~/gahk_web/releases/<sha>/src      the gahk_web package
    ~/gahk_web/releases/<sha>/vendor   its pure-Python dependencies
    ~/gahk_web/bin/updater.py          the self-updater (standard library only)

POST /api/update is handled here, before any release is imported: the stub checks the request's
signature with the [sync] key from config.ini and runs the self-updater. That is how the first
release gets installed (bootstrap), and how a broken release gets replaced.

Standard library only: it must work before any release is installed.
"""

import configparser
import hashlib
import hmac
import importlib.util
import json
import os
import pwd
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import unquote, urlsplit

UPDATE_PATH = "/api/update"
MAX_UPDATE_BODY = 1024 * 1024
MAX_SKEW_SECONDS = 300  # as gahk_web/signing.py
MIN_KEY_LENGTH = 32  # as gahk_web/config.py


def find_home() -> Path:
    """The FTP home that holds gahk_web/ and public_html/.

    [VERIFY ON HETZNER] The script may be reached through a path that is not under the FTP home
    (/usr/www/users/<login>/ vs /usr/home/<login>/), so try, in order: $GAHK_WEB_HOME (SetEnv in
    .htaccess), the account's home directory (CGI runs as the account), the folder above
    public_html.
    """
    candidates = []
    if os.environ.get("GAHK_WEB_HOME"):
        candidates.append(Path(os.environ["GAHK_WEB_HOME"]))
    try:
        candidates.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        pass
    candidates.append(Path(__file__).absolute().parent.parent.parent)
    for candidate in candidates:
        if (candidate / "gahk_web").is_dir():
            return candidate
    return candidates[-1]


def respond(status: str, content_type: str, body: str) -> None:
    headers = [f"Status: {status}", f"Content-Type: {content_type}", "Cache-Control: no-store"]
    if status.startswith("503"):
        headers.append("Retry-After: 300")
    sys.stdout.write("\r\n".join(headers) + "\r\n\r\n" + body + "\n")
    sys.stdout.flush()


def plain(status: str, message: str) -> None:
    respond(status, "text/plain; charset=utf-8", message)


def json_reply(status: str, data: dict[str, object]) -> None:
    respond(status, "application/json", json.dumps(data))


def request_path() -> str:
    """The path the client asked for (and intern signed), read as gahk_web/wsgi.py reads it."""
    request_uri = os.environ.get("REQUEST_URI", "")
    if request_uri:
        return "/" + unquote(urlsplit(request_uri).path, errors="replace").lstrip("/")
    return "/" + os.environ.get("PATH_INFO", "").lstrip("/")


def sync_key(home: Path) -> str:
    explicit = os.environ.get("GAHK_WEB_CONFIG")
    path = Path(explicit) if explicit else home / "gahk_web" / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return ""
    return parser.get("sync", "key", fallback="").strip()


def signature_problem(key: str, method: str, path: str, body: bytes) -> str:
    """The check of gahk_web/signing.py: empty when the request comes from intern."""
    if len(key) < MIN_KEY_LENGTH:
        return "signing is not configured"
    timestamp = os.environ.get("HTTP_X_GAHK_TIMESTAMP", "")
    signature = os.environ.get("HTTP_X_GAHK_SIGNATURE", "").strip().lower()
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return "missing signature"
    if abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
        return "timestamp outside the allowed window"
    message = f"{timestamp}\n{method}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    expected = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "bad signature"
    return ""


def find_updater(app_home: Path, release: Path | None) -> Path | None:
    """~/gahk_web/bin/updater.py, else the active release's copy of the same file."""
    candidates = [app_home / "bin" / "updater.py"]
    if release is not None:
        candidates.append(release / "src" / "gahk_web" / "updater.py")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def handle_update(home: Path, app_home: Path, release: Path | None) -> None:
    """POST /api/update: check the signature, then let the self-updater fetch and install."""
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    if method != "POST":
        json_reply("405 Method Not Allowed", {"error": "method not allowed"})
        return
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = -1
    if not 0 <= length <= MAX_UPDATE_BODY:
        json_reply("413 Payload Too Large", {"error": "the body is too large"})
        return
    body = sys.stdin.buffer.read(length) if length else b""
    problem = signature_problem(sync_key(home), method, UPDATE_PATH, body)
    if problem:
        json_reply("401 Unauthorized", {"error": problem})
        return
    updater = find_updater(app_home, release)
    if updater is None:
        json_reply("503 Service Unavailable", {"error": "no updater installed (~/gahk_web/bin/updater.py)"})
        return
    try:
        spec = importlib.util.spec_from_file_location("gahk_web_updater", updater)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {updater}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses look their module up there
        spec.loader.exec_module(module)
        result = module.nudge(home)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        json_reply("500 Internal Server Error", {"error": f"the updater failed: {exc!r}"})
        return
    json_reply("200 OK", result)


def main() -> None:
    home = find_home()
    os.environ["GAHK_WEB_HOME"] = str(home)
    app_home = home / "gahk_web"

    try:
        sha = (app_home / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        sha = ""
    release = app_home / "releases" / sha
    installed = bool(re.fullmatch(r"[0-9a-f]{7,40}", sha)) and (release / "src" / "gahk_web").is_dir()

    if request_path() == UPDATE_PATH:
        handle_update(home, app_home, release if installed else None)
        return
    if not installed:
        plain("503 Service Unavailable", "gahk.dk er ved at blive installeret. Prøv igen om lidt.")
        return

    sys.path[:0] = [str(release / "src"), str(release / "vendor")]
    from gahk_web.cgi_entry import main as run  # only importable once sys.path is set

    run()


if __name__ == "__main__":
    main()
