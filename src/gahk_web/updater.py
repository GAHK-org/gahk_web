#!/usr/bin/env python3
"""gahk.dk's self-updater: installs the newest CI-built release from GitHub. Standard library only.

It runs only when intern.gahk.dk asks for it, with a signed POST /api/update (intern's beat task every
15 minutes, and the "Opdatér gahk.dk nu" button in intern's Hjemmeside overview). The webhost runs no
cron job. A nudge can only make the webhost fetch what CI published on GitHub; intern cannot hand it
code of its own.

One file, three places:

    src/gahk_web/updater.py         in the repository and in every release
    <release>/bin/updater.py        the same file; each successful update copies it to
    ~/gahk_web/bin/updater.py       where the cgi-bin stub runs it from. Uploaded once by hand, it is
                                    also the bootstrap: it installs the very first release.

The cgi-bin stub checks the request's signature itself and then calls nudge() from
~/gahk_web/bin/updater.py, so an update works before any release is installed and when the active
release is broken. nudge() starts run() in a detached process and waits for it up to NUDGE_WAIT
seconds; a slower install carries on after the HTTP request has been answered.

run(), holding an exclusive lock:

     1. read the manifest (manifest_url, or manifest-<pin>.json for a pinned version)
     2. same version as ~/gahk_web/CURRENT: nothing to do, unless that release's packages were
        installed for another python (Hetzner upgraded it): then steps 5-6 for it, and swap them in
     3. download the bundle (redirects followed, size capped), check its size and sha256
     4. unpack it safely into releases/<sha> (no absolute paths, no "..", no links or devices)
     5. install the release's packages (requirements.lock, every file pinned by its sha256) for this
        python with pip, from PyPI, into a new folder; when a kept release has the identical lock
        and was installed for this python, copy its packages (hard links) instead of running pip
     6. run the release's selftest.py with this python and the new packages, then make them its
        vendor/ (vendor/.abi records the python they are for)
     7. apply the database migrations with the NEW code
     8. install public_html/static (new files only), cgi-bin/gahk.cgi and .htaccess (keeping the
        installed file's LOCAL block); if the site then answers 5xx and the previous .htaccess cures
        that, put it back and fail
     9. switch CURRENT (written atomically)          <- the release goes live here
    10. take the release's bin/updater.py if it passes its own --selftest
    11. re-render the site with the new templates
    12. delete hashed static files neither this nor the previous release uses; keep 3 releases

Any failure before step 9 leaves the old release running and untouched (migrations excepted: they
must keep working with the previous release). Steps 10-12 are repeated or done lazily by the next
update or request, so an interrupted run never leaves a half-switched site. The outcome goes to
<state_dir>/update.json, which GET /api/status reports, and every step to ~/gahk_web/var/update.log.
Temporary files live in ~/gahk_web/tmp and in .tmp-* folders, which every run clears first.

Usage (no SSH on the webhost, so these are for development, tests and CI):

    python3 -I updater.py run [--home DIR]    update now; prints the result as JSON
    python3 -I updater.py packages RELEASE    install an unpacked release's packages for this
                                              python with pip, as step 5 does (CI, smoke tests)
    python3 -I updater.py --selftest          check that this file works with this python

$GAHK_WEB_PIP_FIND_LINKS (tests, offline smoke runs) makes pip take the wheels from that folder
instead of PyPI; the hashes in requirements.lock are checked all the same.
"""

from __future__ import annotations

import configparser
import contextlib
import fcntl
import hashlib
import importlib.machinery
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import urljoin, urlsplit

DEFAULT_MANIFEST_URL = "https://github.com/GAHK-org/gahk_web/releases/download/production/manifest.json"
DEFAULT_BASE_URL = "https://gahk.dk"

KEEP_RELEASES = 3
MANIFEST_MAX_BYTES = 64 * 1024
BUNDLE_MAX_BYTES = 50 * 1024 * 1024
UNPACKED_MAX_BYTES = 300 * 1024 * 1024
MAX_MEMBERS = 20000
HTTP_TIMEOUT = 30  # seconds per network operation
SITE_CHECK_TIMEOUT = 15  # seconds for the request to the site's own /api/health
DOWNLOAD_DEADLINE = 300  # seconds for a whole download
SELFTEST_TIMEOUT = 120
MIGRATE_TIMEOUT = 300
RENDER_TIMEOUT = 300
PIP_TIMEOUT = 600  # seconds for the whole pip install
PIP_LOG_LINES = 40  # the last lines of pip's output that go to update.log
NUDGE_WAIT = 45  # how long the HTTP request waits for the update before it answers
LOG_MAX_BYTES = 1024 * 1024
MIN_PYTHON = (3, 13)

