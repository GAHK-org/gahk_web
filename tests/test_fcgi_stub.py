"""cgi-bin/gahk.fcgi: the persistent FastCGI process, run as mod_fcgid runs it (the listening socket
on fd 0), with this checkout installed as the active release."""

import json
import os
import shutil
import site
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import STATS_SECRET, SYNC_KEY
from fastcgi_client import Answer, FastCGIProcess, base_env
from sqlalchemy.orm import Session

from gahk_web import fcgi_entry, signing, updater
from gahk_web.config import DatabaseConfig

ROOT = Path(__file__).resolve().parents[1]
FCGI_STUB = ROOT / "public_html" / "cgi-bin" / "gahk.fcgi"
CGI_STUB = ROOT / "public_html" / "cgi-bin" / "gahk.cgi"
SHA = "f" * 40


@pytest.fixture(scope="session")
def release_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """This checkout's package as a release (VERSION = SHA), shared by the tests."""
    template = tmp_path_factory.mktemp("release")
    shutil.copytree(
        ROOT / "src" / "gahk_web", template / "src" / "gahk_web", ignore=shutil.ignore_patterns("__pycache__")
    )
    (template / "VERSION").write_text(SHA + "\n")
    return template


@pytest.fixture
def home(tmp_path: Path, release_template: Path, db_config: DatabaseConfig, con: Session) -> Path:
    """A webhost home: config.ini, CURRENT, the release (its packages: this interpreter's), the stubs,
    and an empty test database."""
    home = tmp_path / "home"
    release = home / "gahk_web" / "releases" / SHA
    (release / "vendor").mkdir(parents=True)
    (release / "src").symlink_to(release_template / "src", target_is_directory=True)
    # Debian's and Ubuntu's pythons list a .../local/lib/pythonX.Y/dist-packages that may not exist.
    for packages in filter(os.path.isdir, site.getsitepackages()):
        for entry in Path(packages).iterdir():
            if not (release / "vendor" / entry.name).exists():
                (release / "vendor" / entry.name).symlink_to(entry)
    (release / "vendor" / ".abi").write_text(updater.interpreter_abi() + "\n")
    (home / "gahk_web" / "CURRENT").write_text(SHA + "\n")
    (home / "public_html" / "cgi-bin").mkdir(parents=True)
    for stub in (FCGI_STUB, CGI_STUB):
        shutil.copyfile(stub, home / "public_html" / "cgi-bin" / stub.name)
    (home / "public_html" / "503.html").write_text("<h1>Siden opdateres</h1>", encoding="utf-8")
    (home / "gahk_web" / "config.ini").write_text(
        f"""[database]
host = {db_config.host}
port = {db_config.port}
name = {db_config.name}
user = {db_config.user}
password = {db_config.password}
sslmode = disable

[sync]
key = {SYNC_KEY}

[stats]
secret = {STATS_SECRET}
""",
        encoding="utf-8",
    )
    return home


type Spawn = Callable[..., FastCGIProcess]


@pytest.fixture
def spawn(home: Path, tmp_path: Path) -> Iterator[Spawn]:
    started: list[FastCGIProcess] = []

    def start(**env: str) -> FastCGIProcess:
        process = FastCGIProcess(
            [sys.executable, str(home / "public_html" / "cgi-bin" / "gahk.fcgi")],
            base_env(GAHK_WEB_HOME=str(home), **env),
            tmp_path / f"fcgi-{len(started)}.log",
        )
        started.append(process)
        return process

    yield start
    for process in started:
        process.stop()


def health(process: FastCGIProcess) -> Answer:
    return process.request("GET", "/api/health")


