"""The self-updater: fetch, verify, unpack, self-test, migrate, install, switch, prune."""

import ast
import errno
import json
import sys
import tarfile
import threading
import time
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from bundles import BAD_LOCK, LOCK, css_name, make_bundle, publish, version, write_config

from gahk_web import updater

KEY = "k" * 40


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_pip: Path) -> Path:
    monkeypatch.delenv("GAHK_WEB_CONFIG", raising=False)
    monkeypatch.delenv("GAHK_WEB_HOME", raising=False)
    home = tmp_path / "home"
    (home / "public_html").mkdir(parents=True)
    write_config(home, (tmp_path / "dist").as_uri() + "/manifest.json", KEY)
    return home


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    path = tmp_path / "dist"
    path.mkdir()
    return path


def release(dist: Path, n: int, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    manifest = make_bundle(dist, version(n), **kwargs)
    publish(dist, manifest)
    return manifest


def current(home: Path) -> str:
    return updater.read_current(updater.load_layout(home))


def calls(home: Path) -> list[str]:
    log = home / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def state(home: Path) -> dict[str, Any]:
    return json.loads((home / "gahk_web" / "state" / "update.json").read_text())


def releases(home: Path) -> set[str]:
    return {entry.name for entry in (home / "gahk_web" / "releases").iterdir()}


def vendor(home: Path, sha: str) -> Path:
    return home / "gahk_web" / "releases" / sha / "vendor"


def update_log(home: Path) -> str:
    return (home / "gahk_web" / "var" / "update.log").read_text()


FOREIGN_ABI = "cpython-399 .cpython-399-elsewhere.so elsewhere\n"


@pytest.fixture
def pip_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Counts the updater's pip runs (each still runs, offline)."""
    runs: list[str] = []
    real = updater.run_pip

    def counting(layout: updater.Layout, requirements: Path, target: Path) -> None:
        runs.append(requirements.parent.name)
        real(layout, requirements, target)

    monkeypatch.setattr(updater, "run_pip", counting)
    return runs


def test_first_install_bootstraps_everything(home: Path, dist: Path) -> None:
    v1 = version(1)
    release(dist, 1)

    result = updater.run(home)

    assert result.updated and result.error is None, result.detail
    assert result.as_response() == {
        "updated": True,
        "version": v1,
        "available_version": v1,
        "detail": f"installed {v1}",
    }
    assert current(home) == v1
    assert calls(home) == [f"{v1[:7]} migrate", f"{v1[:7]} rebuild"]
    public = home / "public_html"
    assert (public / ".htaccess").read_text() == f"# {v1}\n"
    assert (public / "cgi-bin" / "gahk.cgi").stat().st_mode & 0o777 == 0o755
    assert (public / "static" / css_name(v1)).is_file()
    assert (public / "static" / "fonts" / "ubuntu.woff2").is_file()
    bin_updater = home / "gahk_web" / "bin" / "updater.py"
    assert bin_updater.read_text() == Path(updater.__file__).read_text()
    assert state(home)["last_result"] == "updated"
    assert state(home)["available_version"] == v1
    assert state(home)["last_error"] is None
    assert "switched from nothing to" in update_log(home)


def test_a_fresh_install_installs_the_packages_with_pip(home: Path, dist: Path, pip_runs: list[str]) -> None:
    v1 = version(1)
    release(dist, 1)

    assert updater.run(home).updated

    assert pip_runs == [v1]
    assert (vendor(home, v1) / "gahkfake" / "__init__.py").is_file()
    assert (vendor(home, v1) / ".abi").read_text() == updater.interpreter_abi() + "\n"
    assert f"{v1}: installed packages for {updater.python_tag()}" in update_log(home)
    assert "pip install: " in update_log(home)
    leftovers = [p.name for p in (home / "gahk_web" / "releases" / v1).iterdir() if p.name.startswith(".")]
    assert leftovers == []
    assert not (home / "gahk_web" / "tmp").exists()


def test_the_pip_command_is_isolated_and_hash_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(updater.FIND_LINKS_ENV, raising=False)
    command = updater.pip_command(tmp_path / "requirements.lock", tmp_path / "vendor")

    assert command[:5] == [sys.executable, "-I", "-m", "pip", "install"]
    for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps", "--no-cache-dir", "--no-input"):
        assert flag in command
    assert "--no-index" not in command  # PyPI in production


def test_an_identical_lock_reuses_the_packages_without_pip(
    home: Path, dist: Path, pip_runs: list[str]
) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2)

    result = updater.run(home)

    assert result.updated, result.detail
    assert pip_runs == [v1]  # not again for v2
    assert f"{v2}: copied the packages of {v1}" in update_log(home)
    old, new = vendor(home, v1) / "gahkfake" / "__init__.py", vendor(home, v2) / "gahkfake" / "__init__.py"
    assert new.read_text() == old.read_text()
    assert new.stat().st_ino == old.stat().st_ino  # a hard link: no extra disk
    old_abi, new_abi = vendor(home, v1) / ".abi", vendor(home, v2) / ".abi"
    assert new_abi.stat().st_ino != old_abi.stat().st_ino  # never shared: it is rewritten
    assert (vendor(home, v2) / ".abi").read_text() == updater.interpreter_abi() + "\n"


