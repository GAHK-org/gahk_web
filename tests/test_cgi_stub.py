"""The cgi-bin stub: finds the active release, runs it with only its own packages, and handles
POST /api/update itself (the bootstrap, and the repair after a python upgrade)."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from bundles import STUB, UPDATER_SOURCE, make_bundle, publish, version, write_config

from gahk_web import signing, updater

KEY = "k" * 40


def cgi(  # noqa: ANN201
    home: Path,
    method: str,
    path: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    stub: Path = STUB,
):
    """Run the stub as Apache would; returns (status, headers, body)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GAHK_WEB_PIP_FIND_LINKS": os.environ.get("GAHK_WEB_PIP_FIND_LINKS", ""),
        "GAHK_WEB_HOME": str(home),
        "REQUEST_METHOD": method,
        "REQUEST_URI": path,
        "PATH_INFO": path,
        "SCRIPT_NAME": "/cgi-bin/gahk.cgi",
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)),
        "SERVER_NAME": "gahk.dk",
        "SERVER_PORT": "443",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "192.0.2.1",
    }
    for name, value in (headers or {}).items():
        env["HTTP_" + name.upper().replace("-", "_")] = value
    done = subprocess.run(
        [sys.executable, str(stub)], input=body, env=env, capture_output=True, timeout=60, check=False
    )
    head, _, content = done.stdout.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    response_headers = dict(line.split(": ", 1) for line in lines)
    return int(response_headers["Status"].split()[0]), response_headers, content


@pytest.fixture
def home(tmp_path: Path, offline_pip: Path) -> Path:
    home = tmp_path / "home"
    (home / "public_html").mkdir(parents=True)
    (tmp_path / "dist").mkdir()
    write_config(home, (tmp_path / "dist" / "manifest.json").as_uri(), KEY)
    (home / "gahk_web" / "bin").mkdir()
    (home / "gahk_web" / "bin" / "updater.py").write_text(UPDATER_SOURCE)
    return home


def signed(body: bytes = b"{}", key: str = KEY) -> dict[str, str]:
    return {**signing.signed_headers(key, "POST", "/api/update", body), "Content-Type": "application/json"}


def test_without_a_release_pages_answer_503(home: Path) -> None:
    status, headers, body = cgi(home, "GET", "/faciliteter")
    assert status == 503
    assert headers["Retry-After"] == "300"
    assert "ved at blive installeret" in body.decode()


def test_an_unsigned_update_is_refused(home: Path) -> None:
    status, _, body = cgi(home, "POST", "/api/update", b"{}")
    assert status == 401
    assert json.loads(body) == {"error": "missing signature"}


def test_an_update_signed_with_another_key_is_refused(home: Path) -> None:
    status, _, body = cgi(home, "POST", "/api/update", b"{}", signed(key="x" * 40))
    assert status == 401
    assert json.loads(body) == {"error": "bad signature"}


def test_update_needs_post(home: Path) -> None:
    assert cgi(home, "GET", "/api/update")[0] == 405


def test_a_signed_update_bootstraps_the_first_release(home: Path, tmp_path: Path) -> None:
    v1 = version(1)
    publish(tmp_path / "dist", make_bundle(tmp_path / "dist", v1))

    status, headers, body = cgi(home, "POST", "/api/update", b"{}", signed())

    assert status == 200, body
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "updated": True,
        "version": v1,
        "available_version": v1,
        "detail": f"installed {v1}",
    }
    assert (home / "gahk_web" / "CURRENT").read_text().strip() == v1


def test_without_any_updater_the_update_says_so(home: Path) -> None:
    shutil.rmtree(home / "gahk_web" / "bin")
    status, _, body = cgi(home, "POST", "/api/update", b"{}", signed())
    assert status == 503
    assert "no updater installed" in json.loads(body)["error"]


def test_without_config_the_update_is_refused(home: Path) -> None:
    (home / "gahk_web" / "config.ini").unlink()
    status, _, body = cgi(home, "POST", "/api/update", b"{}", signed())
    assert status == 401
    assert json.loads(body) == {"error": "signing is not configured"}


