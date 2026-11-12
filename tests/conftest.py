"""Shared fixtures: a real PostgreSQL (GAHK_WEB_TEST_DATABASE), a temporary public_html, a WSGI client."""

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from werkzeug.test import Client as WerkzeugClient

from gahk_web import db, signing
from gahk_web.clock import copenhagen_today
from gahk_web.config import Config, DatabaseConfig

DATABASE_URL = os.environ.get("GAHK_WEB_TEST_DATABASE", "postgres://gahk:gahk@localhost:5434/gahk_web_test")
SYNC_KEY = "k" * 40
STATS_SECRET = "s" * 40
TABLES = (
    "applications",
    "visits",
    "visit_seen",
    "form_rate_limit",
    "generated_files",
    "image_files",
    "images",
)


def database_config() -> DatabaseConfig:
    url = urlsplit(DATABASE_URL)
    return DatabaseConfig(
        host=url.hostname or "localhost",
        port=url.port or 5432,
        name=url.path.lstrip("/"),
        user=url.username or "",
        password=url.password or "",
        sslmode="disable",  # the local test PostgreSQL has no TLS
    )


@pytest.fixture(scope="session")
def db_config() -> Iterator[DatabaseConfig]:
    cfg = database_config()
    con = db.connect(cfg)
    db.execute(con, "DROP SCHEMA public CASCADE")
    db.execute(con, "CREATE SCHEMA public")
    db.migrate(con)
    con.close()
    yield cfg


@pytest.fixture
def con(db_config: DatabaseConfig) -> Iterator[db.Connection]:
    connection = db.connect(db_config)
    db.execute(connection, f"TRUNCATE {', '.join(TABLES)}")
    db.execute(
        connection,
        "UPDATE site_state SET snapshot = NULL, revision = NULL, published_at = NULL, received_at = NULL,"
        " rendered_at = NULL, rendered_on = NULL, rendered_version = NULL",
    )
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheels the fake releases of tests/bundles.py need."""
    from bundles import write_wheelhouse

    return write_wheelhouse(tmp_path_factory.mktemp("wheelhouse"))


@pytest.fixture
def offline_pip(wheelhouse: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The updater's pip installs from the wheelhouse instead of PyPI (still checking the hashes)."""
    monkeypatch.setenv("GAHK_WEB_PIP_FIND_LINKS", str(wheelhouse))
    return wheelhouse


@pytest.fixture
def cfg(tmp_path: Path, db_config: DatabaseConfig) -> Config:
    public_html = tmp_path / "public_html"
    public_html.mkdir()
    return Config(
        database=db_config,
        sync_key=SYNC_KEY,
        stats_secret=STATS_SECRET,
        public_html=public_html,
        state_dir=tmp_path / "state",
        base_url="https://gahk.dk",
        intern_url="https://intern.gahk.dk",
    )


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:  # noqa: ANN401 - parsed JSON
        return json.loads(self.body)


@dataclass
class Client:
    app: Any
    key: str = SYNC_KEY
    remote_addr: str = "192.0.2.10"
    default_headers: dict[str, str] = field(default_factory=dict)

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        *,
        signed: bool = False,
        query: str = "",
    ) -> Result:
        all_headers = {**self.default_headers, **(headers or {})}
        if signed:
            all_headers.update(signing.signed_headers(self.key, method, path, body))
        response = WerkzeugClient(self.app).open(
            path,
            method=method,
            data=body,
            headers=all_headers,
            query_string=query,
            environ_base={"REMOTE_ADDR": self.remote_addr},
        )
        return Result(response.status_code, dict(response.headers), response.get_data())

    def get(self, path: str, **kwargs: object) -> Result:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: bytes = b"", **kwargs: object) -> Result:
        return self.request("POST", path, body, **kwargs)

    def api(
        self, method: str, path: str, data: object = None, raw: bytes | None = None, **kwargs: object
    ) -> Result:
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else b"")
        return self.request(method, path, body, signed=True, **kwargs)


@pytest.fixture
def client(cfg: Config, con: db.Connection) -> Client:
    from gahk_web.app import create_app

    return Client(create_app(cfg))