def test_a_changed_lock_runs_pip(home: Path, dist: Path, pip_runs: list[str]) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, requirements=LOCK + "# bumped\n")

    assert updater.run(home).updated

    assert pip_runs == [v1, v2]


def test_a_pip_failure_leaves_the_old_release_running(home: Path, dist: Path) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, requirements=BAD_LOCK)

    result = updater.run(home)

    assert not result.updated
    assert result.error is not None
    assert result.error.startswith("pip install: ERROR: THESE PACKAGES DO NOT MATCH THE HASHES")
    assert result.detail == f"failed: {result.error}"
    assert current(home) == v1
    assert f"{v2[:7]} migrate" not in calls(home)
    assert state(home)["last_result"] == "failed"
    assert state(home)["last_error"] == result.error
    assert not vendor(home, v2).exists()
    assert not list((home / "gahk_web" / "releases" / v2).glob(".tmp-*"))
    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in update_log(home)


def test_without_pip_the_update_says_so(home: Path, dist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "PIP_MODULE", "gahk_no_such_pip")
    release(dist, 1)

    result = updater.run(home)

    assert result.error == "pip is not available on the webhost"
    assert result.detail == "failed: pip is not available on the webhost"
    assert current(home) == ""


def test_a_python_upgrade_reinstalls_the_packages_on_the_next_nudge(
    home: Path, dist: Path, pip_runs: list[str]
) -> None:
    v1 = version(1)
    release(dist, 1)
    updater.run(home)
    (vendor(home, v1) / ".abi").write_text(FOREIGN_ABI)  # installed for the python Hetzner replaced

    result = updater.run(home)

    assert result.updated and result.error is None, result.detail
    assert result.detail == f"reinstalled packages for {updater.python_tag()} (running {v1})"
    assert result.available_version == v1
    assert pip_runs == [v1, v1]
    assert updater.packages_ok(home / "gahk_web" / "releases" / v1)
    assert calls(home) == [f"{v1[:7]} migrate", f"{v1[:7]} rebuild"]  # nothing else ran
    assert state(home)["last_result"] == "updated"
    assert "its packages are for cpython-399" in update_log(home)
    assert updater.run(home).detail == f"already running {v1}"


def test_a_failed_reinstall_leaves_the_packages_as_they_were(
    home: Path, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = version(1)
    release(dist, 1)
    updater.run(home)
    (vendor(home, v1) / ".abi").write_text(FOREIGN_ABI)
    monkeypatch.setattr(updater, "PIP_MODULE", "gahk_no_such_pip")

    result = updater.run(home)

    assert result.error == "pip is not available on the webhost"
    assert (vendor(home, v1) / ".abi").read_text() == FOREIGN_ABI
    assert current(home) == v1


def test_a_pin_back_to_a_release_with_foreign_packages_reinstalls_them(
    home: Path, dist: Path, pip_runs: list[str]
) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2)
    updater.run(home)
    (vendor(home, v1) / ".abi").write_text(FOREIGN_ABI)
    write_config(home, (dist / "manifest.json").as_uri(), KEY, pin=v1)

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v1
    assert updater.packages_ok(home / "gahk_web" / "releases" / v1)
    assert pip_runs == [v1]  # copied from v2, which has the same lock and fits this python
    assert f"{v1}: copied the packages of {v2}" in update_log(home)