def rewrite(path: Path) -> None:
    """Replace a file the way the updater does (a new file moved into place)."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(path.read_bytes())
    temporary.replace(path)


# --- the release runs ---


def test_one_process_answers_request_after_request(spawn: Spawn, home: Path) -> None:
    process = spawn()
    for _ in range(3):
        answer = health(process)
        assert answer.status == 200, process.output()
        assert answer.headers["X-Gahk-Runner"] == "fastcgi"
        assert json.loads(answer.body) == {"ok": True, "version": SHA, "revision": None}
    # Routed on the browser's path, not the stub's: no snapshot yet, so the 503 page.
    page = process.request("GET", "/faciliteter")
    assert page.status == 503 and "Siden opdateres" in page.text
    moved = process.request("GET", "/intern/den-hurtige/?side=2")
    assert (moved.status, moved.headers["Location"]) == (
        308,
        "https://intern.gahk.dk/intern/den-hurtige/?side=2",
    )
    signed = signing.signed_headers(SYNC_KEY, "GET", "/api/status", b"")
    status = process.request("GET", "/api/status", headers=signed)
    assert status.status == 200 and json.loads(status.body)["version"] == SHA
    assert process.alive()


def test_an_idle_process_ends_soon_after_current_changes(spawn: Spawn, home: Path) -> None:
    """mod_fcgid may never send an old process another request (the updater rewrote gahk.fcgi, a
    new inode), and one that did answer would render with the old templates: it ends by itself."""
    process = spawn()
    assert health(process).status == 200
    rewrite(home / "gahk_web" / "CURRENT")
    assert process.wait(timeout=10) == 0  # without another request
    assert "exiting (CURRENT changed)" in process.output()


def test_an_idle_process_ends_soon_after_config_ini_changes(spawn: Spawn, home: Path) -> None:
    process = spawn()
    assert health(process).status == 200
    config = home / "gahk_web" / "config.ini"
    stat = config.stat()
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))  # touch config.ini
    assert process.wait(timeout=10) == 0
    assert "exiting (config.ini changed)" in process.output()


def test_the_policy_sees_a_switch_while_idle_and_after_a_request(tmp_path: Path) -> None:
    """A request in flight when CURRENT switched is still answered; the process ends after it."""
    current = tmp_path / "CURRENT"
    current.write_text(SHA + "\n")
    policy = fcgi_entry.RestartPolicy((current,))
    assert policy.when_idle() == "" and policy.after_request(1) == ""
    rewrite(current)
    assert policy.when_idle() == "CURRENT changed"
    assert policy.after_request(2) == "CURRENT changed"


def test_the_process_ends_after_enough_requests(spawn: Spawn) -> None:
    process = spawn(GAHK_WEB_FCGI_MAX_REQUESTS="3")
    for _ in range(3):
        assert health(process).status == 200
    assert process.wait() == 0
    assert "exiting (answered 3 requests)" in process.output()


def test_the_process_ends_when_it_is_old_enough(spawn: Spawn) -> None:
    process = spawn(GAHK_WEB_FCGI_MAX_AGE="1")
    assert health(process).status == 200
    # Idle: the loop wakes every second and ends the process once it is too old.
    assert process.wait() == 0
    assert "exiting (running for" in process.output()


def test_a_process_started_on_a_stale_current_gives_way(spawn: Spawn, home: Path) -> None:
    """The stub read CURRENT just before an update switched it: the process runs the old release,
    so it ends after its first request."""
    (home / "gahk_web" / "CURRENT").write_text("e" * 40 + "\n")
    (home / "gahk_web" / "releases" / ("e" * 40)).symlink_to(home / "gahk_web" / "releases" / SHA)
    process = spawn()
    assert health(process).status == 200
    assert process.wait() == 0
    assert f"CURRENT is {'e' * 40}, this is {SHA}" in process.output()


# --- the release cannot run: no crash loop ---


def test_packages_for_another_python_answer_503_without_a_crash_loop(spawn: Spawn, home: Path) -> None:
    abi = home / "gahk_web" / "releases" / SHA / "vendor" / ".abi"
    abi.unlink()
    abi.write_text("cpython-399 .cpython-399-elsewhere.so elsewhere\n")
    process = spawn(GAHK_WEB_FCGI_STANDIN="2")
    for _ in range(3):
        answer = health(process)
        assert (answer.status, answer.headers["Content-Type"]) == (503, "application/json")
        assert answer.headers["X-Gahk-Runner"] == "gahk.fcgi"
        reason = json.loads(answer.body)
        assert reason["ok"] is False and reason["version"] == SHA
        assert "cpython-399" in reason["error"] and "POST /api/update reinstalls them" in reason["error"]
        page = process.request("GET", "/faciliteter")
        assert (page.status, page.headers["Retry-After"]) == (503, "300")
        assert page.text.startswith("<h1>Siden opdateres</h1>")
    assert process.alive()
    # Then it ends by itself, so that mod_fcgid's next process looks at the release again.
    assert process.wait(timeout=10) == 0
    assert "answering 503 for a while" in process.output()


def test_a_release_that_cannot_import_answers_503(spawn: Spawn, home: Path) -> None:
    release = home / "gahk_web" / "releases" / SHA
    (release / "src").unlink()
    (release / "src" / "gahk_web").mkdir(parents=True)
    (release / "src" / "gahk_web" / "__init__.py").write_text("raise RuntimeError('broken release')\n")
    process = spawn(GAHK_WEB_FCGI_STANDIN="5")
    answer = health(process)
    assert answer.status == 503
    error = json.loads(answer.body)["error"]
    # Only the exception's type: its text may quote a file (config.ini's lines).
    assert "the release cannot start (RuntimeError)" in error and "broken release" not in error
    # A signed status says why, with the updater's last result.
    signed = signing.signed_headers(SYNC_KEY, "GET", "/api/status", b"")
    status = process.request("GET", "/api/status", headers=signed)
    assert status.status == 503 and "cannot start" in json.loads(status.body)["error"]
    assert "broken release" in json.loads(status.body)["detail"]
    assert process.request("GET", "/api/status").status == 401
    assert "RuntimeError: broken release" in process.output()  # the traceback, in the error log


def test_a_broken_config_ini_never_reaches_a_client(spawn: Spawn, home: Path) -> None:
    """configparser quotes the lines it cannot parse; health is unsigned."""
    config = home / "gahk_web" / "config.ini"
    config.write_text(config.read_text() + "S3cretPw\n", encoding="utf-8")
    process = spawn(GAHK_WEB_FCGI_STANDIN="5")
    for path in ("/api/health", "/faciliteter", "/api/status"):
        answer = process.request("GET", path)
        assert answer.status == 503, path
        assert b"S3cretPw" not in answer.body and str(home).encode() not in answer.body, path
    assert "config.ini cannot be parsed (ParsingError)" in process.output()
    assert "S3cretPw" not in process.output()


def test_without_a_release_every_request_answers_503(spawn: Spawn, home: Path) -> None:
    (home / "gahk_web" / "CURRENT").unlink()
    process = spawn(GAHK_WEB_FCGI_STANDIN="5")
    answer = process.request("GET", "/faciliteter")
    assert answer.status == 503 and "ved at blive installeret" in answer.text
    big = process.request("POST", "/s", b"x" * 300_000)  # a body is read to its end
    assert big.status == 503
    assert process.alive()


def test_the_stand_in_gives_way_once_the_first_install_has_finished(spawn: Spawn, home: Path) -> None:
    """Found on the fake webhost: the stand-in kept answering "being installed" for up to 30 s
    after the first install, so the operator's check right after it failed."""
    current = home / "gahk_web" / "CURRENT"
    current.rename(current.with_name("CURRENT.later"))
    process = spawn(GAHK_WEB_FCGI_STANDIN="60")
    assert process.request("GET", "/faciliteter").status == 503
    time.sleep(0.5)  # it looks again right after each response
    assert process.alive()
    current.with_name("CURRENT.later").rename(current)  # the updater switched to the new release
    assert health(process).status == 503  # answered by the stand-in, which then gives way
    assert process.wait(timeout=10) == 0
    assert "exiting (the release changed)" in process.output()
    assert health(spawn()).status == 200  # mod_fcgid's next process runs the release


