import io
import os
import subprocess
import sys
from datetime import timedelta
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from conftest import SYNC_KEY, Client, page, publish, snapshot, upload_images

from gahk_web import db, kalender, render
from gahk_web.clock import copenhagen_today
from gahk_web.config import Config
from gahk_web.snapshot import validate

REPO = Path(__file__).resolve().parents[1]


# --- preview ---


def test_preview_renders_an_unsaved_page_in_the_current_site(client: Client, cfg: Config) -> None:
    publish(client)
    item = page(3, "faciliteter/kokken", "Nyt <køkken>", '<p><img src="/media/cms/2026/11/k.jpg"></p>')
    item["background"] = "/media/cms/2026/11/bg.jpg"
    result = client.api(
        "POST", "/api/preview", {"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"}
    )
    assert result.status == 200
    html = result.text
    assert "Nyt &lt;køkken&gt;" in html
    assert 'src="https://intern.gahk.dk/media/cms/2026/11/k.jpg"' in html
    assert "url('https://intern.gahk.dk/media/cms/2026/11/bg.jpg')" in html
    assert '<base href="https://gahk.dk/">' in html
    assert '<a href="/kontakt">Kontakt</a>' in html  # the current menu
    assert 'class="section-nav"' in html
    assert "sendBeacon" not in html
    assert "noindex" in html
    # Never stored: the published page is unchanged.
    assert "Nyt" not in (cfg.public_html / "faciliteter/kokken/index.html").read_text()


def test_preview_news_and_event(client: Client) -> None:
    publish(client)
    news = {
        "id": 99,
        "title": "Forhåndsvist",
        "body": "<p>x</p>",
        "published_at": "2026-11-12T10:00:00+01:00",
    }
    result = client.api(
        "POST", "/api/preview", {"kind": "news", "item": news, "media_base": "https://intern.gahk.dk"}
    )
    assert result.status == 200
    assert "Forhåndsvist" in result.text
    event = {
        "id": 1,
        "title": "Ny dato",
        "description": "",
        "starts_on": (copenhagen_today() + timedelta(days=3)).isoformat(),
    }
    result = client.api(
        "POST", "/api/preview", {"kind": "event", "item": event, "media_base": "https://intern.gahk.dk"}
    )
    assert "Ny dato" in result.text
    assert "Julefrokost" not in result.text  # replaced by the edited version with the same id


def test_preview_works_before_the_first_publish(client: Client) -> None:
    item = page(1, "", "Velkommen", "<p>Hej</p>")
    result = client.api(
        "POST", "/api/preview", {"kind": "page", "item": item, "media_base": "https://intern.gahk.dk"}
    )
    assert result.status == 200
    assert "Velkommen til GAHK" in result.text


def test_preview_is_validated(client: Client) -> None:
    result = client.api(
        "POST", "/api/preview", {"kind": "page", "item": {"id": 1}, "media_base": "https://intern.gahk.dk"}
    )
    assert result.status == 400


# --- calendar pass-through ---


