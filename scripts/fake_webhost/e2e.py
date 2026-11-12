#!/usr/bin/env python3
"""End-to-end test of a built release on a fake Hetzner webhost: uv run python scripts/fake_webhost/e2e.py

    --dist DIR          the built release (default dist/, from scripts/build_bundle.py)
    --no-build          use the image gahkweb-fake-webhost as it is (CI builds it with its layer cache)
    --platform P        e.g. linux/amd64, the webhost's machine (default: docker's native one)
    --wheelhouse DIR    pip takes the wheels from DIR instead of PyPI (offline runs; hashes still checked)
    --keep              leave the containers running afterwards, for a look inside
    --no-suexec         the other guess about Hetzner: Apache itself runs as the account, no suEXEC

It needs docker. It starts a PostgreSQL 17 container and the fake webhost (scripts/fake_webhost/
Dockerfile: Debian's Apache 2.4 with mod_rewrite, mod_fcgid, mod_cgid and suEXEC, the system
python3 3.13 with Debian's pip) on a network of their own, and plays every party:

  - the operator: uploads the four bootstrap files and config.ini into the account, as by SFTP (as
    the account's user, nothing else), flips the CGI-only switch and pins a version in config.ini;
  - GitHub: serves the release's manifest.json and bundle from a stand-in inside the container
    (releases.test, which answers a bundle download with a redirect, as GitHub does), and further
    bundles derived from it: a newer version, one whose self-test fails, one whose .htaccess Apache
    rejects, one whose gahk.fcgi cannot start;
  - intern.gahk.dk: signed API requests (the project's own signing helper), and a calendar feed
    stand-in (intern.test) for the /kalender/ pass-through;
  - visitors: pages, redirects, forms, the visit beacon, addresses that must be refused.

The updater installs the packages with pip from PyPI (or --wheelhouse), from the real
requirements.lock. Each request's handler is read from Apache's access log (fcgid-script,
cgi-script, or none for a file Apache served itself), FastCGI processes from `ps` in the container.
Nothing about the processes is added to the site's responses.

The containers and the network are named gahkweb-e2e-<run>-*, and are removed at the end.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import random
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote, unquote, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gahk_web import signing  # noqa: E402 - after the path

IMAGE = "gahkweb-fake-webhost"
LOGIN = "ext0e2e"  # the Dockerfile's ARG LOGIN
HOME = f"/usr/home/{LOGIN}"
PUBLIC_HTML = f"{HOME}/public_html"
APP = f"{HOME}/gahk_web"
MANIFEST_URL = "http://releases.test:8080/download/manifest.json"
INTERN_URL = "http://intern.test:8080"
CURRENT_STATE_WAIT = 900  # seconds an install may take after its request has answered
SWITCH_LINE = "# RewriteRule ^ - [E=GAHK_CGI_ONLY:1]"
HOSTS = ("gahk.dk", "www.gahk.dk", "releases.test", "intern.test")
# Error-log lines that mean something is wrong with the webhost's set-up, whatever the step.
BAD_LOG = re.compile(
    r"Traceback|Premature end of script headers|exceeds MaxRequestLen|can't apply process slot|"
    r"read data timeout|Invalid command|error reading data|suexec policy violation"
)

# What the webhost's own python said on 2026-11-12 (DEPLOY.md section 13), asked the same way: a CGI
# script run as the account. The image must keep matching it.
WEBHOST_GLIBC = "glibc 2.41"
PIP_RE = re.compile(r"^pip 25\.1\.\d+ from /usr/lib/python3/dist-packages/pip \(python 3\.13\)$")
HOSTCHECK = b"""#!/usr/bin/python3
import json, os, platform, subprocess

pip = []
for flags in (["-I"], []):
    done = subprocess.run(["/usr/bin/python3", *flags, "-m", "pip", "--version"], capture_output=True, text=True)
    pip.append([done.returncode, done.stdout.strip()])