def test_the_stand_in_gives_way_once_the_packages_are_reinstalled(spawn: Spawn, home: Path) -> None:
    abi = home / "gahk_web" / "releases" / SHA / "vendor" / ".abi"
    fitting = abi.read_text()
    abi.unlink()
    abi.write_text("cpython-399 .cpython-399-elsewhere.so elsewhere\n")
    process = spawn(GAHK_WEB_FCGI_STANDIN="60")
    for _ in range(2):
        assert health(process).status == 503
    time.sleep(0.5)  # it looks again right after each response
    assert process.alive()  # nothing changed: it stays
    abi.write_text(fitting)  # POST /api/update reinstalled them for this python
    assert health(process).status == 503
    assert process.wait(timeout=10) == 0
    assert "exiting (the release changed)" in process.output()


def test_a_stand_in_for_a_release_that_cannot_import_waits_for_a_new_one(spawn: Spawn, home: Path) -> None:
    """Retrying the import on every request would be the crash loop the stand-in avoids."""
    release = home / "gahk_web" / "releases" / SHA
    (release / "src").unlink()
    (release / "src" / "gahk_web").mkdir(parents=True)
    (release / "src" / "gahk_web" / "__init__.py").write_text("raise RuntimeError('broken release')\n")
    process = spawn(GAHK_WEB_FCGI_STANDIN="60")
    for _ in range(3):
        assert health(process).status == 503
    time.sleep(0.5)
    assert process.alive()