REQUIREMENTS = "requirements.lock"  # in the bundle's root: uv export of uv.lock, with sha256 hashes
ABI_FILE = ".abi"  # in vendor/: the python the packages were installed for (interpreter_abi())
PIP_MODULE = "pip"
FIND_LINKS_ENV = "GAHK_WEB_PIP_FIND_LINKS"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
PYTHON_SPEC_RE = re.compile(r"^>=\s*([0-9]+)\.([0-9]+)$")
# The block of host-specific lines in public_html/.htaccess that every install keeps from the installed
# file (e.g. SetEnv GAHK_WEB_HOME with the account's login). Everything else comes from the release.
LOCAL_BLOCK_RE = re.compile(rb"^# BEGIN LOCAL\b[^\n]*\n(.*?)^# END LOCAL\b", re.MULTILINE | re.DOTALL)
# site.1a2b3c4d5e6f.css: the build gives css and js a content hash in their names.
HASHED_STATIC_RE = re.compile(r"^.+\.[0-9a-f]{8,}\.[A-Za-z0-9]+$")

# Runs a command of the release (python -m gahk_web <command>) with only its own code and packages
# on sys.path: no site-packages of the host's python, which could shadow them.
RELEASE_RUNNER = (
    "import sys;r=sys.argv.pop(1);sys.path[:]=[r+'/src',r+'/vendor']+[p for p in sys.path "
    "if 'site-packages' not in p and 'dist-packages' not in p];"
    "from gahk_web.__main__ import main;sys.exit(main(sys.argv[1:]))"
)


class UpdateError(Exception):
    """An update step failed; the message says which and why (it ends up on intern's overview)."""


class LockError(Exception):
    """The update lock could not be taken for another reason than a running update."""


# --- where things are ---


@dataclass(frozen=True)
class Layout:
    home: Path
    config: Path
    public_html: Path
    state_dir: Path
    manifest_url: str = DEFAULT_MANIFEST_URL
    pin: str = ""
    base_url: str = DEFAULT_BASE_URL

    @property
    def app(self) -> Path:
        return self.home / "gahk_web"

    @property
    def releases(self) -> Path:
        return self.app / "releases"

    @property
    def current_file(self) -> Path:
        return self.app / "CURRENT"

    @property
    def bin_updater(self) -> Path:
        return self.app / "bin" / "updater.py"

    @property
    def var(self) -> Path:
        return self.app / "var"

    @property
    def tmp(self) -> Path:
        return self.app / "tmp"

    @property
    def log_file(self) -> Path:
        return self.var / "update.log"

    @property
    def lock_file(self) -> Path:
        return self.var / "update.lock"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "update.json"


def home_dir(home: Path | str | None = None) -> Path:
    """The FTP home that holds gahk_web/ and public_html/ (as gahk_web.config decides it)."""
    if home:
        return Path(home)
    return Path(os.environ.get("GAHK_WEB_HOME") or Path.home())


def config_path(home: Path) -> Path:
    explicit = os.environ.get("GAHK_WEB_CONFIG")
    return Path(explicit) if explicit else home / "gahk_web" / "config.ini"


def load_layout(home: Path | str | None = None) -> Layout:
    """Paths from config.ini ([paths], [update], [site]); defaults when the file or a key is missing."""
    root = home_dir(home)
    config = config_path(root)
    parser = configparser.ConfigParser(interpolation=None)
    with contextlib.suppress(configparser.Error, OSError):
        parser.read(config, encoding="utf-8")

    def get(section: str, key: str) -> str:
        return parser.get(section, key, fallback="").strip()

    return Layout(
        home=root,
        config=config,
        public_html=Path(get("paths", "public_html") or root / "public_html"),
        state_dir=Path(get("paths", "state_dir") or root / "gahk_web" / "state"),
        manifest_url=get("update", "manifest_url") or DEFAULT_MANIFEST_URL,
        pin=get("update", "pin").lower(),
        base_url=(get("site", "base_url") or DEFAULT_BASE_URL).rstrip("/"),
    )


# --- small helpers ---


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(layout: Layout, message: str) -> None:
    """Append a line to update.log, which is rotated to update.log.1 at about 1 MB."""
    try:
        layout.var.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if layout.log_file.stat().st_size > LOG_MAX_BYTES:
                layout.log_file.replace(layout.log_file.with_name("update.log.1"))
        with layout.log_file.open("a", encoding="utf-8") as handle:
            for line in message.rstrip().splitlines() or [""]:
                handle.write(f"{utc_now()} {line}\n")
    except OSError:
        pass  # a log that cannot be written must not stop the update


def write_atomic(target: Path, data: bytes, mode: int = 0o644) -> bool:
    """Write via a temporary file and os.replace; False when the file already had this content."""
    try:
        if target.read_bytes() == data:
            if (target.stat().st_mode & 0o777) != mode:
                target.chmod(mode)
            return False
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".tmp-{target.name}-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp).chmod(mode)
        Path(tmp).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
    return True


def read_current(layout: Layout) -> str:
    try:
        sha = layout.current_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return sha if SHA_RE.match(sha) else ""