def install(home: Path, tmp_path: Path) -> tuple[str, Path]:
    """Install a fake release through the stub; returns its sha and the real stub, put in place
    (the fake release ships a dummy gahk.cgi) beside the site's 503 page."""
    v1 = version(1)
    publish(tmp_path / "dist", make_bundle(tmp_path / "dist", v1))
    status, _, body = cgi(home, "POST", "/api/update", b"{}", signed())
    assert status == 200 and json.loads(body)["updated"], body
    stub = home / "public_html" / "cgi-bin" / "gahk.cgi"
    shutil.copyfile(STUB, stub)
    (home / "public_html" / "503.html").write_text("<h1>Siden opdateres</h1>", encoding="utf-8")
    return v1, stub


def test_the_release_runs_with_only_its_own_src_and_packages(home: Path, tmp_path: Path) -> None:
    v1, stub = install(home, tmp_path)
    release = home / "gahk_web" / "releases" / v1

    status, _, body = cgi(home, "GET", "/faciliteter", stub=stub)

    assert status == 200, body
    seen = json.loads(body)
    assert seen["sys_path"][:2] == [str(release / "src"), str(release / "vendor")]
    assert not [entry for entry in seen["sys_path"] if "site-packages" in entry]
    assert str(stub.parent) not in seen["sys_path"]
    assert Path(seen["gahkfake"]).is_relative_to(release / "vendor")


def test_a_symlink_on_the_way_to_the_stub_keeps_cgi_bin_off_sys_path(home: Path, tmp_path: Path) -> None:
    v1, stub = install(home, tmp_path)
    link = tmp_path / "linked_public_html"
    link.symlink_to(home / "public_html", target_is_directory=True)

    status, _, body = cgi(home, "GET", "/faciliteter", stub=link / "cgi-bin" / "gahk.cgi")

    assert status == 200, body
    seen = json.loads(body)["sys_path"]
    assert seen[:2] == [
        str(home / "gahk_web" / "releases" / v1 / "src"),
        str(home / "gahk_web" / "releases" / v1 / "vendor"),
    ]
    assert not [entry for entry in seen if os.path.realpath(entry) == os.path.realpath(stub.parent)]


def test_packages_for_another_python_are_not_imported_until_an_update_reinstalls_them(
    home: Path, tmp_path: Path
) -> None:
    v1, stub = install(home, tmp_path)
    abi = home / "gahk_web" / "releases" / v1 / "vendor" / ".abi"
    abi.write_text("cpython-399 .cpython-399-elsewhere.so elsewhere\n")

    status, headers, body = cgi(home, "GET", "/api/health", stub=stub)
    assert status == 503
    assert headers["Content-Type"] == "application/json"
    health = json.loads(body)
    assert health["ok"] is False and health["version"] == v1 and health["revision"] is None
    assert "cpython-399" in health["error"] and "POST /api/update reinstalls them" in health["error"]

    status, headers, body = cgi(home, "GET", "/faciliteter", stub=stub)
    assert status == 503
    assert headers["Retry-After"] == "300"
    assert body.decode().startswith("<h1>Siden opdateres</h1>")

    status, _, body = cgi(home, "POST", "/api/update", b"{}", signed(), stub=stub)
    assert status == 200, body
    assert json.loads(body)["detail"] == f"reinstalled packages for {updater.python_tag()} (running {v1})"

    assert cgi(home, "GET", "/faciliteter", stub=stub)[0] == 200


def test_a_release_without_packages_answers_503(home: Path, tmp_path: Path) -> None:
    v1, stub = install(home, tmp_path)
    shutil.rmtree(home / "gahk_web" / "releases" / v1 / "vendor")

    status, _, body = cgi(home, "GET", "/api/health", stub=stub)

    assert status == 503
    assert "not installed" in json.loads(body)["error"]


def test_the_stub_and_the_updater_agree_on_the_python() -> None:
    loader = importlib.machinery.SourceFileLoader("gahk_cgi_stub", str(STUB))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    assert module.interpreter_abi() == updater.interpreter_abi()
