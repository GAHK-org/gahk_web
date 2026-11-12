"""The Flask app's plumbing: the browser's path under the stub, errors that never show a traceback,
and the database connection a persistent process reuses from request to request."""

from collections.abc import Callable

import pytest
from conftest import Client, publish
from flask import Flask

from gahk_web import db
from gahk_web.app import create_app
from gahk_web.config import Config
from gahk_web.web import database


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def persistent_app(
    cfg: Config, clock: Callable[[], float] | None = None
) -> tuple[Flask, db.SharedConnection]:
    app = create_app(cfg, persistent=True, runner="fastcgi")
    shared = db.SharedConnection(cfg.database, keep=True, clock=clock or FakeClock())
    app.extensions["gahk_db"] = shared
    return app, shared


def backend_pid(shared: db.SharedConnection) -> int:
    assert shared.connection is not None
    row = db.query_one(shared.connection, "SELECT pg_backend_pid() AS pid")
    assert row is not None
    return int(row["pid"])


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


def test_an_exception_answers_500_without_a_traceback(cfg: Config, con: db.Connection) -> None:
    app = create_app(cfg)

    def broken() -> str:
        raise RuntimeError("secret detail")

    app.add_url_rule("/test/broken", "broken", broken)
    result = Client(app).get("/test/broken")
    assert result.status == 500
    assert result.text == "Der skete en fejl. Prøv igen om lidt."
    assert result.headers["Cache-Control"] == "no-store"


def test_an_unreachable_database_is_503(cfg: Config, con: db.Connection) -> None:
    from dataclasses import replace

    app = create_app(replace(cfg, database=replace(cfg.database, port=1)))
    result = Client(app).get("/findes-ikke")
    assert (result.status, result.headers["Retry-After"]) == (503, "300")
    result = Client(app).api("GET", "/api/status")
    assert (result.status, result.body) == (503, b'{"error": "database unavailable"}')
    result = Client(app).get("/api/health")
    assert result.status == 503 and result.json()["ok"] is False


# --- the database connection ---


def test_a_cgi_request_closes_its_connection(client: Client) -> None:
    publish(client)
    shared: db.SharedConnection = client.app.extensions["gahk_db"]
    assert client.get("/findes-ikke").status == 404
    assert shared.connection is None


def test_a_persistent_process_reuses_one_connection(cfg: Config, con: db.Connection) -> None:
    app, shared = persistent_app(cfg)
    client = Client(app)
    publish(client)
    pid = backend_pid(shared)
    for _ in range(3):
        assert client.get("/findes-ikke").status == 404
    assert shared.connects == 1
    assert backend_pid(shared) == pid
    health = client.get("/api/health")
    assert health.headers["X-Gahk-Runner"] == "fastcgi"


def test_a_dropped_connection_is_replaced(cfg: Config, con: db.Connection) -> None:
    clock = FakeClock()
    app, shared = persistent_app(cfg, clock)
    client = Client(app)
    publish(client)
    db.execute(con, "SELECT pg_terminate_backend(:pid)", pid=backend_pid(shared))

    # Idle for a while: the connection is asked first, and replaced when it does not answer.
    clock.now += db.PING_AFTER + 1
    assert client.get("/findes-ikke").status == 404
    assert shared.connects == 2

    # Dropped between two quick requests: that request fails, the next gets a new connection.
    db.execute(con, "SELECT pg_terminate_backend(:pid)", pid=backend_pid(shared))
    assert client.get("/findes-ikke").status == 500
    assert client.get("/findes-ikke").status == 404
    assert shared.connects == 3


def test_an_idle_connection_is_closed_and_reopened(cfg: Config, con: db.Connection) -> None:
    clock = FakeClock()
    app, shared = persistent_app(cfg, clock)
    client = Client(app)
    publish(client)
    shared.close_if_idle()
    assert shared.connection is not None
    clock.now += db.IDLE_CLOSE + 1
    shared.close_if_idle()
    assert shared.connection is None
    assert client.get("/findes-ikke").status == 404
    assert shared.connects == 2


def test_a_transaction_left_open_is_rolled_back(cfg: Config, con: db.Connection) -> None:
    app, shared = persistent_app(cfg)

    def leaky() -> str:
        connection = database()
        connection.execute("BEGIN")
        connection.execute("INSERT INTO form_rate_limit (client, at) VALUES ('leak', now())")
        return "done"

    app.add_url_rule("/test/leaky", "leaky", leaky)
    client = Client(app)
    assert client.get("/test/leaky").status == 200
    assert shared.connection is not None
    from psycopg import pq

    assert shared.connection.info.transaction_status == pq.TransactionStatus.IDLE
    assert db.query(con, "SELECT 1 FROM form_rate_limit WHERE client = 'leak'") == []


def test_a_failed_request_leaves_no_advisory_lock_behind(cfg: Config, con: db.Connection) -> None:
    app, shared = persistent_app(cfg)

    def crashes_holding_a_lock() -> str:
        db.execute(database(), "SELECT pg_advisory_lock(:key)", key=db.LOCK_RENDER)
        raise RuntimeError("crash while holding the render lock")

    app.add_url_rule("/test/crash", "crash", crashes_holding_a_lock)
    assert Client(app).get("/test/crash").status == 500
    assert shared.connection is not None  # still open for the next request
    with db.try_advisory_lock(con, db.LOCK_RENDER) as got:
        assert got


@pytest.mark.parametrize("persistent", [True, False])
def test_every_request_ends_without_an_open_transaction(
    cfg: Config, con: db.Connection, persistent: bool
) -> None:
    app = create_app(cfg, persistent=persistent)
    client = Client(app)
    publish(client)
    for path in ("/findes-ikke", "/gammel-side", "/faciliteter/"):
        client.get(path)
        shared: db.SharedConnection = app.extensions["gahk_db"]
        if persistent:
            assert shared.connection is not None
            from psycopg import pq

            assert shared.connection.info.transaction_status == pq.TransactionStatus.IDLE
        else:
            assert shared.connection is None
