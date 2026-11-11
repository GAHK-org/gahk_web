"""The cgi-bin stub: finds the active release, and handles POST /api/update itself (the bootstrap)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from bundles import STUB, UPDATER_SOURCE, make_bundle, publish, version, write_config

from gahk_web import signing

KEY = "k" * 40


def cgi(home: Path, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):  # noqa: ANN201
    """Run the stub as Apache would; returns (status, headers, body)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
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
        [sys.executable, str(STUB)], input=body, env=env, capture_output=True, timeout=60, check=False
    )
    head, _, content = done.stdout.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    response_headers = dict(line.split(": ", 1) for line in lines)
    return int(response_headers["Status"].split()[0]), response_headers, content


@pytest.fixture
def home(tmp_path: Path) -> Path:
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