def test_leftovers_of_an_interrupted_run_are_deleted(home: Path, dist: Path) -> None:
    v1 = version(1)
    release(dist, 1)
    updater.run(home)
    leftovers = [
        home / "gahk_web" / "tmp" / "pip-build",
        home / "gahk_web" / "releases" / v1 / ".tmp-vendor-x" / "gahkfake",
        home / "gahk_web" / "releases" / ".tmp-y" / "release",
    ]
    for path in leftovers:
        path.mkdir(parents=True)

    updater.run(home)

    assert not any(path.parent.exists() for path in leftovers)
    assert (vendor(home, v1) / "gahkfake").is_dir()


def test_the_packages_command_installs_like_an_update(dist: Path, tmp_path: Path, offline_pip: Path) -> None:
    manifest = make_bundle(dist, version(1))
    target = tmp_path / "release"
    updater.unpack(dist / manifest["bundle"], target)

    assert updater.main(["packages", str(target)]) == 0

    assert (target / "vendor" / "gahkfake").is_dir()
    assert updater.packages_ok(target)


def test_an_install_keeps_the_local_block_of_the_installed_htaccess(home: Path, dist: Path) -> None:
    v2 = version(2)
    template = "# {}\n# BEGIN LOCAL\n# END LOCAL\nRewriteEngine On\n"
    release(dist, 1, htaccess=template.format("one"))
    updater.run(home)
    installed = home / "public_html" / ".htaccess"
    local = "SetEnv GAHK_WEB_HOME /usr/home/gahk\n"
    installed.write_text(installed.read_text().replace("# END LOCAL", local + "# END LOCAL"))
    release(dist, 2, htaccess=template.format("two") + "# no local block in what the release ships\n")

    assert updater.run(home).updated
    assert current(home) == v2
    assert installed.read_text() == (
        f"# two\n# BEGIN LOCAL\n{local}# END LOCAL\nRewriteEngine On\n"
        "# no local block in what the release ships\n"
    )


def test_keep_local_block() -> None:
    new = b"a\n# BEGIN LOCAL (host)\n# END LOCAL\nb\n"
    installed = b"x\n# BEGIN LOCAL (host)\nSetEnv A b\n# END LOCAL\ny\n"
    assert (
        updater.keep_local_block(new, installed) == b"a\n# BEGIN LOCAL (host)\nSetEnv A b\n# END LOCAL\nb\n"
    )
    assert updater.keep_local_block(new, None) == new
    assert updater.keep_local_block(new, b"no block") == new
    assert updater.keep_local_block(b"no block", installed) == b"no block"
    # An empty installed block takes the release's commented-out switches.
    switches = b"a\n# BEGIN LOCAL\n# RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n# END LOCAL\n"
    assert updater.keep_local_block(switches, b"# BEGIN LOCAL\n\n# END LOCAL\n") == switches


def test_unchanged_manifest_is_a_no_op(home: Path, dist: Path) -> None:
    v1 = version(1)
    release(dist, 1)
    updater.run(home)

    result = updater.run(home)

    assert not result.updated and result.error is None
    assert result.detail == f"already running {v1}"
    assert calls(home) == [f"{v1[:7]} migrate", f"{v1[:7]} rebuild"]  # nothing ran again
    assert state(home)["last_result"] == "unchanged"


def test_a_bundle_with_the_wrong_sha256_is_refused(home: Path, dist: Path) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    manifest = make_bundle(dist, v2)
    publish(dist, {**manifest, "sha256": "0" * 64})

    result = updater.run(home)

    assert not result.updated
    assert result.error == "the bundle's sha256 does not match the manifest"
    assert current(home) == v1
    assert releases(home) == {v1}  # no release and no leftover temporary folder
    assert state(home)["last_result"] == "failed"
    assert state(home)["available_version"] == v2


def test_a_bundle_larger_than_the_manifest_says_is_refused(home: Path, dist: Path) -> None:
    manifest = make_bundle(dist, version(1))
    publish(dist, {**manifest, "size": manifest["size"] - 10})

    result = updater.run(home)

    assert result.error is not None and "larger than" in result.error
    assert current(home) == ""