facts = {"python": platform.python_version(), "glibc": os.confstr("CS_GNU_LIBC_VERSION"), "pip": pip}
print("Content-Type: application/json\\n\\n" + json.dumps(facts))
"""


class E2EError(Exception):
    pass


def expect(condition: object, message: str) -> None:
    if not condition:
        raise E2EError(message)


def run(*args: str, data: bytes | None = None, check: bool = True, timeout: float = 600) -> str:
    done = subprocess.run(  # noqa: S603 - docker with arguments of ours
        args, input=data, capture_output=True, timeout=timeout, check=False
    )
    if check and done.returncode != 0:
        raise E2EError(
            f"{' '.join(args[:4])} …: {(done.stderr or done.stdout).decode(errors='replace').strip()}"
        )
    return done.stdout.decode(errors="replace")


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise E2EError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- releases ---


@dataclass(frozen=True)
class Release:
    """A bundle and its manifest, as CI would put them on the production release."""

    version: str
    bundle: bytes
    what: str

    @property
    def name(self) -> str:
        return f"gahk_web-{self.version}.tar.gz"

    def manifest(self) -> bytes:
        data = {
            "version": self.version,
            "bundle": self.name,
            "sha256": hashlib.sha256(self.bundle).hexdigest(),
            "size": len(self.bundle),
            "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "python": ">=3.13",
        }
        return (json.dumps(data, indent=2) + "\n").encode()


def derive(base: Release, what: str, edit: Callable[[Path], None]) -> Release:
    """A bundle like base with another version (and an edit), packed as build_bundle.py packs one."""
    builder = load_module("e2e_build_bundle", ROOT / "scripts" / "build_bundle.py")
    version = hashlib.sha1(f"{base.version} {what}".encode(), usedforsecurity=False).hexdigest()
    with tempfile.TemporaryDirectory(prefix="gahk_web-e2e-") as tmp:
        stage = Path(tmp) / "release"
        with tarfile.open(fileobj=io.BytesIO(base.bundle), mode="r:gz") as tar:
            tar.extractall(stage, filter="data")
        (stage / "VERSION").write_text(version + "\n", encoding="ascii")
        edit(stage)
        target = Path(tmp) / "bundle.tar.gz"
        builder.write_tarball(stage, target, int(time.time()))
        return Release(version, target.read_bytes(), what)


def no_edit(_stage: Path) -> None:
    pass


def failing_selftest(stage: Path) -> None:
    """A release that does not import: its own self-test must stop it before the switch."""
    app = stage / "src" / "gahk_web" / "app.py"
    app.write_text(
        app.read_text(encoding="utf-8") + "\nraise RuntimeError('broken on purpose')\n", encoding="utf-8"
    )


def broken_htaccess(stage: Path) -> None:
    """A .htaccess Apache rejects (500 for every request under public_html, /api/update included)."""
    htaccess = stage / "public_html" / ".htaccess"
    htaccess.write_text(htaccess.read_text(encoding="utf-8") + "\nNoSuchDirective on\n", encoding="utf-8")


def broken_fcgi(stage: Path) -> None:
    """A gahk.fcgi that cannot even be compiled: FastCGI fails, CGI would work."""
    stub = stage / "public_html" / "cgi-bin" / "gahk.fcgi"
    stub.write_text(stub.read_text(encoding="utf-8") + "\ndef (:\n", encoding="utf-8")


# --- the snapshot: tests/conftest.py's, with real pictures ---


def picture(width: int, height: int, seed: int, noisy: bool) -> bytes:
    """A JPEG Pillow can read. A noisy one is large (over the 100 000 bytes that go to CGI)."""
    from PIL import Image

    if noisy:
        image = Image.frombytes("RGB", (width, height), random.Random(seed).randbytes(width * height * 3))  # noqa: S311 - noise, not a secret
    else:
        image = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


# The kitchen picture's name keeps its Danish letters (NFC), as intern sends it, and an old address
# with one maps to it.
DANISH_NAME = "køkken-på-første_sal.jpg"
DANISH_HREF = "k%C3%B8kken-p%C3%A5-f%C3%B8rste_sal.jpg"
DANISH_LEGACY = "/static/legacy/image/upload/images/køkken.jpg"


def snapshot_with_pictures() -> tuple[dict[str, Any], dict[str, bytes]]:
    """conftest.snapshot(), its three made-up JPEGs swapped for real ones (one of them large), the
    kitchen picture named with Danish letters."""
    conftest = load_module("e2e_conftest", ROOT / "tests" / "conftest.py")
    data: dict[str, Any] = conftest.rename_image(conftest.snapshot(), "kokken.jpg", DANISH_NAME)
    kokken = next(image["url"] for image in data["images"] if image["url"].endswith(DANISH_NAME))
    data["legacy_images"][DANISH_LEGACY] = kokken
    old = [conftest.GANG, conftest.HERO, conftest.KOKKEN]
    new = [picture(1200, 800, 1, False), picture(2400, 1600, 2, True), picture(1000, 700, 3, False)]
    text = json.dumps(data)
    for before, after in zip(old, new, strict=True):
        old_sha, new_sha = hashlib.sha256(before).hexdigest(), hashlib.sha256(after).hexdigest()
        text = text.replace(old_sha, new_sha).replace(old_sha[:16], new_sha[:16])
        text = text.replace(f'"size": {len(before)}', f'"size": {len(after)}')
    data = json.loads(text)
    data["revision"] = hashlib.sha256(text.encode()).hexdigest()
    return data, {hashlib.sha256(blob).hexdigest(): blob for blob in new}


# --- HTTP ---


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:  # noqa: ANN401 - parsed JSON
        return json.loads(self.body)

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@dataclass
class AccessEntry:
    host: str
    request: str
    status: int
    handler: str
    runner: str


class Webhost:
    """The fake webhost's container and the requests the driver sends it."""

    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self.key = key
        self.https_port = int(run("docker", "port", name, "443/tcp").split(":")[-1].split()[0])
        self.http_port = int(run("docker", "port", name, "80/tcp").split(":")[-1].split()[0])
        self.context = ssl.create_default_context(
            cadata=self.read("/etc/ssl/fake-hetzner/gahk.dk.crt").decode()
        )
        self.context.check_hostname = False  # the certificate names gahk.dk; the driver dials 127.0.0.1
        self._access_seen = 0
        self._errors_seen = 0

    # -- the container --

    def exec(self, *command: str, user: str = "root", data: bytes | None = None, check: bool = True) -> str:
        return run("docker", "exec", "-i", "-u", user, self.name, *command, data=data, check=check)

    def read(self, path: str, user: str = "root", *, check: bool = True) -> bytes:
        done = subprocess.run(  # noqa: S603
            ["docker", "exec", "-u", user, self.name, "cat", path],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if done.returncode != 0 and check:
            raise E2EError(f"cannot read {path}: {done.stderr.decode(errors='replace').strip()}")
        return done.stdout

    def upload(self, path: str, data: bytes, mode: str) -> None:
        """What an SFTP upload by the account's user does: write the file, set its mode."""
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1" && chmod "$2" "$1"'
        self.exec("sh", "-c", script, "sh", path, mode, user=LOGIN, data=data)

    def serve_release(self, release: Release, *, manifest: bool = True) -> None:
        """Put the bundle where releases.test serves it, and make it the production manifest."""
        self.exec("sh", "-c", f"cat > /srv/releases/storage/{release.name}", data=release.bundle)
        name = f"manifest-{release.version}.json"
        self.exec("sh", "-c", f"cat > /srv/releases/download/{name}", data=release.manifest())
        if manifest:
            self.exec("sh", "-c", "cat > /srv/releases/download/manifest.json", data=release.manifest())

    def reload(self, *settings: str) -> None:
        self.exec("fake-webhost", "reload", *settings)
        time.sleep(1)

    def fcgi_pids(self) -> set[int]:
        out = self.exec("ps", "-eo", "pid=,user=,args=")
        return {
            int(line.split()[0])
            for line in out.splitlines()
            if "gahk.fcgi" in line and line.split()[1] == LOGIN
        }

    def current(self) -> str:
        return self.read(f"{APP}/CURRENT", user=LOGIN).decode().strip()

    def log_tail(self, path: str, lines: int = 40) -> str:
        return self.exec("tail", "-n", str(lines), path, check=False)

    def new_errors(self) -> list[str]:
        """Apache's error-log lines since the last call."""
        lines = self.read("/var/log/apache2/error.log").decode(errors="replace").splitlines()
        new, self._errors_seen = lines[self._errors_seen :], len(lines)
        return new

    def access(self) -> list[AccessEntry]:
        """Access-log entries for gahk.dk since the last call."""
        lines = self.read("/var/log/apache2/access.log").decode(errors="replace").splitlines()
        new, self._access_seen = lines[self._access_seen :], len(lines)
        entries = []
        for line in new:
            match = re.match(r'^(\S+) \S+ "(.*)" (\d+) (\S+) "(.*)" \d+$', line)
            if match:
                host, request, status, handler, runner = match.groups()
                entries.append(AccessEntry(host, request, int(status), handler, runner))
        return entries

    # -- requests --

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        *,
        host: str = "gahk.dk",
        https: bool = True,
    ) -> Response:
        connection: http.client.HTTPConnection
        if https:
            connection = http.client.HTTPSConnection(
                "127.0.0.1", self.https_port, context=self.context, timeout=120
            )
        else:
            connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=120)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            all_headers = {"Host": host, "User-Agent": "Mozilla/5.0 (gahk_web e2e)", **(headers or {})}
            if body or method in ("POST", "PUT"):
                all_headers["Content-Length"] = str(len(body))
            for name, value in all_headers.items():
                connection.putheader(name, value)
            connection.endheaders(body or None)
            answer = connection.getresponse()
            data = answer.read()
            return Response(answer.status, {k.lower(): v for k, v in answer.getheaders()}, data)
        finally:
            connection.close()

    def get(self, path: str, **kwargs: Any) -> Response:  # noqa: ANN401
        return self.request("GET", path, **kwargs)

    def api(
        self,
        method: str,
        path: str,
        data: object = None,
        *,
        raw: bytes | None = None,
        content_type: str = "application/json",
        query: str = "",
    ) -> Response:
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else b"")
        headers = {**signing.signed_headers(self.key, method, path, body), "Content-Type": content_type}
        return self.request(method, path + (f"?{query}" if query else ""), body, headers)

    def wait_health(self, version: str, runner: str, within: float = 60) -> Response:
        """GET /api/health until the version answers through the runner (old processes give way)."""
        deadline = time.monotonic() + within
        while True:
            health = self.get("/api/health")
            if (
                health.status == 200
                and health.json().get("version") == version
                and health.header("X-Gahk-Runner") == runner
            ):
                return health
            if time.monotonic() > deadline:
                raise E2EError(
                    f"health never came from {version} through {runner}: {health.status} "
                    f"{health.header('X-Gahk-Runner')} {health.text[:300]}"
                )
            time.sleep(1)

    def update(self, data: object = None, query: str = "") -> dict[str, Any]:
        """A signed POST /api/update. When the request ends before the install does (the updater
        carries on, detached), wait for its outcome in GET /api/status."""
        started = datetime.now(UTC).replace(microsecond=0)
        answer = self.api("POST", "/api/update", {} if data is None else data, query=query)
        if answer.status == 200 and "still running" not in answer.text:
            result: dict[str, Any] = answer.json()
            return result
        deadline = time.monotonic() + CURRENT_STATE_WAIT
        while time.monotonic() < deadline:
            time.sleep(2)
            status = self.api("GET", "/api/status")
            if status.status not in (200, 503):
                continue
            state = status.json().get("update") or {}
            checked = state.get("checked_at")
            if checked and datetime.fromisoformat(checked) >= started and not self.updating():
                # A process that stood in for the release before the install may answer once more.
                for _ in range(5):
                    if status.status == 200:
                        break
                    time.sleep(1)
                    status = self.api("GET", "/api/status")
                return {
                    "updated": state.get("last_result") == "updated",
                    "version": status.json().get("version"),
                    "detail": f"{state.get('last_result')}: {state.get('last_error') or ''}",
                    "request": f"HTTP {answer.status}",
                }
        raise E2EError(f"the update did not end within {CURRENT_STATE_WAIT} s")

    def updating(self) -> bool:
        return "updater.py run" in self.exec("ps", "-eo", "args=")