# --- snapshot builders ---


def jpeg(seed: str) -> bytes:
    return b"\xff\xd8\xff\xe0" + hashlib.sha256(seed.encode()).digest() * 4


def image(name: str, data: bytes, content_type: str = "image/jpeg") -> dict[str, Any]:
    sha = hashlib.sha256(data).hexdigest()
    return {
        "url": f"/billeder/{sha[:16]}/{name}",
        "sha256": sha,
        "content_type": content_type,
        "size": len(data),
    }


GANG = jpeg("gang")
HERO = jpeg("hero")
KOKKEN = jpeg("kokken")
IMAGE_BYTES = {hashlib.sha256(data).hexdigest(): data for data in (GANG, HERO, KOKKEN)}


def page(page_id: int, path: str, title: str, body: str = "<p>Tekst</p>", **extra: object) -> dict[str, Any]:
    return {
        "id": page_id,
        "path": path,
        "title": title,
        "body": body,
        "background": "",
        "description": "",
        "updated_at": "2026-11-01T10:00:00+01:00",
        **extra,
    }


def snapshot(today: date | None = None, **overrides: object) -> dict[str, Any]:
    today = today or copenhagen_today()
    gang, hero, kokken = image("gang.jpg", GANG), image("hero.jpg", HERO), image("kokken.jpg", KOKKEN)
    data: dict[str, Any] = {
        "format": 1,
        "revision": "a" * 64,
        "published_at": "2026-11-11T12:00:00+01:00",
        "site": {"front_page_id": 1, "default_background": gang["url"]},
        "legacy_images": {"/static/legacy/image/upload/images/gang.jpg": gang["url"]},
        "pages": [
            page(1, "", "Velkommen", "<p>Forsidetekst</p>", background=hero["url"]),
            page(2, "faciliteter", "Faciliteter", "<p>Om faciliteterne</p>"),
            page(
                3,
                "faciliteter/kokken",
                "Køkkenet",
                f'<p><img src="{kokken["url"]}" alt=""></p>',
                background=kokken["url"],
            ),
            page(4, "kontakt", "Kontakt <os>", "<p>Skriv til os</p>", description="Sådan kontakter du os"),
            page(5, "optagelse", "Optagelse", "<p>Kriterier og husleje</p>"),
        ],
        "news": [
            {
                "id": 1,
                "title": "Gammel nyhed",
                "body": "<p>Før</p>",
                "published_at": "2026-01-01T12:00:00+01:00",
            },
            {
                "id": 2,
                "title": "Ny <nyhed>",
                "body": "<p>Nu</p>",
                "published_at": "2026-11-01T12:00:00+01:00",
            },
        ],
        "events": [
            {
                "id": 1,
                "title": "Julefrokost",
                "description": "<p>Mad</p>",
                "starts_on": (today + timedelta(days=10)).isoformat(),
            },
            {
                "id": 2,
                "title": "Sommerfest",
                "description": "",
                "starts_on": (today - timedelta(days=30)).isoformat(),
            },
            {"id": 3, "title": "Idag-fest", "description": "", "starts_on": today.isoformat()},
        ],
        "menu": [
            {"label": "Hjem", "url": "/"},
            {"label": "Faciliteter", "url": "/faciliteter"},
            {"label": "Begivenheder", "url": "/begivenheder/"},
            {"label": "Optagelse", "url": "/optagelse/"},
            {"label": "Kontakt", "url": "/kontakt"},
        ],
        "redirects": {"gammel-side": "kontakt"},
        "images": [gang, hero, kokken],
    }
    data.update(overrides)
    return data


def upload_images(client: Client, shas: list[str] | None = None) -> None:
    for sha, data in IMAGE_BYTES.items():
        if shas is None or sha in shas:
            result = client.api("PUT", f"/api/images/{sha}", raw=data, headers={"Content-Type": "image/jpeg"})
            assert result.status == 204, result.text


def publish(client: Client, data: dict[str, Any] | None = None) -> Result:
    upload_images(client)
    result = client.api("POST", "/api/publish", data or snapshot())
    assert result.status == 200, result.text
    return result
