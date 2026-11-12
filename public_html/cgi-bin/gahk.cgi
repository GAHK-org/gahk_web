#!/usr/bin/python3
"""gahk.dk's CGI entry point: hands a request to the active release of gahk_web, one process each.

Installed into ~/public_html/cgi-bin/ from each release (mode 755), and uploaded once by hand for
the first install. It only finds the release and starts it; all the logic lives in the release, so
this stub rarely changes:

    ~/gahk_web/CURRENT                 the active release (a git sha)
    ~/gahk_web/releases/<sha>/src      the gahk_web package
    ~/gahk_web/releases/<sha>/vendor   its packages, installed by the updater for this python
                                       (vendor/.abi says which python)
    ~/gahk_web/bin/updater.py          the self-updater (standard library only)

Most requests reach the release through gahk.fcgi, a persistent FastCGI process; .htaccess sends
only POST /api/update, request bodies too large for FastCGI, and (with the operator's GAHK_CGI_ONLY
switch) everything here. gahk.fcgi loads this file as a module and shares all of it but main(), so
both stubs find the home, check the release and answer for a release that cannot run in the same
way. Both stay standard library only: they must work before any release is installed. Both name
/usr/bin/python3, the webhost's python (CPython 3.13.5, checked 2026-11-12), not "env python3":
the release's packages fit one python (vendor/.abi), so both stubs and the updater they start must
run the same one, whatever PATH a CGI or FastCGI process gets.

POST /api/update is handled here, before any release is imported: the stub checks the request's
signature with the [sync] key from config.ini and runs the self-updater. That is how the first
release gets installed (bootstrap), and how a broken release gets replaced.

The release runs with only its own src/ and vendor/ on sys.path; the host python's site-packages
could shadow them. When vendor/ was installed for another python (Hetzner upgraded it), the stub does
not import the release: GET /api/health answers 503 with the reason, other requests the 503 page,
and the next POST /api/update reinstalls the packages. While the release cannot run (none installed
yet, its packages do not fit, or it fails to import), a signed GET /api/status answers 503 with the
reason and the updater's last result (update.json), so how a slow install or repair ended can still
be read.
"""

import configparser
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import os
import pwd
import re
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

UPDATE_PATH = "/api/update"
HEALTH_PATH = "/api/health"
STATUS_PATH = "/api/status"
MAX_UPDATE_BODY = 1024 * 1024
MAX_SKEW_SECONDS = 300  # as gahk_web/signing.py
MIN_KEY_LENGTH = 32  # as gahk_web/config.py
NOT_INSTALLED = "no release is installed yet"
# What answers, on GET /api/health (as gahk_web/api.py's RUNNER_HEADER): the stub itself.
RUNNER_HEADER = "X-Gahk-Runner"

# (status line, headers, body): what a stub answers instead of the release.
type Answer = tuple[str, list[tuple[str, str]], bytes]
type BodyReader = Callable[[], bytes | None]


