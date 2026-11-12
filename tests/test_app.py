"""The Flask app's plumbing: the browser's path under the stub, errors that never show a traceback,
and the database connection a persistent process reuses from request to request."""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from conftest import Client, execute, publish, query
from flask import Flask
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from gahk_web import db
from gahk_web.app import create_app
from gahk_web.config import Config
from gahk_web.web import database


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def backend_pid() -> str:
    return str(database().scalar(select(func.pg_backend_pid())))


type PersistentApp = Callable[..., tuple[Flask, db.Database]]


@pytest.fixture
def persistent_app(cfg: Config) -> Iterator[PersistentApp]:
    """Makes the app as the FastCGI process runs it, with a route that tells its connection's
    backend; its pooled connection is closed after the test (with any lock a failing test leaked)."""
    made: list[db.Database] = []

    def make(clock: Callable[[], float] | None = None) -> tuple[Flask, db.Database]:
        app = create_app(cfg, persistent=True, runner="fastcgi")
        app.add_url_rule("/test/pid", "pid", backend_pid)
        process: db.Database = app.extensions["gahk_db"]
        process.clock = clock or FakeClock()
        made.append(process)
        return app, process

    yield make
    for process in made:
        process.close()


def lock_is_free(con: Session, key: int = db.LOCK_RENDER) -> bool:
    with db.try_advisory_lock(con, key) as got:
        return got


def transactions_left_open(con: Session) -> list[dict[str, Any]]:
    """The app's connections (not this test's own) that sit in a transaction."""
    return query(
        con,
        "SELECT pid, state FROM pg_stat_activity WHERE application_name = 'gahk_web'"
        " AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'",
    )


# --- the request as the browser sent it ---


def test_the_app_routes_on_the_browsers_path_not_the_stubs(client: Client) -> None:
    publish(client)
    stub = {"SCRIPT_NAME": "/cgi-bin/gahk.fcgi"}
    result = client.request("GET", "/faciliteter/", headers={}, query="x=1")
    assert (result.status, result.headers["Location"]) == (301, "/faciliteter?x=1")
    # Apache: PATH_INFO is the rewritten path behind the stub, REQUEST_URI the original request.
    from werkzeug.test import Client as WerkzeugClient

    response = WerkzeugClient(client.app).get(
        "/faciliteter/",
        environ_overrides={**stub, "REQUEST_URI": "/faciliteter/?x=1", "QUERY_STRING": "x=1"},
    )
    assert (response.status_code, response.headers["Location"]) == (301, "/faciliteter?x=1")
    response = WerkzeugClient(client.app).get(
        "/gammel-side", environ_overrides={**stub, "REQUEST_URI": "/gammel-side"}
    )
    assert response.headers["Location"] == "/kontakt"  # never /cgi-bin/gahk.fcgi/kontakt


def test_utf8_paths_are_decoded_once(client: Client) -> None:
    publish(client)
    result = client.get("/k%C3%B8kken")  # percent-encoded by the browser
    assert result.status == 404
    from werkzeug.test import Client as WerkzeugClient

    response = WerkzeugClient(client.app).get(
        "/køkken", environ_overrides={"REQUEST_URI": "/k%C3%B8kken", "SCRIPT_NAME": "/cgi-bin/gahk.fcgi"}
    )
    assert response.status_code == 404
    assert "Siden findes ikke" in response.get_data(as_text=True)


def test_options_and_odd_methods_get_405_not_an_automatic_answer(client: Client) -> None:
    publish(client)
    result = client.request("OPTIONS", "/kontakt")
    assert (result.status, result.headers["Allow"]) == (405, "GET, HEAD")
    result = client.request("PROPFIND", "/kontakt")
    assert (result.status, result.headers["Allow"]) == (405, "GET, HEAD")
    result = client.request("PROPFIND", "/api/status")
    assert (result.status, result.json()) == (405, {"error": "method not allowed"})


def test_head_answers_like_get_without_a_body(client: Client) -> None:
    publish(client)
    get, head = client.get("/findes-ikke"), client.request("HEAD", "/findes-ikke")
    assert (head.status, head.body) == (404, b"")
    assert head.headers["Content-Length"] == get.headers["Content-Length"] != "0"