def unsafe(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.size = 1 if kind == tarfile.REGTYPE else 0
    return info


@pytest.mark.parametrize(
    "member",
    [
        unsafe("../evil.py"),
        unsafe("src/../../evil.py"),
        unsafe("/tmp/evil.py"),
        unsafe("src/link", tarfile.SYMTYPE, "/etc/passwd"),
        unsafe("src/hard", tarfile.LNKTYPE, "VERSION"),
        unsafe("src/dev", tarfile.CHRTYPE),
        unsafe("src/fifo", tarfile.FIFOTYPE),
    ],
    ids=["dotdot", "nested-dotdot", "absolute", "symlink", "hardlink", "device", "fifo"],
)
def test_unsafe_tar_members_are_refused(
    home: Path, dist: Path, tmp_path: Path, member: tarfile.TarInfo
) -> None:
    release(dist, 1, extra=[member])

    result = updater.run(home)

    assert result.error is not None
    assert "unsafe path" in result.error or "link or special file" in result.error
    assert current(home) == ""
    assert releases(home) == set()
    assert not (tmp_path / "evil.py").exists() and not (home / "gahk_web" / "evil.py").exists()


def test_a_failing_selftest_leaves_the_old_release_running(home: Path, dist: Path) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, failing=("selftest",))

    result = updater.run(home)

    assert not result.updated
    assert result.error == "self-test failed: selftest broke"
    assert current(home) == v1
    assert f"{v2[:7]} migrate" not in calls(home)  # it stopped before the migrations
    assert (home / "public_html" / ".htaccess").read_text() == f"# {v1}\n"
    assert not (home / "public_html" / "static" / css_name(v2)).exists()


def test_a_failing_migration_leaves_the_old_release_running(home: Path, dist: Path) -> None:
    v1 = version(1)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, failing=("migrate",))

    result = updater.run(home)

    assert result.error == "migrations failed: migrate broke"
    assert current(home) == v1
    assert (home / "public_html" / ".htaccess").read_text() == f"# {v1}\n"


def test_a_failing_rerender_does_not_undo_the_switch(home: Path, dist: Path) -> None:
    v2 = version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, failing=("rebuild",))

    result = updater.run(home)

    assert result.updated and result.error is None
    assert "re-render failed" in result.detail
    assert current(home) == v2


def test_pin_holds_a_kept_release_without_downloading(home: Path, dist: Path) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2)
    updater.run(home)
    (dist / f"gahk_web-{v1}.tar.gz").unlink()  # proves the kept copy is used
    write_config(home, (dist / "manifest.json").as_uri(), KEY, pin=v1)

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v1
    assert result.available_version == v2
    assert (home / "public_html" / ".htaccess").read_text() == f"# {v1}\n"

    again = updater.run(home)
    assert not again.updated
    assert again.detail == f"already running {v1} (pinned; {v2} is available)"


def test_pin_downloads_a_release_from_its_own_manifest(home: Path, dist: Path) -> None:
    v1, v3 = version(1), version(3)
    make_bundle(dist, v1)
    release(dist, 2)
    make_bundle(dist, v3)
    write_config(home, (dist / "manifest.json").as_uri(), KEY, pin=v3)

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v3
    assert releases(home) == {v3}


def test_a_short_pin_is_refused(home: Path, dist: Path) -> None:
    release(dist, 1)
    write_config(home, (dist / "manifest.json").as_uri(), KEY, pin="abc1234")

    assert updater.run(home).error == "[update] pin must be a full 40-character git sha"


def test_installs_switch_current_and_keep_three_releases(home: Path, dist: Path) -> None:
    shas = [version(n) for n in range(1, 5)]
    for n, sha in enumerate(shas, start=1):
        release(dist, n)
        result = updater.run(home)
        assert result.updated, result.detail
        assert current(home) == sha
        time.sleep(0.01)  # distinct install times

    assert releases(home) == set(shas[1:])
    static = home / "public_html" / "static"
    # The previous release's hashed files stay (pages cached by browsers still name them).
    assert not (static / css_name(shas[0])).exists()
    assert not (static / css_name(shas[1])).exists()
    assert (static / css_name(shas[2])).exists()
    assert (static / css_name(shas[3])).exists()
    assert (static / "fonts" / "ubuntu.woff2").exists()  # unhashed files are only replaced