def read_state(layout: Layout) -> dict[str, Any]:
    try:
        data = json.loads(layout.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def update_lock(layout: Layout) -> Iterator[bool]:
    """An exclusive, non-blocking lock. The kernel drops it if the process dies, so it never sticks.

    Yields False while another update holds it. Any other failure (no lock support on the file
    system, no permission) raises LockError, so it is reported as a failure, not as contention."""
    try:
        layout.var.mkdir(parents=True, exist_ok=True)
        handle = layout.lock_file.open("a")
    except OSError as exc:
        raise LockError(str(exc)) from exc
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # EWOULDBLOCK/EAGAIN: another update holds the lock
            yield False
            return
        except OSError as exc:
            raise LockError(str(exc)) from exc
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --- the manifest and the download ---


@dataclass(frozen=True)
class Manifest:
    version: str
    bundle: str
    sha256: str
    size: int
    python: str = ">=3.13"


def parse_manifest(data: bytes) -> Manifest:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("the manifest is not JSON") from exc
    if not isinstance(raw, dict):
        raise UpdateError("the manifest is not a JSON object")
    version, bundle, sha256, size = raw.get("version"), raw.get("bundle"), raw.get("sha256"), raw.get("size")
    python = raw.get("python", ">=3.13")
    if not isinstance(version, str) or not SHA_RE.match(version):
        raise UpdateError("the manifest's version is not a full git sha")
    if bundle != f"gahk_web-{version}.tar.gz":
        raise UpdateError("the manifest names an unexpected bundle")
    if not isinstance(sha256, str) or not HEX64_RE.match(sha256):
        raise UpdateError("the manifest's sha256 is not 64 hex digits")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= BUNDLE_MAX_BYTES:
        raise UpdateError(f"the manifest's bundle size is missing or over {BUNDLE_MAX_BYTES} bytes")
    if not isinstance(python, str) or not PYTHON_SPEC_RE.match(python):
        raise UpdateError("the manifest's python requirement is not like >=3.13")
    return Manifest(version, bundle, sha256, size, python)


def python_ok(spec: str, version: tuple[int, ...] = tuple(sys.version_info[:2])) -> bool:
    """Whether this python satisfies the manifest's requirement (">=3.13")."""
    match = PYTHON_SPEC_RE.match(spec)
    if not match:
        return False
    return version >= (int(match.group(1)), int(match.group(2)))


class _NoDowngradeRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects (GitHub sends release downloads to its storage host), but never https → http."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,  # noqa: ANN401 - http.client.HTTPMessage
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
            raise UpdateError(f"refused a redirect from https to {urlsplit(newurl).scheme}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, allowed_scheme: str) -> IO[bytes]:
    if urlsplit(url).scheme != allowed_scheme:
        raise UpdateError(f"refused to fetch {url}: not {allowed_scheme}")
    opener = urllib.request.build_opener(_NoDowngradeRedirects())
    request = urllib.request.Request(  # noqa: S310 - the scheme is checked above
        url, headers={"User-Agent": "gahk_web-updater", "Cache-Control": "no-cache"}
    )
    try:
        response: IO[bytes] = opener.open(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"{url}: {exc}") from exc
    return response


def fetch(url: str, allowed_scheme: str, limit: int, sink: IO[bytes]) -> str:
    """Copy url into sink, at most limit bytes and DOWNLOAD_DEADLINE seconds; returns the sha256."""
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    with _open(url, allowed_scheme) as response:
        while True:
            try:
                chunk = response.read(64 * 1024)
            except OSError as exc:
                raise UpdateError(f"{url}: {exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateError(f"{url} is larger than {limit} bytes")
            if time.monotonic() > deadline:
                raise UpdateError(f"{url} took longer than {DOWNLOAD_DEADLINE} s")
            digest.update(chunk)
            sink.write(chunk)
    return digest.hexdigest()


def fetch_manifest(layout: Layout, version: str = "") -> Manifest:
    """The production manifest, or with version the manifest CI keeps beside each bundle."""
    url = urljoin(layout.manifest_url, f"manifest-{version}.json") if version else layout.manifest_url
    buffer = io.BytesIO()
    fetch(url, urlsplit(layout.manifest_url).scheme, MANIFEST_MAX_BYTES, buffer)
    manifest = parse_manifest(buffer.getvalue())
    if version and manifest.version != version:
        raise UpdateError(f"manifest-{version}.json describes {manifest.version}")
    return manifest


def download(layout: Layout, manifest: Manifest, dest: Path) -> None:
    url = urljoin(layout.manifest_url, manifest.bundle)
    with dest.open("wb") as handle:
        sha256 = fetch(
            url, urlsplit(layout.manifest_url).scheme, min(manifest.size, BUNDLE_MAX_BYTES), handle
        )
    if dest.stat().st_size != manifest.size:
        raise UpdateError(f"the bundle is {dest.stat().st_size} bytes, the manifest says {manifest.size}")
    if sha256 != manifest.sha256:
        raise UpdateError("the bundle's sha256 does not match the manifest")


# --- unpacking ---


def safe_member_path(name: str) -> PurePosixPath:
    """The member's relative path, or UpdateError for anything that could land outside the release."""
    if not name or "\x00" in name or "\\" in name:
        raise UpdateError(f"unsafe path in the bundle: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise UpdateError(f"unsafe path in the bundle: {name!r}")
    parts = [part for part in path.parts if part != "."]
    if not parts:
        raise UpdateError(f"unsafe path in the bundle: {name!r}")
    return PurePosixPath(*parts)


def unpack(bundle: Path, target: Path) -> None:
    """Extract into target (which must not exist yet). Every member is checked before any is written:
    only plain files and folders, relative paths that stay inside target, bounded count and size."""
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            members: list[tuple[PurePosixPath, tarfile.TarInfo]] = []
            total = 0
            for member in tar:
                if len(members) >= MAX_MEMBERS:
                    raise UpdateError(f"the bundle has more than {MAX_MEMBERS} entries")
                rel = safe_member_path(member.name)
                if not (member.isreg() or member.isdir()):
                    raise UpdateError(f"the bundle holds a link or special file: {member.name!r}")
                total += member.size
                if total > UNPACKED_MAX_BYTES:
                    raise UpdateError(f"the bundle unpacks to more than {UNPACKED_MAX_BYTES} bytes")
                members.append((rel, member))
            target.mkdir(parents=True)
            for rel, member in members:
                dest = target.joinpath(*rel.parts)
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    dest.chmod(0o755)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise UpdateError(f"cannot read {member.name!r} from the bundle")
                with source, dest.open("xb") as out:
                    shutil.copyfileobj(source, out)
                dest.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise UpdateError(f"cannot unpack the bundle: {exc}") from exc


# --- running the release ---


def _subprocess_env(layout: Layout) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", FIND_LINKS_ENV)
    }
    env["HOME"] = os.environ.get("HOME", str(layout.home))
    env["GAHK_WEB_HOME"] = str(layout.home)
    env["GAHK_WEB_CONFIG"] = str(layout.config)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_step(layout: Layout, what: str, args: list[str], timeout: int, cwd: Path | None = None) -> None:
    """Run a python subprocess; its output goes to the log, a non-zero exit becomes an UpdateError."""
    try:
        done = subprocess.run(  # noqa: S603 - our own interpreter with fixed arguments
            [sys.executable, "-I", *args],
            cwd=cwd,
            env=_subprocess_env(layout),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"{what} took longer than {timeout} s") from exc
    except OSError as exc:
        raise UpdateError(f"{what} could not start: {exc}") from exc
    output = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
    if output:
        log(layout, f"{what}: {output}")
    if done.returncode != 0:
        last = output.splitlines()[-1] if output else f"exit status {done.returncode}"
        raise UpdateError(f"{what} failed: {last}")


def run_release(layout: Layout, release: Path, what: str, command: str, timeout: int) -> None:
    run_step(layout, what, ["-c", RELEASE_RUNNER, str(release), command], timeout)


# --- the release's packages (vendor/) ---


def interpreter_abi() -> str:
    """Which compiled packages this python can load: interpreter, extension-module ABI, machine.

    Like "cpython-313 .cpython-313-x86_64-linux-gnu.so x86_64". The cgi-bin stub computes the same
    string, cheaply, on every request; keep the two in step."""
    tag = sys.implementation.cache_tag or sys.implementation.name
    return f"{tag} {importlib.machinery.EXTENSION_SUFFIXES[0]} {os.uname().machine}"


def python_tag() -> str:
    return sys.implementation.cache_tag or sys.implementation.name


def vendor_abi(vendor: Path) -> str:
    try:
        return (vendor / ABI_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def packages_ok(release: Path) -> bool:
    """Whether the release's vendor/ was installed for this python."""
    return vendor_abi(release / "vendor") == interpreter_abi()


def pip_command(requirements: Path, target: Path) -> list[str]:
    command = [
        sys.executable,
        "-I",
        "-m",
        PIP_MODULE,
        "install",
        "--target",
        str(target),
        "--require-hashes",
        "--only-binary=:all:",
        "--no-deps",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--no-input",
        "--no-warn-script-location",
        "--progress-bar",
        "off",
        "--timeout",
        "30",
        "--retries",
        "2",
        "-r",
        str(requirements),
    ]
    find_links = os.environ.get(FIND_LINKS_ENV, "")
    if find_links:
        command += ["--no-index", "--find-links", find_links]
    return command


def run_pip(layout: Layout, requirements: Path, target: Path) -> None:
    """pip install the requirements into target, isolated from any pip configuration of the host."""
    shutil.rmtree(layout.tmp, ignore_errors=True)
    layout.tmp.mkdir(parents=True, exist_ok=True)
    env = _subprocess_env(layout)
    env.update(TMPDIR=str(layout.tmp), PIP_CONFIG_FILE=os.devnull)
    started = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S603 - our own interpreter with fixed arguments
            pip_command(requirements, target),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PIP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log_pip(layout, (exc.stdout or b"") + (exc.stderr or b""))
        raise UpdateError(f"pip install took longer than {PIP_TIMEOUT} s") from exc
    except OSError as exc:
        raise UpdateError(f"pip install could not start: {exc}") from exc
    finally:
        shutil.rmtree(layout.tmp, ignore_errors=True)
    lines = _log_pip(layout, done.stdout + done.stderr)
    if done.returncode != 0:
        if any(f"No module named {PIP_MODULE}" in line for line in lines):
            raise UpdateError("pip is not available on the webhost")
        errors = [line.strip() for line in lines if line.startswith("ERROR:")] or lines[-1:]
        reason = " ".join(errors[-3:]) or f"exit status {done.returncode}"
        raise UpdateError(f"pip install: {reason[:500]}")
    log(layout, f"pip install took {time.monotonic() - started:.0f} s")


def _log_pip(layout: Layout, output: bytes) -> list[str]:
    """The last lines of pip's output to update.log; returns all of them."""
    lines = output.decode("utf-8", "replace").strip().splitlines()
    log(layout, "pip install: " + "\n".join(lines[-PIP_LOG_LINES:]))
    return lines


def copy_linked(source: Path, target: Path) -> None:
    """Copy a folder tree, hard-linking the files where the file system allows (no extra disk).

    A linked file is shared, so nothing may write into one in place: Python replaces its .pyc files
    atomically, and vendor/.abi is left out here and written anew."""

    def link(src: str, dst: str) -> None:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

    shutil.copytree(
        source, target, symlinks=True, copy_function=link, ignore=shutil.ignore_patterns(ABI_FILE)
    )


def install_packages(layout: Layout, release: Path, donors: list[Path]) -> Path:
    """Install the release's requirements.lock for this python into a new folder in the release.

    Returns the folder. The caller self-tests the release with it and then swaps it in with
    use_packages(); nothing the running site uses is touched. A donor (a kept release) whose lock
    is identical and whose packages are for this python is copied instead of running pip, so a
    release that changes only code needs neither PyPI nor time."""
    lock = release / REQUIREMENTS
    if not lock.is_file():
        raise UpdateError(f"the release has no {REQUIREMENTS}")
    wanted = lock.read_bytes()
    work = Path(tempfile.mkdtemp(prefix=".tmp-vendor-", dir=release))
    try:
        donor = next((d for d in donors if d != release and _same_packages(d, wanted)), None)
        if donor is not None:
            work.rmdir()
            copy_linked(donor / "vendor", work)
            how = f"copied the packages of {donor.name}"
        else:
            log(layout, f"{release.name}: installing packages for {python_tag()} with pip")
            run_pip(layout, lock, work)
            how = f"installed packages for {python_tag()}"
        write_atomic(work / ABI_FILE, (interpreter_abi() + "\n").encode("utf-8"))
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    log(layout, f"{release.name}: {how}")
    return work


def _same_packages(donor: Path, lock: bytes) -> bool:
    try:
        return packages_ok(donor) and (donor / REQUIREMENTS).read_bytes() == lock
    except OSError:
        return False


def use_packages(release: Path, packages: Path) -> None:
    """Make packages the release's vendor/ (two renames; the old vendor/ is deleted)."""
    vendor = release / "vendor"
    old = None
    if vendor.exists():
        old = Path(tempfile.mkdtemp(prefix=".tmp-old-vendor-", dir=release))
        vendor.replace(old / "vendor")
    packages.replace(vendor)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def kept_releases(layout: Layout, first: str = "") -> list[Path]:
    """The installed releases, `first` (the running one) first, then the most recently installed."""
    if not layout.releases.is_dir():
        return []
    found = [entry for entry in layout.releases.iterdir() if entry.is_dir() and SHA_RE.match(entry.name)]
    found.sort(key=lambda entry: (entry.name != first, -entry.stat().st_mtime))
    return found


def ready_release(layout: Layout, release: Path, current: str) -> None:
    """Packages for this python (unless the release has them already), and the release's self-test
    with them. The packages become the release's vendor/ only once the self-test passed."""
    selftest = [str(release / "selftest.py")]
    if packages_ok(release):
        run_step(layout, "self-test", selftest, SELFTEST_TIMEOUT, cwd=release)
        return
    packages = install_packages(layout, release, kept_releases(layout, current))
    try:
        run_step(layout, "self-test", [*selftest, "--vendor", str(packages)], SELFTEST_TIMEOUT, cwd=release)
        use_packages(release, packages)
    finally:
        shutil.rmtree(packages, ignore_errors=True)  # gone already when it became vendor/


def clean_leftovers(layout: Layout) -> None:
    """Delete what an interrupted run left behind: ~/gahk_web/tmp and the .tmp-* folders."""
    shutil.rmtree(layout.tmp, ignore_errors=True)
    if not layout.releases.is_dir():
        return
    for entry in layout.releases.iterdir():
        if entry.name.startswith(".tmp-"):
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.is_dir() and SHA_RE.match(entry.name):
            for inner in entry.glob(".tmp-*"):
                shutil.rmtree(inner, ignore_errors=True)


# --- installing ---


def static_files(static_root: Path) -> set[str]:
    if not static_root.is_dir():
        return set()
    return {path.relative_to(static_root).as_posix() for path in static_root.rglob("*") if path.is_file()}


def keep_local_block(new: bytes, installed: bytes | None) -> bytes:
    """The release's .htaccess with the installed file's LOCAL block in place of its own (empty) one."""
    old = LOCAL_BLOCK_RE.search(installed or b"")
    block = LOCAL_BLOCK_RE.search(new)
    if old is None or block is None:
        return new
    return new[: block.start(1)] + old.group(1) + new[block.end(1) :]


def install_public(layout: Layout, release: Path, installed_htaccess: bytes | None = None) -> None:
    """Copy the release's public_html files into place: static files first, then the stub, .htaccess
    (keeping the LOCAL block of the installed one)."""
    source = release / "public_html"
    for required in (".htaccess", "cgi-bin/gahk.cgi"):
        if not (source / required).is_file():
            raise UpdateError(f"the release has no public_html/{required}")
    for rel in sorted(static_files(source / "static")):
        write_atomic(layout.public_html / "static" / rel, (source / "static" / rel).read_bytes())
    cgi_bin = layout.public_html / "cgi-bin"
    cgi_bin.mkdir(parents=True, exist_ok=True)
    cgi_bin.chmod(0o755)  # suEXEC refuses a script in a group- or world-writable folder
    write_atomic(cgi_bin / "gahk.cgi", (source / "cgi-bin" / "gahk.cgi").read_bytes(), 0o755)
    htaccess = keep_local_block((source / ".htaccess").read_bytes(), installed_htaccess)
    write_atomic(layout.public_html / ".htaccess", htaccess)


def site_status(layout: Layout) -> int | None:
    """The HTTP status of the site's own GET /api/health, or None when it cannot be reached."""
    url = layout.base_url + "/api/health"
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "gahk_web-updater"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=SITE_CHECK_TIMEOUT) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        log(layout, f"site check: {url}: {exc}")
        return None


def check_htaccess(layout: Layout, previous: bytes | None) -> None:
    """After install_public: when the site answers 5xx and the previous .htaccess cures that, put the
    previous one back and fail the update, so the CGI stub (and with it POST /api/update) keeps working.

    Apache answers 500 for every URL under an .htaccess it rejects. A 5xx that the previous file does
    not cure (the database is down, say) is not the .htaccess's fault, so the new one stays. Skipped
    on the first install, where the operator is watching, and when the file did not change."""
    target = layout.public_html / ".htaccess"
    new = target.read_bytes()
    if previous is None or previous == new:
        return
    status = site_status(layout)
    if status is None or status < 500:
        return
    write_atomic(target, previous)
    restored = site_status(layout)
    if restored is not None and restored < 500:
        raise UpdateError(
            f"the new .htaccess breaks the site (HTTP {status}); put back the previous .htaccess"
        )
    log(layout, f"site check: HTTP {status} with the new .htaccess and {restored} with the old one")
    write_atomic(target, new)


def prune_static(layout: Layout, keep: set[str]) -> list[str]:
    """Delete content-hashed static files that no kept release ships. Unhashed ones are only replaced."""
    root = layout.public_html / "static"
    removed = []
    for rel in sorted(static_files(root) - keep):
        if HASHED_STATIC_RE.match(PurePosixPath(rel).name):
            with contextlib.suppress(OSError):
                (root / rel).unlink()
                removed.append(rel)
    return removed


def refresh_updater(layout: Layout, release: Path) -> str:
    """Take the release's updater for ~/gahk_web/bin/updater.py if it passes its own self-test."""
    new = release / "bin" / "updater.py"
    if not new.is_file():
        return "the release has no bin/updater.py"
    try:
        if layout.bin_updater.read_bytes() == new.read_bytes():
            return ""
    except OSError:
        pass
    try:
        run_step(layout, "updater self-test", [str(new), "--selftest"], SELFTEST_TIMEOUT)
    except UpdateError as exc:
        return f"kept the old bin/updater.py: {exc}"
    write_atomic(layout.bin_updater, new.read_bytes(), 0o755)
    log(layout, "installed the release's bin/updater.py")
    return ""


def prune_releases(layout: Layout, current: str) -> list[str]:
    """Keep the KEEP_RELEASES most recently installed releases (always the current and pinned ones)."""
    if not layout.releases.is_dir():
        return []
    removed = []
    releases = []
    for entry in layout.releases.iterdir():
        if entry.name.startswith(".tmp-"):
            shutil.rmtree(entry, ignore_errors=True)  # left by an interrupted run; we hold the lock
        elif entry.is_dir() and SHA_RE.match(entry.name):
            releases.append(entry)
    releases.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    keep = {entry.name for entry in releases[:KEEP_RELEASES]} | {current, layout.pin}
    for entry in releases:
        if entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def release_ok(release: Path, version: str) -> bool:
    try:
        return (release / "VERSION").read_text(encoding="utf-8").strip() == version and (
            release / "src" / "gahk_web"
        ).is_dir()
    except OSError:
        return False


def prepare_release(layout: Layout, manifest: Manifest) -> Path:
    """Download, verify and unpack the release (or reuse an intact copy from an earlier install)."""
    release = layout.releases / manifest.version
    if release_ok(release, manifest.version):
        log(layout, f"{manifest.version}: already unpacked")
        return release
    if release.exists():
        shutil.rmtree(release)
    layout.releases.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".tmp-", dir=layout.releases))
    try:
        bundle = work / manifest.bundle
        log(layout, f"{manifest.version}: downloading {manifest.bundle} ({manifest.size} bytes)")
        download(layout, manifest, bundle)
        unpacked = work / "release"
        unpack(bundle, unpacked)
        if not release_ok(unpacked, manifest.version):
            raise UpdateError("the bundle's VERSION does not match the manifest")
        unpacked.replace(release)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return release


# --- the update ---


@dataclass
class Result:
    updated: bool
    version: str
    available_version: str | None
    detail: str
    error: str | None = None

    def as_response(self) -> dict[str, Any]:
        return {
            "updated": self.updated,
            "version": self.version or None,
            "available_version": self.available_version,
            "detail": self.detail,
        }


def save_state(layout: Layout, result: Result) -> None:
    state = {
        "checked_at": utc_now(),
        "available_version": result.available_version,
        "last_result": "failed" if result.error else ("updated" if result.updated else "unchanged"),
        "last_error": result.error,
        "version": result.version or None,
        "detail": result.detail,
    }
    try:
        layout.state_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(layout.state_file, json.dumps(state, indent=2).encode("utf-8"))
    except OSError as exc:
        log(layout, f"could not write {layout.state_file}: {exc}")


def _install(layout: Layout, target: str, manifest: Manifest | None, current: str) -> Result:
    """Steps 3-11 for a version that differs from the running one. Without a manifest, the release
    must already be unpacked (a pin back to a release that is still kept)."""
    if manifest is None:
        release = layout.releases / target
        if not release_ok(release, target):
            raise UpdateError(f"{target} is neither kept here nor described by a manifest")
        log(layout, f"{target}: using the kept copy")
    else:
        if not python_ok(manifest.python):
            raise UpdateError(f"{target} needs Python {manifest.python}, this is {sys.version.split()[0]}")
        release = prepare_release(layout, manifest)
    ready_release(layout, release, current)
    run_release(layout, release, "migrations", "migrate", MIGRATE_TIMEOUT)
    try:
        previous_htaccess: bytes | None = (layout.public_html / ".htaccess").read_bytes()
    except FileNotFoundError:
        previous_htaccess = None
    install_public(layout, release, previous_htaccess)
    check_htaccess(layout, previous_htaccess)

    write_atomic(layout.current_file, (target + "\n").encode("ascii"))
    os.utime(release)  # "newest" for prune_releases means most recently installed
    log(layout, f"switched from {current or 'nothing'} to {target}")

    notes = []
    note = refresh_updater(layout, release)
    if note:
        notes.append(note)
    try:
        run_release(layout, release, "re-render", "rebuild", RENDER_TIMEOUT)
    except UpdateError as exc:  # the site re-renders lazily on the next request anyway
        notes.append(str(exc))
    previous = layout.releases / current if current else None
    keep = static_files(release / "public_html" / "static")
    if previous is not None:
        keep |= static_files(previous / "public_html" / "static")
    removed = prune_static(layout, keep)
    if removed:
        log(layout, f"removed old static files: {', '.join(removed)}")
    gone = prune_releases(layout, target)
    if gone:
        log(layout, f"removed old releases: {', '.join(gone)}")
    detail = f"installed {target}" + (f" (was {current})" if current else "")
    return Result(True, target, None, "; ".join([detail, *notes]))


def _reinstall_packages(layout: Layout, current: str) -> Result:
    """The running release's packages are for another python (Hetzner upgraded it, so the site
    answers 503): install them for this one, self-test, swap them in. No new version needed."""
    release = layout.releases / current
    installed = vendor_abi(release / "vendor") or "none"
    log(layout, f"{current}: its packages are for {installed}, this is {interpreter_abi()}")
    ready_release(layout, release, current)
    note = refresh_updater(layout, release)
    detail = f"reinstalled packages for {python_tag()} (running {current})"
    return Result(True, current, None, f"{detail}; {note}" if note else detail)


def run(home: Path | str | None = None) -> Result:
    """Check for a new release and install it. Never raises: failures are in the result."""
    layout = load_layout(home)
    try:
        with update_lock(layout) as locked:
            if not locked:
                current = read_current(layout)
                return Result(
                    False,
                    current,
                    read_state(layout).get("available_version"),
                    "an update is already running",
                )
            return _run_locked(layout)
    except LockError as exc:
        message = f"cannot take the update lock: {exc}"
        log(layout, f"update failed: {message}")
        result = Result(
            False,
            read_current(layout),
            read_state(layout).get("available_version"),
            f"failed: {message}",
            message,
        )
        save_state(layout, result)
        return result


def _run_locked(layout: Layout) -> Result:
    """run() once it holds the lock."""
    current = read_current(layout)
    available: str | None = None
    try:
        clean_leftovers(layout)
        if not layout.config.is_file():
            raise UpdateError(f"{layout.config} is missing")
        if layout.pin and not SHA_RE.match(layout.pin):
            raise UpdateError("[update] pin must be a full 40-character git sha")
        published = fetch_manifest(layout)
        available = published.version
        target = layout.pin or published.version
        manifest: Manifest | None = published
        if target != published.version:
            # A pin: a release that is still kept needs no download, else its own manifest.
            kept = release_ok(layout.releases / target, target)
            manifest = None if kept else fetch_manifest(layout, target)
        if target == current and release_ok(layout.releases / current, current):
            if packages_ok(layout.releases / current):
                note = refresh_updater(layout, layout.releases / current)  # heals an interrupted run
                pinned = (
                    f" (pinned; {available} is available)" if layout.pin and layout.pin != available else ""
                )
                result = Result(False, current, available, f"already running {current}{pinned}")
                if note:
                    result.detail += f"; {note}"
            else:
                result = _reinstall_packages(layout, current)
                result.available_version = available
        else:
            log(layout, f"updating {current or 'nothing'} -> {target}")
            result = _install(layout, target, manifest, current)
            result.available_version = available
    except UpdateError as exc:
        log(layout, f"update failed: {exc}")
        result = Result(False, read_current(layout), available, f"failed: {exc}", str(exc))
    except Exception as exc:  # anything unexpected must still be reported, not lost
        log(layout, f"update failed unexpectedly: {exc!r}")
        result = Result(False, read_current(layout), available, f"failed: {exc!r}", repr(exc))
    save_state(layout, result)
    return result


def nudge(home: Path | str | None = None, wait: float = NUDGE_WAIT) -> dict[str, Any]:
    """POST /api/update: start run() in its own process and session, wait for it up to `wait` seconds.

    The update is detached from the CGI request, so neither Apache's CGI timeout nor a client that
    hangs up can stop it halfway; a slow install finishes after the answer, and GET /api/status shows
    how it ended."""
    root = home_dir(home)
    layout = load_layout(root)
    try:
        process = subprocess.Popen(  # noqa: S603 - our own interpreter and file
            [sys.executable, "-I", str(Path(__file__).resolve()), "run", "--home", str(root)],
            env=_subprocess_env(layout),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return Result(
            False, read_current(layout), None, f"failed: could not start the updater: {exc}"
        ).as_response()
    try:
        output, _ = process.communicate(timeout=wait)
    except subprocess.TimeoutExpired:
        state = read_state(layout)
        return Result(
            False,
            read_current(layout),
            state.get("available_version"),
            "the update is still running; GET /api/status shows how it ends",
        ).as_response()
    try:
        data = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        tail = read_state(layout).get("detail") or "the updater gave no result"
        return Result(False, read_current(layout), None, f"failed: {tail}").as_response()
    return data


# --- self-test of this file (python3 -I updater.py --selftest) ---


def _tar_bytes(entries: list[tarfile.TarInfo], payload: bytes = b"x") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info in entries:
            tar.addfile(info, io.BytesIO(payload) if info.isreg() else None)
    return buffer.getvalue()


def selftest() -> list[str]:
    """Problems that would stop this updater from working with this python; empty when it is fine."""
    problems = []
    if sys.version_info < MIN_PYTHON:
        problems.append(f"Python {sys.version.split()[0]} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
    try:
        import ssl  # noqa: F401 - only to prove HTTPS works here

        urllib.request.HTTPSHandler  # noqa: B018 - missing when python was built without ssl
    except (ImportError, AttributeError):
        problems.append("this python has no ssl module, so it cannot fetch from GitHub")
    sample = json.dumps(
        {"version": "a" * 40, "bundle": f"gahk_web-{'a' * 40}.tar.gz", "sha256": "b" * 64, "size": 10}
    ).encode()
    try:
        parse_manifest(sample)
    except UpdateError as exc:
        problems.append(f"manifest parsing: {exc}")
    with tempfile.TemporaryDirectory() as scratch:
        good, bad = tarfile.TarInfo("src/ok.txt"), tarfile.TarInfo("../evil.txt")
        good.size = bad.size = 1
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        for name, entries, should_pass in (
            ("good", [good], True),
            ("dotdot", [bad], False),
            ("symlink", [link], False),
        ):
            bundle = Path(scratch) / f"{name}.tar.gz"
            bundle.write_bytes(_tar_bytes(entries))
            try:
                unpack(bundle, Path(scratch) / name)
                passed = True
            except UpdateError:
                passed = False
            if passed != should_pass:
                problems.append(f"unpack check failed on the {name} sample")
        lock_layout = Layout(Path(scratch), Path(scratch) / "c.ini", Path(scratch), Path(scratch))
        try:
            with update_lock(lock_layout) as locked:
                if not locked:
                    problems.append("cannot take the update lock")
        except LockError as exc:
            problems.append(f"cannot take the update lock: {exc}")
    return problems


def install_release_packages(release: Path) -> int:
    """updater.py packages RELEASE: step 5 of an update for an unpacked release (CI, smoke tests)."""
    with tempfile.TemporaryDirectory(prefix="gahk_web-packages-") as scratch:
        root = Path(scratch)
        layout = Layout(root, root / "config.ini", root / "public_html", root / "state")
        try:
            use_packages(release, install_packages(layout, release, []))
        except UpdateError as exc:
            with contextlib.suppress(OSError):
                print(layout.log_file.read_text(encoding="utf-8"), file=sys.stderr)
            print(f"packages: {exc}", file=sys.stderr)
            return 1
    print(f"installed packages for {python_tag()} into {release / 'vendor'}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "packages":
        return install_release_packages(Path(argv[1]).resolve())
    if argv == ["--selftest"]:
        problems = selftest()
        for problem in problems:
            print(f"updater selftest: {problem}", file=sys.stderr)
        if not problems:
            print(f"updater selftest ok (Python {sys.version.split()[0]})")
        return 1 if problems else 0
    if argv[:1] == ["run"] and len(argv) in (1, 3) and (len(argv) == 1 or argv[1] == "--home"):
        result = run(argv[2] if len(argv) == 3 else None)
        with contextlib.suppress(OSError):  # nudge() may have answered and gone already
            print(json.dumps(result.as_response()), flush=True)
        return 1 if result.error else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