def test_responses_are_not_cached_unless_they_say_so(client: Client) -> None:
    publish(client)
    assert client.get("/intern/").headers["Cache-Control"] == "no-store"
    assert client.get("/findes-ikke").headers["Cache-Control"] == "public, max-age=60"


# --- errors ---


def test_an_exception_answers_500_without_a_traceback(cfg: Config, con: Session) -> None:
    app = create_app(cfg)

    def broken() -> str:
        raise RuntimeError("secret detail")

    app.add_url_rule("/test/broken", "broken", broken)
    result = Client(app).get("/test/broken")
    assert result.status == 500
    assert result.text == "Der skete en fejl. Prøv igen om lidt."
    assert result.headers["Cache-Control"] == "no-store"


def test_an_unreachable_database_is_503(cfg: Config, con: Session) -> None:
    from dataclasses import replace

    app = create_app(replace(cfg, database=replace(cfg.database, port=1)))
    result = Client(app).get("/findes-ikke")
    assert (result.status, result.headers["Retry-After"]) == (503, "300")
    result = Client(app).api("GET", "/api/status")
    assert (result.status, result.body) == (503, b'{"error": "database unavailable"}')
    result = Client(app).get("/api/health")
    assert result.status == 503 and result.json()["ok"] is False


def test_a_visit_beacon_without_a_database_logs_one_line(
    cfg: Config, con: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Every page a visitor opens sends a beacon: while the database is down, each one used to write
    a whole traceback to Apache's error log (found in the full-system e2e)."""
    from dataclasses import replace

    app = create_app(replace(cfg, database=replace(cfg.database, port=1)))
    browser = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1"}
    with caplog.at_level(logging.ERROR, logger="gahk_web"):
        assert Client(app).post("/s", b"/faciliteter", headers=browser).status == 204
    records = [record for record in caplog.records if record.name == "gahk_web"]
    assert [record.getMessage().split(":")[0] for record in records] == ["gahk_web"]
    assert "database unavailable, a page view is lost" in records[0].getMessage()
    assert records[0].exc_info is None


# --- the database connection ---


def test_a_cgi_request_closes_its_connection(client: Client) -> None:
    publish(client)
    process: db.Database = client.app.extensions["gahk_db"]
    assert client.get("/findes-ikke").status == 404
    assert process.connects > 0
    assert process.open_connections == 0


def test_a_persistent_process_reuses_one_connection(con: Session, persistent_app: PersistentApp) -> None:
    app, process = persistent_app()
    client = Client(app)
    publish(client)
    pid = client.get("/test/pid").text
    for _ in range(3):
        assert client.get("/findes-ikke").status == 404
    assert client.get("/test/pid").text == pid
    assert (process.connects, process.open_connections) == (1, 1)
    health = client.get("/api/health")
    assert health.headers["X-Gahk-Runner"] == "fastcgi"


def test_a_dropped_connection_is_replaced(con: Session, persistent_app: PersistentApp) -> None:
    app, process = persistent_app()
    client = Client(app)
    publish(client)
    # Each request's connection is pinged first, and replaced when it does not answer: whether it
    # was dropped while the process was idle or between two quick requests.
    for connects in (2, 3):
        execute(con, "SELECT pg_terminate_backend(:pid)", pid=int(client.get("/test/pid").text))
        assert client.get("/findes-ikke").status == 404
        assert (process.connects, process.open_connections) == (connects, 1)


def test_an_idle_connection_is_closed_and_reopened(con: Session, persistent_app: PersistentApp) -> None:
    clock = FakeClock()
    app, process = persistent_app(clock)
    client = Client(app)
    publish(client)
    process.close_if_idle()
    assert process.open_connections == 1
    clock.now += db.IDLE_CLOSE + 1
    process.close_if_idle()
    assert process.open_connections == 0
    assert client.get("/findes-ikke").status == 404
    assert (process.connects, process.open_connections) == (2, 1)


def test_a_transaction_left_open_is_rolled_back(con: Session, persistent_app: PersistentApp) -> None:
    app, _process = persistent_app()

    def leaky() -> str:
        database().execute(text("INSERT INTO form_rate_limit (client, at) VALUES ('leak', now())"))
        return "done"  # and no commit

    app.add_url_rule("/test/leaky", "leaky", leaky)
    client = Client(app)
    assert client.get("/test/leaky").status == 200
    assert transactions_left_open(con) == []
    assert query(con, "SELECT 1 FROM form_rate_limit WHERE client = 'leak'") == []


def test_a_failed_request_leaves_no_advisory_lock_behind(con: Session, persistent_app: PersistentApp) -> None:
    app, process = persistent_app()

    def crashes_holding_a_lock() -> str:
        database().execute(select(func.pg_advisory_lock(db.LOCK_RENDER)))
        raise RuntimeError("crash while holding the render lock")

    def fails_in_a_transaction_under_the_lock() -> str:
        with db.advisory_lock(database(), db.LOCK_RENDER):
            database().execute(text("SELECT 1/0"))  # the transaction now refuses every statement
        return "unreachable"

    def forgets_its_lock() -> str:
        database().execute(select(func.pg_advisory_lock(db.LOCK_RENDER)))
        return "done"

    app.add_url_rule("/test/crash", "crash", crashes_holding_a_lock)
    app.add_url_rule("/test/fails", "fails", fails_in_a_transaction_under_the_lock)
    app.add_url_rule("/test/forgets", "forgets", forgets_its_lock)
    client = Client(app)
    pid = client.get("/test/pid").text
    for path, status in (("/test/crash", 500), ("/test/fails", 500), ("/test/forgets", 200)):
        assert client.get(path).status == status
        assert lock_is_free(con), path
    # The same connection all along: the pool released the locks, it did not close the connection.
    assert client.get("/test/pid").text == pid
    assert (process.connects, process.open_connections) == (1, 1)


def test_the_pool_releases_advisory_locks_whatever_returns_the_connection(cfg: Config, con: Session) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(db.URL, **db.engine_options(cfg.database, persistent=True))
    db.release_locks_on_return(engine)
    try:
        with engine.connect() as connection:
            connection.execute(select(func.pg_advisory_lock(db.LOCK_VARIANTS)))
            assert not lock_is_free(con, db.LOCK_VARIANTS)
        assert lock_is_free(con, db.LOCK_VARIANTS)
        assert engine.pool.checkedin() == 1  # kept for the next request, without the lock
    finally:
        engine.dispose()


def test_the_lock_helpers_release_their_lock_when_the_work_fails(engine: Engine, con: Session) -> None:
    with db.session_for(engine) as session:
        with pytest.raises(ZeroDivisionError):
            with db.advisory_lock(session, db.LOCK_RENDER):
                assert not lock_is_free(con)
                raise ZeroDivisionError
        assert lock_is_free(con)
        with pytest.raises(Exception, match="division by zero"):
            with db.try_advisory_lock(session, db.LOCK_RENDER) as got:
                assert got
                session.execute(text("SELECT 1/0"))
        assert lock_is_free(con)
        with db.try_advisory_lock(session, db.LOCK_RENDER) as got:
            assert got
            assert not lock_is_free(con)
        assert lock_is_free(con)


@pytest.mark.parametrize("persistent", [True, False])
def test_every_request_ends_without_an_open_transaction(cfg: Config, con: Session, persistent: bool) -> None:
    app = create_app(cfg, persistent=persistent)
    client = Client(app)
    publish(client)
    process: db.Database = app.extensions["gahk_db"]
    for path in ("/findes-ikke", "/gammel-side", "/faciliteter/"):
        client.get(path)
        assert transactions_left_open(con) == []
        assert process.open_connections == (1 if persistent else 0)


def test_cgi_reads_what_the_app_left_of_the_body_and_no_more() -> None:
    """Apache drops a CGI answer when the script ends without reading the whole body; reading past
    CONTENT_LENGTH would wait for a body that never comes."""
    import io

    from gahk_web.cgi_entry import draining

    def app(environ: dict[str, Any], start_response: Callable[..., object]) -> list[bytes]:
        environ["wsgi.input"].read(1000)  # the app takes a little
        start_response("204 No Content", [])
        return []

    stream = io.BytesIO(b"x" * 300_000 + b"not part of the body")
    draining(app)({"wsgi.input": stream, "CONTENT_LENGTH": "300000"}, lambda *_: None)
    assert stream.read() == b"not part of the body"