def test_bin_updater_is_replaced_only_by_one_that_passes_its_selftest(home: Path, dist: Path) -> None:
    release(dist, 1)
    updater.run(home)
    bin_updater = home / "gahk_web" / "bin" / "updater.py"
    before = bin_updater.read_text()
    release(dist, 2, updater_source="import sys\nsys.exit(3)\n")

    result = updater.run(home)

    assert result.updated
    assert "kept the old bin/updater.py" in result.detail
    assert bin_updater.read_text() == before


def test_only_one_update_runs_at_a_time(home: Path, dist: Path) -> None:
    release(dist, 1)
    layout = updater.load_layout(home)
    with updater.update_lock(layout) as locked:
        assert locked
        result = updater.run(home)
    assert result.detail == "an update is already running"
    assert current(home) == ""


def test_a_lock_that_cannot_be_taken_is_a_failure(
    home: Path, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only contention means "already running"; e.g. ENOLCK on a network home is a failure."""
    release(dist, 1)

    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(updater.fcntl, "flock", no_locks)
    result = updater.run(home)

    assert result.error is not None
    assert result.detail.startswith("failed: cannot take the update lock:")
    assert "No locks available" in result.detail
    assert state(home)["last_result"] == "failed"
    assert current(home) == ""


def test_a_missing_manifest_is_reported(home: Path, dist: Path) -> None:
    result = updater.run(home)

    assert result.error is not None and "manifest.json" in result.error
    assert state(home)["last_result"] == "failed"


def test_without_config_nothing_happens(home: Path, dist: Path) -> None:
    release(dist, 1)
    (home / "gahk_web" / "config.ini").unlink()

    result = updater.run(home)

    assert result.error is not None and "config.ini is missing" in result.error
    assert not (home / "gahk_web" / "releases").exists()
    assert current(home) == ""


class _Redirecting(SimpleHTTPRequestHandler):
    """Like GitHub: /download/<name> answers 302 to where the file really is."""

    def do_GET(self) -> None:
        if self.path.startswith("/download/"):
            self.send_response(302)
            self.send_header("Location", "/files/" + self.path.removeprefix("/download/"))
            self.end_headers()
            return
        self.path = self.path.removeprefix("/files")
        super().do_GET()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def http_dist(dist: Path) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: _Redirecting(*args, directory=str(dist)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/download/manifest.json"
    server.shutdown()


def test_downloads_follow_redirects(home: Path, dist: Path, http_dist: str) -> None:
    release(dist, 1)
    write_config(home, http_dist, KEY)

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == version(1)


class _Site(SimpleHTTPRequestHandler):
    """Plays Apache and the app for GET /api/health: 500 while public_html/.htaccess holds "Broken"
    (an .htaccess Apache rejects); otherwise STATUS with the version in CURRENT, through RUNNER
    ("cgi" when the LOCAL block switches to CGI only). The first OLD_ANSWERS answers come from
    FastCGI processes of the release before (PREVIOUS)."""

    home: Path
    status = 200
    runner = "fastcgi"
    old_answers = 0
    previous = ""
    served = 0

    def do_GET(self) -> None:
        site = type(self)
        site.served += 1
        htaccess = (self.home / "public_html" / ".htaccess").read_bytes()
        if b"Broken" in htaccess:
            self.send_response(500)
            self.end_headers()
            return
        if b"Misrouted" in htaccess:  # Apache's own 404: the request never reaches the app
            self.send_response(404)
            self.end_headers()
            return
        version = (self.home / "gahk_web" / "CURRENT").read_text().strip()
        runner = "cgi" if updater.cgi_only(htaccess) else site.runner
        if site.old_answers > 0:
            site.old_answers -= 1
            version = site.previous
        body = json.dumps({"ok": site.status == 200, "version": version, "revision": None}).encode()
        self.send_response(site.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Gahk-Runner", runner)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def site(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Site]]:
    monkeypatch.setattr(updater, "SITE_CHECK_PAUSE", 0)
    handler = type("Site", (_Site,), {"home": home})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifest_url = (home.parent / "dist").as_uri() + "/manifest.json"
    write_config(home, manifest_url, KEY, base_url=f"http://127.0.0.1:{server.server_address[1]}")
    yield handler
    server.shutdown()


def test_after_the_switch_the_new_release_must_answer_through_fastcgi(
    home: Path, dist: Path, site: type[_Site]
) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2)
    # Two old FastCGI processes answer once more each, then end; the third answer is the new release.
    site.old_answers, site.previous, site.served = 2, v1, 0

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v2
    assert site.served == 3
    assert (home / "public_html" / "cgi-bin" / "gahk.fcgi").read_text().endswith(f"# fcgi {v2}\n")
    assert f"site check: HTTP 200, version {v2}, through fastcgi" in update_log(home)


def test_an_htaccess_that_breaks_the_site_is_put_back(home: Path, dist: Path, site: type[_Site]) -> None:
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, htaccess="Broken\n")

    result = updater.run(home)

    assert (
        result.error is not None and "the site does not answer with the new release (HTTP 500" in result.error
    )
    assert "put back the previous release, .htaccess and stubs" in result.error
    assert (home / "public_html" / ".htaccess").read_text() == f"# {v1}\n"
    assert current(home) == v1  # the old release runs again
    assert calls(home)[-1] == f"{v2[:7]} migrate"  # put back before it was re-rendered
    assert state(home)["last_result"] == "failed"


def test_a_release_fastcgi_cannot_run_is_put_back(home: Path, dist: Path, site: type[_Site]) -> None:
    """gahk.fcgi's stand-in answers for the new release (it cannot start): the previous one goes back."""
    v1 = version(1)
    release(dist, 1)
    updater.run(home)
    release(dist, 2)
    site.runner = "gahk.fcgi"

    result = updater.run(home)

    assert result.error is not None and "through gahk.fcgi, expected" in result.error
    assert current(home) == v1
    # The stubs went back too: a broken new gahk.fcgi must not outlive the rollback.
    assert (home / "public_html" / "cgi-bin" / "gahk.fcgi").read_text().endswith(f"# fcgi {v1}\n")
    assert (home / "public_html" / "cgi-bin" / "gahk.fcgi").stat().st_mode & 0o777 == 0o755


def test_a_5xx_the_old_release_does_not_cure_keeps_the_new_one(
    home: Path, dist: Path, site: type[_Site]
) -> None:
    """The database is down, say: not the new release's fault, so the update goes through."""
    v2 = version(2)
    release(dist, 1)
    updater.run(home)
    release(dist, 2)
    site.status = 503
    site.runner = ""  # the site answers 503 without the app (the old release's answer would do the same)

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v2
    assert (home / "public_html" / ".htaccess").read_text() == f"# {v2}\n"
    assert "the previous release does no better" in update_log(home)


def test_with_the_cgi_only_switch_the_release_must_answer_through_cgi(
    home: Path, dist: Path, site: type[_Site]
) -> None:
    v2 = version(2)
    template = "# {}\n# BEGIN LOCAL\n# RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n# END LOCAL\n"
    release(dist, 1, htaccess=template.format("one"))
    updater.run(home)
    installed = home / "public_html" / ".htaccess"
    installed.write_text(installed.read_text().replace("# RewriteRule", "RewriteRule"))
    release(dist, 2, htaccess=template.format("two"))

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v2
    assert "RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n# END LOCAL" in installed.read_text()
    assert f"version {v2}, through cgi" in update_log(home)


def test_a_release_from_before_fastcgi_must_answer_through_cgi(
    home: Path, dist: Path, site: type[_Site]
) -> None:
    """A pin back to a release without gahk.fcgi takes the installed one away."""
    v1, v2 = version(1), version(2)
    release(dist, 1)
    updater.run(home)
    site.runner = "cgi"
    release(dist, 2, fcgi=False)

    result = updater.run(home)

    assert result.updated, result.detail
    assert current(home) == v2
    assert not (home / "public_html" / "cgi-bin" / "gahk.fcgi").exists()
    assert (home / "public_html" / "cgi-bin" / "gahk.cgi").read_text().endswith(f"# {v2}\n")
    assert v1 != v2


def test_an_htaccess_that_misroutes_the_api_is_put_back(home: Path, dist: Path, site: type[_Site]) -> None:
    """The site answered as this one before the switch; afterwards /api/health is Apache's own 404
    (no X-Gahk-Runner): the new .htaccess no longer reaches the app, so it is not "another site"."""
    v1 = version(1)
    release(dist, 1)
    updater.run(home)
    release(dist, 2, htaccess="Misrouted\n")

    result = updater.run(home)

    assert (
        result.error is not None and "the site does not answer with the new release (HTTP 404" in result.error
    )
    assert (home / "public_html" / ".htaccess").read_text() == f"# {v1}\n"
    assert current(home) == v1
    assert "not this site" not in update_log(home)


def test_an_answer_from_another_site_skips_the_check(home: Path, dist: Path, site: type[_Site]) -> None:
    """Before the DNS switch gahk.dk is still the old site, which knows no /api/health."""
    release(dist, 1)
    updater.run(home)
    release(dist, 2)
    site.status, site.runner = 404, ""

    result = updater.run(home)

    assert result.updated, result.detail
    assert "not this site" in update_log(home)


def test_cgi_only() -> None:
    assert not updater.cgi_only(b"# BEGIN LOCAL\n# RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n# END LOCAL\n")
    assert updater.cgi_only(b"# BEGIN LOCAL\n  RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n# END LOCAL\n")
    assert not updater.cgi_only(b"RewriteRule ^ - [E=GAHK_CGI_ONLY:1]\n")  # only the LOCAL block counts
    assert not updater.cgi_only(b"# BEGIN LOCAL\n# END LOCAL\nRewriteCond %{ENV:GAHK_CGI_ONLY} =1\n")


def test_nudge_runs_the_update_in_its_own_process(home: Path, dist: Path) -> None:
    release(dist, 1)

    result = updater.nudge(home)

    assert result["updated"] is True, result
    assert result["version"] == version(1)
    assert current(home) == version(1)


def test_nudge_answers_before_a_slow_update_ends(home: Path, dist: Path) -> None:
    release(dist, 1)

    result = updater.nudge(home, wait=0)

    assert result["updated"] is False
    assert "still running" in result["detail"]
    deadline = time.monotonic() + 30
    while current(home) != version(1) and time.monotonic() < deadline:  # it carries on detached
        time.sleep(0.1)
    assert current(home) == version(1)
    while time.monotonic() < deadline:  # let it finish before tmp_path goes away
        layout = updater.load_layout(home)
        with updater.update_lock(layout) as locked:
            if locked:
                break
        time.sleep(0.1)


def test_manifest_validation() -> None:
    good = {"version": "a" * 40, "bundle": f"gahk_web-{'a' * 40}.tar.gz", "sha256": "b" * 64, "size": 10}
    assert updater.parse_manifest(json.dumps(good).encode()).python == ">=3.13"
    for bad in (
        {**good, "version": "abc"},
        {**good, "bundle": "../evil.tar.gz"},
        {**good, "sha256": "xyz"},
        {**good, "size": updater.BUNDLE_MAX_BYTES + 1},
        {**good, "size": True},
        {**good, "python": "3.13"},
    ):
        with pytest.raises(updater.UpdateError):
            updater.parse_manifest(json.dumps(bad).encode())


def test_a_release_for_a_newer_python_is_refused(home: Path, dist: Path) -> None:
    manifest = make_bundle(dist, version(1))
    publish(dist, {**manifest, "python": ">=3.99"})

    result = updater.run(home)

    assert result.error is not None and "needs Python >=3.99" in result.error


def test_the_log_is_rotated(home: Path) -> None:
    layout = updater.load_layout(home)
    layout.var.mkdir(parents=True)
    layout.log_file.write_text("x" * (updater.LOG_MAX_BYTES + 1))

    updater.log(layout, "fresh")

    assert layout.log_file.read_text().endswith("fresh\n")
    assert layout.log_file.with_name("update.log.1").stat().st_size > updater.LOG_MAX_BYTES


def test_the_updater_uses_the_standard_library_only() -> None:
    tree = ast.parse(Path(updater.__file__).read_text())
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    modules |= {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert modules - {"__future__"} <= sys.stdlib_module_names
    assert not any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree))


def test_the_updater_selftest_passes() -> None:
    assert updater.selftest() == []