def find_home() -> Path:
    """The FTP home that holds gahk_web/ and public_html/.

    On Hetzner that is /usr/home/<login>, with public_html directly under it (checked 2026-11-12).
    In order: $GAHK_WEB_HOME (SetEnv in the .htaccess LOCAL block, should a path ever differ; it
    reaches CGI only, not FastCGI), the account's home directory (both run as the account), the
    folder above public_html.
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


@dataclass(frozen=True)
class Install:
    """The active release as the stub finds it."""

    home: Path
    sha: str  # ~/gahk_web/CURRENT when that release is installed, else ""
    release: Path | None  # its folder, when installed
    problem: str = ""  # why it cannot run here; "" when it can
    repairable: bool = False  # POST /api/update can cure the problem (packages for another python)

    @property
    def app_home(self) -> Path:
        return self.home / "gahk_web"


def locate() -> Install:
    """Find the home (and export it for the release) and the active release, and check whether it
    can run on this python."""
    home = find_home()
    os.environ["GAHK_WEB_HOME"] = str(home)
    try:
        sha = (home / "gahk_web" / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        sha = ""
    release = home / "gahk_web" / "releases" / sha
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or not (release / "src" / "gahk_web").is_dir():
        return Install(home, "", None, NOT_INSTALLED)
    problem = packages_problem(release)
    return Install(home, sha, release, problem, repairable=bool(problem))


def cannot_start(install: Install, exc: BaseException) -> Install:
    """The release failed to import or start: log why, and answer as for any release that cannot run."""
    traceback.print_exception(exc, file=sys.stderr)
    return Install(install.home, install.sha, install.release, f"the release cannot start: {exc!r}")


# --- answers ---


def answer(status: str, content_type: str, body: str | bytes, *extra: tuple[str, str]) -> Answer:
    headers = [("Content-Type", content_type), ("Cache-Control", "no-store"), *extra]
    if status.startswith("503"):
        headers.append(("Retry-After", "300"))
    data = body.encode("utf-8") if isinstance(body, str) else body
    return status, headers, data


def plain(status: str, message: str) -> Answer:
    return answer(status, "text/plain; charset=utf-8", message + "\n")


def json_answer(status: str, data: dict[str, object], *extra: tuple[str, str]) -> Answer:
    return answer(status, "application/json", json.dumps(data) + "\n", *extra)


def cgi_response(result: Answer) -> bytes:
    """An answer as CGI (and FastCGI's stdout stream) carries it: a Status header, then the rest."""
    status, headers, body = result
    lines = [f"Status: {status}", *(f"{name}: {value}" for name, value in headers)]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def request_path(environ: Mapping[str, str]) -> str:
    """The path the client asked for (and intern signed), read as gahk_web/web.py reads it."""
    request_uri = environ.get("REQUEST_URI", "")
    if request_uri:
        return "/" + unquote(urlsplit(request_uri).path, errors="replace").lstrip("/")
    return "/" + environ.get("PATH_INFO", "").lstrip("/")


def read_config(home: Path) -> configparser.ConfigParser:
    """config.ini, or an empty one when it is missing or broken."""
    explicit = os.environ.get("GAHK_WEB_CONFIG")
    path = Path(explicit) if explicit else home / "gahk_web" / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return configparser.ConfigParser(interpolation=None)
    return parser


def sync_key(home: Path) -> str:
    return read_config(home).get("sync", "key", fallback="").strip()


def signature_problem(key: str, environ: Mapping[str, str], method: str, path: str, body: bytes) -> str:
    """The check of gahk_web/signing.py: empty when the request comes from intern."""
    if len(key) < MIN_KEY_LENGTH:
        return "signing is not configured"
    timestamp = environ.get("HTTP_X_GAHK_TIMESTAMP", "")
    signature = environ.get("HTTP_X_GAHK_SIGNATURE", "").strip().lower()
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return "missing signature"
    if abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
        return "timestamp outside the allowed window"
    message = f"{timestamp}\n{method}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    expected = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "bad signature"
    return ""


def signed_body(
    install: Install, environ: Mapping[str, str], method: str, path: str, read_body: BodyReader
) -> bytes | Answer:
    """The request's body when intern signed it, else the answer that refuses it."""
    body = read_body()
    if body is None:
        return json_answer("413 Payload Too Large", {"error": "the body is too large"})
    problem = signature_problem(sync_key(install.home), environ, method, path, body)
    if problem:
        return json_answer("401 Unauthorized", {"error": problem})
    return body


def find_updater(install: Install) -> Path | None:
    """~/gahk_web/bin/updater.py, else the active release's copy of the same file."""
    candidates = [install.app_home / "bin" / "updater.py"]
    if install.release is not None:
        candidates.append(install.release / "src" / "gahk_web" / "updater.py")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def update_answer(install: Install, environ: Mapping[str, str], read_body: BodyReader) -> Answer:
    """POST /api/update: check the signature, then let the self-updater fetch and install."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method != "POST":
        return json_answer("405 Method Not Allowed", {"error": "method not allowed"})
    refused = signed_body(install, environ, method, UPDATE_PATH, read_body)
    if not isinstance(refused, bytes):
        return refused
    updater = find_updater(install)
    if updater is None:
        return json_answer(
            "503 Service Unavailable", {"error": "no updater installed (~/gahk_web/bin/updater.py)"}
        )
    try:
        spec = importlib.util.spec_from_file_location("gahk_web_updater", updater)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {updater}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses look their module up there
        spec.loader.exec_module(module)
        result = module.nudge(install.home)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return json_answer("500 Internal Server Error", {"error": f"the updater failed: {exc!r}"})
    return json_answer("200 OK", result)


def update_state(home: Path) -> dict[str, object]:
    """The updater's last result, as the release's GET /api/status reports it (gahk_web/api.py)."""
    state: dict[str, object] = {
        "checked_at": None,
        "available_version": None,
        "last_result": None,
        "last_error": None,
    }
    state_dir = read_config(home).get("paths", "state_dir", fallback="").strip()
    path = Path(state_dir or home / "gahk_web" / "state") / "update.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        state.update({key: data.get(key) for key in state})
    return state


def status_answer(install: Install, environ: Mapping[str, str], read_body: BodyReader) -> Answer:
    """GET /api/status while the release cannot answer it: 503, with the reason and how the last
    update ended (a slow install or repair finishes after its POST /api/update has answered)."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method != "GET":
        return json_answer("405 Method Not Allowed", {"error": "method not allowed"})
    refused = signed_body(install, environ, method, STATUS_PATH, read_body)
    if not isinstance(refused, bytes):
        return refused
    return json_answer(
        "503 Service Unavailable",
        {
            "error": install.problem,
            "version": install.sha or None,
            "revision": None,
            "published_at": None,
            "rendered_at": None,
            "outbox": None,
            "update": update_state(install.home),
        },
    )


def interpreter_abi() -> str:
    """The python vendor/ must be installed for; the same string as gahk_web/updater.py's."""
    tag = sys.implementation.cache_tag or sys.implementation.name
    return f"{tag} {importlib.machinery.EXTENSION_SUFFIXES[0]} {os.uname().machine}"


def packages_problem(release: Path) -> str:
    try:
        installed = (release / "vendor" / ".abi").read_text(encoding="utf-8").strip()
    except OSError:
        return "the release's packages are not installed"
    if installed != interpreter_abi():
        return f"the release's packages are for {installed}, not for this python ({interpreter_abi()})"
    return ""


def unavailable_answer(install: Install, environ: Mapping[str, str], runner: str) -> Answer:
    """The release cannot run (until POST /api/update has reinstalled its packages, when that is
    the problem): health says why, every other request gets the site's 503 page."""
    if request_path(environ) == HEALTH_PATH:
        reason = install.problem
        reason += "; POST /api/update reinstalls them" if install.repairable else "; see the error log"
        data: dict[str, object] = {"ok": False, "version": install.sha, "revision": None, "error": reason}
        return json_answer("503 Service Unavailable", data, (RUNNER_HEADER, runner))
    try:  # the site's own 503 page, which the release renders into public_html
        page = (Path(__file__).absolute().parent.parent / "503.html").read_bytes()
    except OSError:
        return plain("503 Service Unavailable", "Siden opdateres. Prøv igen om lidt.")
    return answer("503 Service Unavailable", "text/html; charset=utf-8", page)


def stub_answer(
    install: Install, environ: Mapping[str, str], read_body: BodyReader, runner: str
) -> Answer | None:
    """What the stub answers itself; None when the release should answer."""
    path = request_path(environ)
    if path == UPDATE_PATH:
        return update_answer(install, environ, read_body)
    if install.problem and path == STATUS_PATH:
        return status_answer(install, environ, read_body)
    if install.release is None:
        return plain("503 Service Unavailable", "gahk.dk er ved at blive installeret. Prøv igen om lidt.")
    if install.problem:
        print(f"{runner}: {install.problem}", file=sys.stderr)
        return unavailable_answer(install, environ, runner)
    return None


def isolate(release: Path, script: str) -> None:
    """Put only the release's src/ and vendor/ in front of the standard library on sys.path.

    Python puts the running script's folder first on sys.path, resolved (symlinks followed), while
    __file__ keeps the path Apache gave; compare both resolved, or a link on the way keeps cgi-bin/
    on the path ahead of the standard library."""
    here = os.path.realpath(Path(script).parent)
    sys.path[:] = [str(release / "src"), str(release / "vendor")] + [
        entry
        for entry in sys.path
        if os.path.realpath(entry) != here and "site-packages" not in entry and "dist-packages" not in entry
    ]


# --- CGI ---


def read_cgi_body() -> bytes | None:
    """The request body, or None when it is larger than any request this stub answers."""
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = -1
    if not 0 <= length <= MAX_UPDATE_BODY:
        return None
    return sys.stdin.buffer.read(length) if length else b""


def main() -> None:
    install = locate()
    result = stub_answer(install, os.environ, read_cgi_body, "gahk.cgi")
    if result is None and install.release is not None:
        try:
            isolate(install.release, __file__)
            from gahk_web.cgi_entry import main as run  # only importable once sys.path is set
        except Exception as exc:
            result = unavailable_answer(cannot_start(install, exc), os.environ, "gahk.cgi")
        else:
            run()
            return
    if result is not None:
        sys.stdout.buffer.write(cgi_response(result))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