# --- the run ---


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tag = f"gahkweb-e2e-{secrets.token_hex(3)}"
        self.network = self.tag
        self.db = f"{self.tag}-db"
        self.web = f"{self.tag}-web"
        self.key = secrets.token_urlsafe(48)
        self.db_password = secrets.token_urlsafe(24)
        self.timings: list[tuple[str, float]] = []
        self.started = time.monotonic()

    @contextlib.contextmanager
    def step(self, what: str, host: Webhost | None = None, *, failures: bool = False) -> Iterator[None]:
        """A step of the scenario, timed. Unless it provokes failures on purpose, Apache's error log
        must stay free of signs of a broken set-up while it runs."""
        started = time.monotonic()
        print(f"- {what}", flush=True)
        yield
        self.timings.append((what, time.monotonic() - started))
        if host is not None:
            bad = [line for line in host.new_errors() if BAD_LOG.search(line)]
            expect(failures or not bad, f"{what}: Apache's error log:\n" + "\n".join(bad[-20:]))

    # -- containers --

    def platform(self) -> list[str]:
        return ["--platform", self.args.platform] if self.args.platform else []

    def start(self) -> Webhost:
        if not self.args.no_build:
            run("docker", "build", *self.platform(), "-q", "-t", IMAGE, str(HERE), timeout=1800)
        run("docker", "network", "create", self.network)
        run(
            "docker", "run", "-d", "--name", self.db, "--network", self.network, "--network-alias", "db",
            *self.platform(), "-e", "POSTGRES_USER=gahk", "-e", f"POSTGRES_PASSWORD={self.db_password}",
            "-e", "POSTGRES_DB=gahk_web", "postgres:17",
        )  # fmt: skip
        hosts = [f"--add-host={host}:127.0.0.1" for host in HOSTS]
        mounts = ["-v", f"{self.args.wheelhouse.resolve()}:/wheelhouse:ro"] if self.args.wheelhouse else []
        mounts += [] if self.args.suexec else ["-e", "FAKE_SUEXEC=0"]
        run(
            "docker", "run", "-d", "--name", self.web, "--network", self.network, *self.platform(),
            "-p", "127.0.0.1::443", "-p", "127.0.0.1::80", *hosts, *mounts, IMAGE,
        )  # fmt: skip
        deadline = time.monotonic() + 120
        while run("docker", "exec", self.db, "pg_isready", "-U", "gahk", check=False).find("accepting") < 0:
            expect(time.monotonic() < deadline, "PostgreSQL did not start")
            time.sleep(1)
        while "apache2" not in run("docker", "exec", self.web, "ps", "-eo", "comm=", check=False):
            expect(
                time.monotonic() < deadline,
                f"Apache did not start: {run('docker', 'logs', self.web, check=False)}",
            )
            time.sleep(1)
        return Webhost(self.web, self.key)

    def stop(self) -> None:
        if self.args.keep:
            print(f"kept: docker rm -f {self.web} {self.db}; docker network rm {self.network}")
            return
        run("docker", "rm", "-f", self.web, self.db, check=False)
        run("docker", "network", "rm", self.network, check=False)

    def pip_source(self) -> str:
        return "the wheelhouse" if self.args.wheelhouse else "PyPI"

    def config_ini(self, pin: str = "") -> bytes:
        text = f"""; written by scripts/fake_webhost/e2e.py, as the operator would (DEPLOY.md section 3)
[database]
host = db
port = 5432
name = gahk_web
user = gahk
password = {self.db_password}
sslmode = disable

[sync]
key = {self.key}

[stats]
secret = {secrets.token_urlsafe(48)}

[site]
base_url = https://gahk.dk
intern_url = {INTERN_URL}

[update]
manifest_url = {MANIFEST_URL}
"""
        return (text + (f"pin = {pin}\n" if pin else "")).encode()

    # -- the test --

    def main(self) -> None:
        dist: Path = self.args.dist
        manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
        first = Release(manifest["version"], (dist / manifest["bundle"]).read_bytes(), "the built release")
        expect(
            hashlib.sha256(first.bundle).hexdigest() == manifest["sha256"],
            "dist/ does not match its manifest",
        )
        second = derive(first, "a newer release", no_edit)
        broken = [
            derive(first, "a release whose self-test fails", failing_selftest),
            derive(first, "a release whose .htaccess Apache rejects", broken_htaccess),
            derive(first, "a release whose gahk.fcgi cannot start", broken_fcgi),
        ]
        snapshot, pictures = snapshot_with_pictures()
        try:
            with self.step("start PostgreSQL 17 and the fake webhost"):
                host = self.start()
            try:
                self.scenario(host, dist, first, second, broken, snapshot, pictures)
            except BaseException:
                print(
                    "\n--- error.log ---\n" + host.log_tail("/var/log/apache2/error.log", 60), file=sys.stderr
                )
                print("--- update.log ---\n" + host.log_tail(f"{APP}/var/update.log", 60), file=sys.stderr)
                raise
        finally:
            self.stop()
        total = time.monotonic() - self.started
        print("\nwebhost e2e ok")
        for what, seconds in self.timings:
            print(f"  {seconds:6.1f} s  {what}")
        print(f"  {total:6.1f} s  in all")

    def scenario(
        self,
        host: Webhost,
        dist: Path,
        first: Release,
        second: Release,
        broken: list[Release],
        snapshot: dict[str, Any],
        pictures: dict[str, bytes],
    ) -> None:
        steps: list[tuple[str, Callable[[], None]]] = [
            ("bootstrap: the operator's upload (four files and config.ini), as by SFTP", lambda: self.bootstrap(host, dist)),
            (f"first install: POST /api/update outlives a 2 s CGI timeout, pip from {self.pip_source()}", lambda: self.first_install(host, first)),
            ("publish: images (small through FastCGI, large through CGI), variants, snapshot", lambda: self.publish(host, snapshot, pictures)),
            ("static files: served by Apache, cache headers, both slash forms", lambda: self.static(host, snapshot)),
            ("an image name with Danish letters: the file, its percent-encoded URL, old addresses", lambda: self.danish_name(host, snapshot)),
            ("the app: redirects, 404, a POST to a page", lambda: self.dynamic(host)),
            ("forms, the visit beacon, the outbox and its ack", lambda: self.outbox(host)),
            ("preview and the calendar pass-through", lambda: self.preview_and_calendar(host, snapshot)),
            ("refused: the stubs, any script, dot-files", lambda: self.refused(host)),
            ("second release: CURRENT switches, the FastCGI process is replaced", lambda: self.second_release(host, second)),
            ("the CGI-only switch", lambda: self.cgi_only(host, second)),
        ]  # fmt: skip
        for what, action in steps:
            with self.step(what, host):
                action()
        for release in broken:
            with self.step(f"{release.what}: the running release stays", host, failures=True):
                self.broken_release(host, release, second)
        with self.step("rollback: a pin to the first release (kept, no download)", host):
            self.rollback(host, first)
        suexec = [
            line
            for line in host.read("/var/log/apache2/suexec.log", check=False).decode().splitlines()
            if "cmd:" not in line
        ]
        expect(not suexec, "suEXEC refused something:\n" + "\n".join(suexec[-20:]))

    def bootstrap(self, host: Webhost, dist: Path) -> None:
        # What the account held before: the November 2026 experiments (DEPLOY.md section 4 step 3).
        host.upload(
            f"{PUBLIC_HTML}/.htaccess", b"DirectoryIndex index.py\nSetEnv GAHK_WEB_HOME /elsewhere\n", "644"
        )
        experiment = (
            b"#!/usr/bin/python3\nimport os\n"
            b"print('Content-Type: text/plain\\n\\nexperiment', os.environ.get('GAHK_WEB_HOME', '-'))\n"
        )
        host.upload(f"{PUBLIC_HTML}/install.py", experiment, "755")
        host.upload(f"{PUBLIC_HTML}/fcgitest/test.fcgi", experiment, "755")
        host.upload(f"{PUBLIC_HTML}/hostcheck.cgi", HOSTCHECK, "755")
        facts = host.get("/hostcheck.cgi").json()
        print(f"    host: {facts}")
        expect(
            facts["python"].startswith("3.13.")
            and facts["glibc"] == WEBHOST_GLIBC
            and all(code == 0 and PIP_RE.match(out) for code, out in facts["pip"]),
            f"the fake webhost no longer looks like Hetzner's (DEPLOY.md section 13): {facts}",
        )
        ran = host.get("/install.py").text.split()
        expect(ran[:1] == ["experiment"], "the server does not run *.py as CGI")
        # suEXEC hands a script only its own list of variables: SetEnv in .htaccess does not reach it.
        expected = "-" if self.args.suexec else "/elsewhere"
        expect(ran[1:] == [expected], f"SetEnv reached the script as {ran[1:]}, expected {expected}")
        # The bootstrap upload (DEPLOY.md section 4).
        host.upload(f"{APP}/config.ini", self.config_ini(), "600")
        host.upload(f"{APP}/bin/updater.py", (dist / "updater.py").read_bytes(), "644")
        host.upload(f"{PUBLIC_HTML}/cgi-bin/gahk.cgi", (dist / "gahk.cgi").read_bytes(), "755")
        host.upload(f"{PUBLIC_HTML}/cgi-bin/gahk.fcgi", (dist / "gahk.fcgi").read_bytes(), "755")
        host.upload(f"{PUBLIC_HTML}/.htaccess", (dist / "htaccess").read_bytes(), "644")
        for path in ("/install.py", "/fcgitest/test.fcgi", "/hostcheck.cgi"):
            expect(host.get(path).status == 403, f"{path} still runs after the bootstrap")
        front = host.get("/")
        expect(front.status == 503 and "installeret" in front.text, f"before the install /: {front.status}")
        health = host.get("/api/health")
        expect(
            health.status == 503 and "installeret" in health.text,
            f"before the install health: {health.status}",
        )
        expect(host.api("GET", "/api/status").status == 503, "before the install a signed status is 503")
        stand_in = {entry.handler for entry in host.access() if entry.request.startswith("GET /api/")}
        expect(stand_in == {"fcgid-script"}, f"before the install, gahk.fcgi must answer: {stand_in}")
        unsigned = host.request("POST", "/api/update", b"{}")
        expect(unsigned.status == 401, f"an unsigned update: {unsigned.status}")
        handlers = {entry.handler for entry in host.access() if '"POST /api/update' in f'"{entry.request}'}
        expect(handlers == {"cgi-script"}, f"/api/update did not go through CGI: {handlers}")

    def first_install(self, host: Webhost, first: Release) -> None:
        host.serve_release(first)
        # Hetzner's CGI time limit is not known. With a short one Apache gives up on the request
        # long before pip is done; the updater runs detached and must finish all the same.
        host.reload("APACHE_TIMEOUT=2")
        expect(host.get("/").status == 503, "before the install / must answer 503")  # gahk.fcgi stands in
        started = time.monotonic()
        result = host.update()
        installed = time.monotonic()
        expect(result.get("version") == first.version and result.get("updated"), f"first install: {result}")
        print(
            f"    installed {first.version[:12]} in {installed - started:.0f} s ({result.get('request', 'HTTP 200')})"
        )
        expect(host.current() == first.version, "CURRENT does not name the first release")
        # The FastCGI process that stood in while nothing was installed gives way at once.
        health = host.wait_health(first.version, "fastcgi")
        waited = time.monotonic() - installed
        expect(waited < 5, f"the release answered only {waited:.0f} s after the install")
        expect(health.json() == {"ok": True, "version": first.version, "revision": None}, health.text)
        host.reload()
        status = host.api("GET", "/api/status")
        expect(status.status == 200 and status.json()["update"]["last_result"] == "updated", status.text)
        release_log = host.read("/var/log/apache2/releases.log").decode()
        expect(
            f"GET /download/{first.name}" in release_log and " 302 " in release_log, "no redirect followed"
        )
        offline = "Looking in links: /wheelhouse" in host.read(f"{APP}/var/update.log", user=LOGIN).decode()
        expect(offline == bool(self.args.wheelhouse), f"pip did not install from {self.pip_source()}")
        vendor = host.exec("ls", f"{APP}/releases/{first.version}/vendor", user=LOGIN)
        packages = set(vendor.split())
        expect({"flask", "psycopg", "PIL", "sqlalchemy", "alembic"} <= packages, f"vendor/: {vendor}")
        # The updater made the schema with the release's Alembic migrations.
        tables = run(
            "docker", "exec", self.db, "psql", "-U", "gahk", "-d", "gahk_web", "-Atc",
            "SELECT version_num FROM alembic_version; SELECT count(*) FROM site_state",
        ).split()  # fmt: skip
        expect(len(tables) == 2 and tables[0].isdigit() and tables[1] == "1", f"database: {tables}")
        mode = host.exec("stat", "-c", "%a %U", f"{PUBLIC_HTML}/cgi-bin/gahk.fcgi").strip()
        expect(mode == f"755 {LOGIN}", f"gahk.fcgi installed as {mode}")

    def publish(self, host: Webhost, snapshot: dict[str, Any], pictures: dict[str, bytes]) -> None:
        missing = host.api("POST", "/api/images/missing", {"sha256": list(pictures)})
        expect(missing.status == 200 and set(missing.json()["missing"]) == set(pictures), missing.text)
        early = host.api("POST", "/api/publish", snapshot)
        expect(early.status == 409, f"publish before the images: {early.status}")
        host.access()
        for sha, data in pictures.items():
            put = host.api("PUT", f"/api/images/{sha}", raw=data, content_type="image/jpeg")
            expect(put.status == 204, f"PUT image ({len(data)} bytes): {put.status} {put.text}")
        handlers = [(entry.request.split()[1][:20], entry.handler) for entry in host.access()]
        sizes = sorted(len(data) for data in pictures.values())
        expect(sizes[0] < 100_000 < sizes[-1], f"the pictures should straddle 100 000 bytes: {sizes}")
        expect(
            {handler for _, handler in handlers} == {"fcgid-script", "cgi-script"},
            f"image uploads should use both runners: {handlers}",
        )
        published = host.api("POST", "/api/publish", snapshot)
        expect(published.status == 200, f"publish: {published.status} {published.text}")
        rebuilt = host.api("POST", "/api/rebuild", {})
        expect(rebuilt.status == 200, f"rebuild: {rebuilt.status} {rebuilt.text}")
        # Both may outlast mod_fcgid's FcgidIOTimeout, so .htaccess sends them to CGI.
        long_requests = [(entry.request, entry.handler) for entry in host.access()]
        expect(
            len(long_requests) == 2 and all(handler == "cgi-script" for _, handler in long_requests),
            f"publish and rebuild should run as CGI: {long_requests}",
        )
        hero = next(image for image in snapshot["images"] if image["url"].endswith("/hero.jpg"))
        folder = hero["url"].rsplit("/", 1)[0]
        listing = host.exec("ls", f"{PUBLIC_HTML}{folder}", user=LOGIN).split()
        expect(
            {"_480w.webp", "_960w.webp", "_1600w.webp", "_2400w.webp", "hero.jpg"} <= set(listing),
            str(listing),
        )
        variant = host.get(f"{folder}/_960w.webp")
        expect(variant.status == 200 and variant.header("Content-Type") == "image/webp", "variant not served")
        expect(
            "immutable" in variant.header("Cache-Control"),
            f"variant cache: {variant.header('Cache-Control')}",
        )
        front = host.get("/")
        expect(
            front.status == 200 and f"{folder}/_480w.webp 480w" in front.text, "the front page has no srcset"
        )

    def static(self, host: Webhost, snapshot: dict[str, Any]) -> None:
        host.access()
        for path in (
            "/",
            "/faciliteter",
            "/faciliteter/",
            "/faciliteter/kokken",
            "/begivenheder/",
            "/begivenheder",
        ):
            page = host.get(path)
            expect(page.status == 200, f"{path}: {page.status}")
            expect(
                page.header("Cache-Control") == "public, max-age=60",
                f"{path}: {page.header('Cache-Control')}",
            )
            expect(page.header("Strict-Transport-Security"), f"{path}: no HSTS on https")
            expect(page.header("X-Content-Type-Options") == "nosniff", f"{path}: no nosniff")
        head = host.request("HEAD", "/faciliteter")
        expect(head.status == 200 and not head.body, f"HEAD /faciliteter: {head.status}")
        css = re.search(r"/static/css/site\.[0-9a-f]+\.css", host.get("/").text)
        if css is None:
            raise E2EError("the front page links no hashed stylesheet")
        for path, cache in (
            (css.group(0), "public, max-age=31536000, immutable"),
            (snapshot["images"][0]["url"], "public, max-age=31536000, immutable"),
            ("/favicon.ico", "public, max-age=604800"),
            ("/static/icons/icon-192x192.png", "public, max-age=604800"),
            ("/sw.js", "no-cache"),
            ("/sitemap.xml", "public, max-age=60"),
            ("/robots.txt", "public, max-age=60"),
        ):
            answer = host.get(path)
            expect(answer.status == 200, f"{path}: {answer.status}")
            expect(
                answer.header("Cache-Control") == cache,
                f"{path}: Cache-Control {answer.header('Cache-Control')}",
            )
        python = [entry.request for entry in host.access() if entry.handler != "-"]
        expect(not python, f"these static requests started Python: {python}")

    def danish_name(self, host: Webhost, snapshot: dict[str, Any]) -> None:
        url = next(image["url"] for image in snapshot["images"] if image["url"].endswith(DANISH_NAME))
        folder = url.rsplit("/", 1)[0]
        href = f"{folder}/{DANISH_HREF}"
        listing = host.exec(
            "python3", "-I", "-c", "import os, sys; print(sorted(os.listdir(os.fsencode(sys.argv[1]))))",
            f"{PUBLIC_HTML}{folder}", user=LOGIN,
        )  # fmt: skip
        expect(
            repr(DANISH_NAME.encode("utf-8")) in listing, f"the file is not named in UTF-8 (NFC): {listing}"
        )
        host.access()
        original = host.get(href)
        expect(original.status == 200, f"{href}: {original.status}")
        expect(original.header("Content-Type") == "image/jpeg", f"{href}: {original.header('Content-Type')}")
        expect(
            original.header("Cache-Control") == "public, max-age=31536000, immutable",
            f"{href}: Cache-Control {original.header('Cache-Control')}",
        )
        lower = host.get(href.lower())  # %c3%b8 is the same byte as %C3%B8
        expect(lower.status == 200 and lower.body == original.body, f"lower-case escapes: {lower.status}")
        python = [entry.request for entry in host.access() if entry.handler != "-"]
        expect(not python, f"the picture started Python: {python}")
        nfd = host.get(f"{folder}/{DANISH_HREF.replace('p%C3%A5', 'pa%CC%8A')}")
        expect(nfd.status == 404, f"the NFD spelling is another name: {nfd.status}")

        page = host.get("/faciliteter/kokken").text
        expect(
            f'<meta property="og:image" content="https://gahk.dk{href}">' in page, "og:image is not encoded"
        )
        expect(f"{folder}/_480w.webp 480w" in page, "the body's picture has no srcset")
        expect(DANISH_NAME not in page, "the page spells the image name out")

        # /public/image/… → (Apache) /static/legacy/image/… → (the app) the picture's /billeder/ URL.
        # mod_rewrite writes its escapes in lower case (%c3%b8): the same bytes.
        old = host.get("/public/image/upload/images/k%C3%B8kken.jpg")
        legacy = "/static/legacy/image/upload/images/k%C3%B8kken.jpg"
        expect(
            old.status == 301 and unquote(old.header("Location")).endswith(unquote(legacy)),
            f"/public/image with Danish letters: {old.status} {old.header('Location')}",
        )
        moved = host.get(urlsplit(old.header("Location")).path)
        expect(
            moved.status == 301 and moved.header("Location") == href,
            f"{legacy}: {moved.status} {moved.header('Location')}",
        )
        expect(host.get(moved.header("Location")).body == original.body, "the redirect leads elsewhere")

    def dynamic(self, host: Webhost) -> None:
        cases = [
            ("http", "gahk.dk", "/faciliteter", 301, "https://gahk.dk/faciliteter"),
            ("https", "www.gahk.dk", "/faciliteter?a=1", 301, "https://gahk.dk/faciliteter?a=1"),
            ("https", "gahk.dk", "/intern/x?y=1", 308, "https://intern.gahk.dk/x?y=1"),
            ("https", "gahk.dk", "/wiki/", 308, "https://intern.gahk.dk/wiki/"),
            (
                "https",
                "gahk.dk",
                "/public/image/upload/images/gang.jpg",
                301,
                "/static/legacy/image/upload/images/gang.jpg",
            ),
            ("https", "gahk.dk", "/gammel-side?utm=x", 301, "/kontakt?utm=x"),
            ("https", "gahk.dk", "/kontakt/", 200, ""),
            ("https", "gahk.dk", "/optagelse/send_rundvisning", 301, "/optagelse/ansoeg"),
        ]
        for scheme, name, path, status, location in cases:
            answer = host.request("GET", path, host=name, https=scheme == "https")
            expect(answer.status == status, f"{scheme} {name}{path}: {answer.status}, expected {status}")
            if location:
                got = answer.header("Location")
                expect(got.endswith(location), f"{name}{path}: Location {got}, expected {location}")
            if scheme == "http":
                expect(not answer.header("Strict-Transport-Security"), "HSTS sent over plain http")
        legacy = host.get("/static/legacy/image/upload/images/gang.jpg")
        expect(
            legacy.status == 301 and legacy.header("Location").startswith("/billeder/"),
            f"legacy: {legacy.status}",
        )
        missing = host.get("/findes-ikke")
        expect(missing.status == 404 and "<html" in missing.text.lower(), f"404 page: {missing.status}")
        expect(not missing.header("X-Gahk-Runner"), "only /api/health says what answered")
        challenge = f"{PUBLIC_HTML}/.well-known/acme-challenge/e2e-token"
        host.upload(challenge, b"e2e-token.key\n", "644")
        expect(
            host.get("/.well-known/acme-challenge/e2e-token").text == "e2e-token.key\n", "no ACME challenge"
        )
        plain = host.request("GET", "/.well-known/acme-challenge/e2e-token", https=False)
        expect(plain.status == 301 and plain.header("Location").startswith("https://"), "ACME over http")
        expect(
            missing.header("X-Content-Type-Options") == "nosniff", "the app's answers lack Apache's headers"
        )
        post = host.request(
            "POST", "/faciliteter", b"x=1", {"Content-Type": "application/x-www-form-urlencoded"}
        )
        expect(post.status == 405, f"POST to a page (an existing file) must reach the app: {post.status}")
        entries = host.access()
        handlers = {entry.request.split()[1]: entry.handler for entry in entries}
        expect(handlers.get("/findes-ikke") == "fcgid-script", f"404 not from FastCGI: {handlers}")
        expect(handlers.get("/intern/x?y=1") == "-", "an intern redirect started Python")

    def outbox(self, host: Webhost) -> None:
        form = host.get("/optagelse/ansoeg")
        expect(form.status == 200 and 'name="full_name"' in form.text, f"form page: {form.status}")
        fields = {
            "full_name": "Ada Lovelace",
            "email": "ada@example.org",
            "gender": "female",
            "age": "24",
            "heard_about_us": "hjemmeside",
            "motivation": "Fællesskabet",
        }
        body = "&".join(f"{name}={quote(value)}" for name, value in fields.items()).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://gahk.dk"}
        posted = host.request("POST", "/optagelse/ansoeg", body, headers)
        expect(
            posted.status == 303 and posted.header("Location").endswith("/optagelse/ansoeg/success"),
            str(posted),
        )
        expect(host.get("/optagelse/ansoeg/success").status == 200, "no success page")
        foreign = host.request(
            "POST", "/optagelse/ansoeg", body, {**headers, "Origin": "https://evil.example"}
        )
        expect(foreign.status == 403, f"a post from another origin: {foreign.status}")
        beacon = host.request("POST", "/s", b"/faciliteter", {"Content-Type": "text/plain"})
        expect(beacon.status == 204, f"beacon: {beacon.status}")
        outbox = host.api("GET", "/api/outbox")
        expect(outbox.status == 200, outbox.text)
        applications = outbox.json()["applications"]
        expect(
            len(applications) == 1 and applications[0]["fields"]["full_name"] == "Ada Lovelace",
            f"outbox applications: {applications}",
        )
        visits = {(visit["path"], visit["views"]) for visit in outbox.json()["visits"]}
        expect(("faciliteter", 1) in visits and ("*", 1) in visits, f"outbox visits: {visits}")
        ack = host.api("POST", "/api/outbox/ack", {"applications": [applications[0]["id"]]})
        expect(ack.status == 200 and ack.json()["deleted_applications"] == 1, ack.text)
        expect(host.api("GET", "/api/outbox").json()["applications"] == [], "the ack deleted nothing")

    def preview_and_calendar(self, host: Webhost, snapshot: dict[str, Any]) -> None:
        item = {**snapshot["pages"][1], "title": "Forhåndsvisning", "body": "<p>Kladde</p>"}
        preview = host.api(
            "POST", "/api/preview", {"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"}
        )
        expect(
            preview.status == 200 and "Kladde" in preview.text and "noindex" in preview.text,
            preview.text[:300],
        )
        feed = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        host.exec("sh", "-c", "cat > /srv/intern/kalender/tok_E2E-1.ics", data=feed)
        calendar = host.get("/kalender/tok_E2E-1.ics")
        expect(
            calendar.status == 200 and calendar.body == feed,
            f"calendar: {calendar.status} {calendar.text[:200]}",
        )
        expect(calendar.header("Content-Type").startswith("text/calendar"), calendar.header("Content-Type"))
        expect(calendar.header("Cache-Control") == "private, max-age=0", calendar.header("Cache-Control"))
        expect(host.get("/kalender/unknown.ics").status == 404, "an unknown feed is not passed on as 404")
        intern_log = host.read("/var/log/apache2/intern.log").decode()
        expect("GET /kalender/tok_E2E-1.ics" in intern_log, "the feed was not fetched from intern")

    def refused(self, host: Webhost) -> None:
        host.access()
        for path in (
            "/cgi-bin/gahk.fcgi",
            "/cgi-bin/gahk.cgi",
            "/cgi-bin/gahk.fcgi/api/health",
            "/cgi-bin/gahk.cgi/api/update",
            "/cgi-bin/",
            "/cgi-bin",
            "/CGI-BIN/gahk.cgi",
            "/cgi%2Dbin/gahk.cgi",
            "/cgi-bin/../cgi-bin/gahk.fcgi",
            "/install.py",
            "/INSTALL.PY",
            "/foo.py",
            "/x.cgi.jpg",
            "/fcgitest/test.fcgi",
            "/fcgitest/test.fcgi/x",
            "/static/x.py",
            "/.htaccess",
            "/.git/config",
            "/static/.tmp-site.css",
        ):
            for method in ("GET", "POST"):
                answer = host.request(method, path, b"{}" if method == "POST" else b"")
                expect(answer.status == 403, f"{method} {path}: {answer.status}, expected 403")
        ran = [entry.request for entry in host.access() if entry.status != 403]
        expect(not ran, f"a refused request was answered: {ran}")

    def second_release(self, host: Webhost, second: Release) -> None:
        for _ in range(3):
            host.wait_health(host.current(), "fastcgi")
        before = host.fcgi_pids()
        expect(before, "no FastCGI process is running")
        host.serve_release(second)
        # A request cannot point the updater anywhere else: body and query string are ignored.
        result = host.update(
            {"manifest_url": "http://evil.test/manifest.json"}, query="manifest_url=http://evil.test/"
        )
        expect(result.get("updated") and result.get("version") == second.version, f"second update: {result}")
        expect(host.current() == second.version, "CURRENT did not switch")
        host.wait_health(second.version, "fastcgi")
        for _ in range(3):
            host.wait_health(second.version, "fastcgi")
        time.sleep(1)
        after = host.fcgi_pids()
        expect(after and not before & after, f"FastCGI processes before {before}, after {after}")
        errors = host.read("/var/log/apache2/error.log").decode(errors="replace")
        expect(
            "exiting (CURRENT changed)" in errors or "exiting (CURRENT is" in errors, "no process gave way"
        )
        expect(
            "evil.test" not in host.read(f"{APP}/var/update.log", user=LOGIN).decode(), "the request steered"
        )

    def set_cgi_only(self, host: Webhost, on: bool) -> None:
        installed = host.read(f"{PUBLIC_HTML}/.htaccess", user=LOGIN).decode()
        switched = SWITCH_LINE.removeprefix("# ")
        new = installed.replace(SWITCH_LINE, switched) if on else installed.replace(switched, SWITCH_LINE)
        expect(new != installed or (switched in installed) == on, "the switch line is not in the LOCAL block")
        host.upload(f"{PUBLIC_HTML}/.htaccess", new.encode(), "644")

    def cgi_only(self, host: Webhost, second: Release) -> None:
        self.set_cgi_only(host, True)
        host.access()
        host.wait_health(second.version, "cgi", within=5)
        expect(host.get("/findes-ikke").status == 404, "404 under CGI only")
        expect(host.get("/gammel-side").status == 301, "a redirect under CGI only")
        expect(
            host.request("POST", "/s", b"/", {"Content-Type": "text/plain"}).status == 204,
            "beacon under CGI only",
        )
        expect(host.get("/faciliteter").status == 200, "a page under CGI only")
        expect(host.api("GET", "/api/status").status == 200, "status under CGI only")
        handlers = {entry.handler for entry in host.access()}
        expect(handlers == {"cgi-script", "-"}, f"CGI only, but handlers {handlers}")
        # /api/update keeps working (it always goes through CGI).
        result = host.update()
        expect(not result.get("updated") and "already running" in str(result.get("detail")), str(result))
        self.set_cgi_only(host, False)
        host.wait_health(second.version, "fastcgi", within=5)

    def broken_release(self, host: Webhost, release: Release, running: Release) -> None:
        htaccess = host.read(f"{PUBLIC_HTML}/.htaccess", user=LOGIN)
        host.serve_release(release)
        result = host.update()
        detail = str(result.get("detail"))
        expect(not result.get("updated") and detail.startswith("failed"), f"{release.what}: {result}")
        print(f"    {detail[:160]}")
        expect(host.current() == running.version, f"CURRENT is {host.current()} after {release.what}")
        host.wait_health(running.version, "fastcgi", within=40)
        expect(
            host.read(f"{PUBLIC_HTML}/.htaccess", user=LOGIN) == htaccess, "the .htaccess was not put back"
        )
        expect(host.get("/findes-ikke").status == 404 and host.get("/").status == 200, "the site is broken")
        host.serve_release(running)  # the production manifest back on the running release

    def rollback(self, host: Webhost, first: Release) -> None:
        before = host.read("/var/log/apache2/releases.log").decode().count(first.name)
        host.upload(f"{APP}/config.ini", self.config_ini(pin=first.version), "600")
        result = host.update()
        expect(result.get("updated") and result.get("version") == first.version, f"rollback: {result}")
        host.wait_health(first.version, "fastcgi")
        after = host.read("/var/log/apache2/releases.log").decode().count(first.name)
        expect(after == before, "the rollback downloaded a release that is kept")
        expect(host.get("/faciliteter").status == 200, "the site after the rollback")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--no-build", action="store_true", help=f"use the image {IMAGE} as it is")
    parser.add_argument("--platform", default="", help="docker's --platform, e.g. linux/amd64")
    parser.add_argument("--wheelhouse", type=Path, help="a folder of wheels to use instead of PyPI")
    parser.add_argument("--keep", action="store_true", help="leave the containers running")
    parser.add_argument(
        "--no-suexec", dest="suexec", action="store_false", help="Apache itself runs as the account"
    )
    args = parser.parse_args(argv)
    if shutil.which("docker") is None:
        print("e2e: docker is needed", file=sys.stderr)
        return 2
    try:
        Run(args).main()
    except E2EError as exc:
        print(f"e2e: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