def test_the_stand_in_answers_signed_update_requests_too(spawn: Spawn, home: Path) -> None:
    (home / "gahk_web" / "CURRENT").unlink()
    process = spawn(GAHK_WEB_FCGI_STANDIN="5")
    unsigned = process.request("POST", "/api/update", b"{}")
    assert (unsigned.status, json.loads(unsigned.body)) == (401, {"error": "missing signature"})
    signed = signing.signed_headers(SYNC_KEY, "POST", "/api/update", b"{}")
    answer = process.request("POST", "/api/update", b"{}", headers=signed)
    assert answer.status == 503 and "no updater installed" in json.loads(answer.body)["error"]


# --- flup: no traceback reaches a client ---

BROKEN_APP = """
import sys
sys.path.insert(0, sys.argv[1])
from gahk_web.fcgi_entry import RestartPolicy, Server

def app(environ, start_response):
    if environ["PATH_INFO"] == "/midway":
        start_response("200 OK", [("Content-Type", "text/plain")])
        return midway()
    raise RuntimeError("secret detail")

def midway():
    yield b"first part"
    raise RuntimeError("secret detail midway")

Server(app, RestartPolicy(())).run()
"""


def test_flup_never_sends_a_traceback(tmp_path: Path) -> None:
    process = FastCGIProcess(
        [sys.executable, "-c", BROKEN_APP, str(ROOT / "src")], base_env(), tmp_path / "broken.log"
    )
    try:
        answer = process.request("GET", "/boom")
        assert answer.status == 500
        assert answer.text == "Der skete en fejl. Prøv igen om lidt."
        assert answer.headers["Cache-Control"] == "no-store"
        assert b"Traceback" in answer.stderr and b"secret detail" in answer.stderr  # Apache's error log
        midway = process.request("GET", "/midway")
        assert midway.body == b"first part"  # cut short, with nothing appended
        assert b"secret detail midway" in midway.stderr
        assert process.alive()
    finally:
        process.stop()


def test_cgi_still_runs_the_whole_app(home: Path) -> None:
    """gahk.cgi (the fallback for everything) answers through the same Flask app."""
    env = base_env(
        GAHK_WEB_HOME=str(home),
        REQUEST_METHOD="GET",
        REQUEST_URI="/intern/x?y=1",
        PATH_INFO="/intern/x",
        SCRIPT_NAME="/cgi-bin/gahk.cgi",
        QUERY_STRING="y=1",
        SERVER_NAME="gahk.dk",
        SERVER_PORT="443",
        SERVER_PROTOCOL="HTTP/1.1",
        REMOTE_ADDR="192.0.2.1",
    )
    done = subprocess.run(
        [sys.executable, str(home / "public_html" / "cgi-bin" / "gahk.cgi")],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    head = done.stdout.partition(b"\r\n\r\n")[0].decode()
    assert "Status: 308" in head and "Location: https://intern.gahk.dk/intern/x?y=1" in head, done.stderr
    env["REQUEST_URI"] = env["PATH_INFO"] = "/api/health"
    done = subprocess.run(
        [sys.executable, str(home / "public_html" / "cgi-bin" / "gahk.cgi")],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert b"X-Gahk-Runner: cgi" in done.stdout


def test_no_bytecode_lands_in_cgi_bin(spawn: Spawn, home: Path) -> None:
    process = spawn()
    assert health(process).status == 200
    assert not (home / "public_html" / "cgi-bin" / "__pycache__").exists()