class Upstream(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


def test_calendar_is_streamed_from_intern(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float) -> Upstream:
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Upstream(
            b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            headers={"Content-Type": "text/calendar; charset=utf-8", "Cache-Control": "private, no-store"},
        )

    monkeypatch.setattr(kalender, "HTTPResponse", Upstream)
    monkeypatch.setattr(kalender, "urlopen", fake_urlopen)
    result = client.get("/kalender/abc_DEF-123.ics")
    assert result.status == 200
    assert result.body.startswith(b"BEGIN:VCALENDAR")
    assert result.headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert result.headers["Cache-Control"] == "private, no-store"
    assert seen == {"url": "https://intern.gahk.dk/kalender/abc_DEF-123.ics", "timeout": 20}


def test_calendar_errors_pass_through(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(request: Request, timeout: float) -> Upstream:
        headers = Message()
        headers["Content-Type"] = "text/plain"
        raise HTTPError(request.full_url, 404, "Not Found", headers, io.BytesIO(b"nope"))

    monkeypatch.setattr(kalender, "urlopen", gone)
    result = client.get("/kalender/old-token.ics")
    assert result.status == 404
    assert result.headers["Cache-Control"] == "private, no-store"

    def down(request: Request, timeout: float) -> Upstream:
        raise URLError("unreachable")

    monkeypatch.setattr(kalender, "urlopen", down)
    assert client.get("/kalender/abc.ics").status == 502
    assert client.get("/kalender/../x.ics").status in (404, 503)  # not a feed address


# --- housekeeping: the day changes, nobody runs cron ---


def test_lazy_rebuild_when_the_day_changed(client: Client, cfg: Config, con: db.Connection) -> None:
    today = copenhagen_today()
    yesterday = today - timedelta(days=1)
    data = snapshot()
    data["events"] = [{"id": 9, "title": "Igaar-fest", "description": "", "starts_on": yesterday.isoformat()}]
    upload_images(client)
    with db.advisory_lock(con, db.LOCK_RENDER):
        from gahk_web import images

        snap = validate(data)
        images.materialize(con, cfg, snap)
        render.render_site(con, cfg, snap, yesterday, store_snapshot=True)
    con.run(
        "INSERT INTO visit_seen (day, path, visitor) VALUES (:d, '*', 'old')", d=today - timedelta(days=3)
    )
    assert "Igaar-fest" in (cfg.public_html / "index.html").read_text()

    client.post("/s", b"/", headers={"User-Agent": "Mozilla/5.0"})  # any request that reaches the app

    assert "Igaar-fest" not in (cfg.public_html / "index.html").read_text()
    state = db.query_one(con, "SELECT rendered_on FROM site_state")
    assert state is not None and state["rendered_on"] == today
    assert db.query(con, "SELECT * FROM visit_seen WHERE visitor = 'old'") == []


def test_no_rebuild_on_the_same_day(client: Client, cfg: Config) -> None:
    publish(client)
    (cfg.public_html / "kontakt/index.html").write_text("edited by hand")
    client.get("/findes-ikke")
    assert (cfg.public_html / "kontakt/index.html").read_text() == "edited by hand"


# --- the CGI stub, end to end ---


def run_cgi(home: Path, environ: dict[str, str]) -> str:
    env = {"PATH": os.environ.get("PATH", ""), "GAHK_WEB_HOME": str(home), **environ}
    stub = REPO / "public_html" / "cgi-bin" / "gahk.cgi"
    done = subprocess.run([sys.executable, str(stub)], env=env, capture_output=True, check=True, timeout=60)
    return done.stdout.decode()


def test_cgi_stub_without_a_release_answers_503(tmp_path: Path) -> None:
    (tmp_path / "gahk_web").mkdir()
    output = run_cgi(tmp_path, {"REQUEST_METHOD": "GET", "PATH_INFO": "/"})
    assert output.startswith("Status: 503")


def test_cgi_stub_runs_the_current_release(tmp_path: Path, cfg: Config, con: db.Connection) -> None:
    release = tmp_path / "gahk_web" / "releases" / "abc1234"
    release.mkdir(parents=True)
    (release / "src").symlink_to(REPO / "src")
    (tmp_path / "gahk_web" / "CURRENT").write_text("abc1234\n")
    database = cfg.database
    (tmp_path / "gahk_web" / "config.ini").write_text(
        f"[database]\nhost = {database.host}\nport = {database.port}\nname = {database.name}\n"
        f"user = {database.user}\npassword = {database.password}\n"
        f"[sync]\nkey = {SYNC_KEY}\n[stats]\nsecret = {'s' * 40}\n"
        f"[paths]\npublic_html = {cfg.public_html}\nstate_dir = {cfg.state_dir}\n"
    )
    output = run_cgi(
        tmp_path, {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/health", "SERVER_PROTOCOL": "HTTP/1.1"}
    )
    assert output.startswith("Status: 200 OK")
    assert '"ok": true' in output
    output = run_cgi(
        tmp_path, {"REQUEST_METHOD": "GET", "PATH_INFO": "/intern/x", "REQUEST_URI": "/intern/x?y=1"}
    )
    assert "Status: 308" in output
    assert "Location: https://intern.gahk.dk/x?y=1" in output
