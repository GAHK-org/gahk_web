"""The FastCGI entry point: public_html/cgi-bin/gahk.fcgi puts the active release on sys.path and
calls serve().

Apache's mod_fcgid starts the process on demand (more of them when requests overlap) and hands it
requests over the listening socket on fd 0, one at a time. flup's single-threaded server answers
them with the Flask app, which keeps one database connection for request after request (db.py).

After each response the process checks, with one stat() of each, whether ~/gahk_web/CURRENT or
config.ini changed since it started, and counts its requests and its age. When CURRENT or the
config changed, after MAX_REQUESTS requests or once older than MAX_AGE, it ends: the response has
gone out, and mod_fcgid starts a fresh process (with the new release, or the new config) for the next
request. So the first request after an update may still get the old release's answer; the next one
gets the new release. While idle, the loop wakes every second: it closes a database connection idle
for too long and ends the process once it is too old. SIGTERM (mod_fcgid ending an idle process)
ends it after the current request.

flup is old (its last release is from 2018), but its FastCGI server is small, pure Python and works
on Python 3.13 and 3.14 (checked: it imports and compiles without warnings and needs none of the
modules removed in 3.13). Its error page for an exception that escapes the app imports cgitb when
debugging; Server.error replaces it with a plain 500, the traceback goes to stderr only.
"""

import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flup.server import fcgi_base, fcgi_single

from . import config as config_module
from .app import config_error_app, create_app
from .config import ConfigError, load_config
from .db import Database
from .version import code_version
from .web import StartResponse, WsgiApp

MAX_REQUESTS = 500
MAX_AGE = 3600.0  # seconds
# Overrides for tests (mod_fcgid passes no environment of ours to the process).
MAX_REQUESTS_ENV = "GAHK_WEB_FCGI_MAX_REQUESTS"
MAX_AGE_ENV = "GAHK_WEB_FCGI_MAX_AGE"
SERVER_ERROR = (
    b"Status: 500 Internal Server Error\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Cache-Control: no-store\r\n\r\n" + "Der skete en fejl. Prøv igen om lidt.".encode()
)

log = logging.getLogger("gahk_web")

type Stamp = tuple[int, int, int] | None


def stamp(path: Path) -> Stamp:
    """What one stat() says about a file: enough to see it was replaced or rewritten."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


@dataclass
class RestartPolicy:
    """When the process should end, so that mod_fcgid starts a fresh one."""

    watched: tuple[Path, ...]
    max_requests: int = MAX_REQUESTS
    max_age: float = MAX_AGE
    clock: Callable[[], float] = time.monotonic
    stale: str = ""  # set when the process is known to run the wrong release from the start
    started: float = field(init=False)
    stamps: tuple[Stamp, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()
        self.stamps = tuple(stamp(path) for path in self.watched)

    def after_request(self, served: int) -> str:
        """Why the process should end after this response, or ""."""
        if self.stale:
            return self.stale
        for path, before in zip(self.watched, self.stamps, strict=True):
            if stamp(path) != before:
                return f"{path.name} changed"
        if served >= self.max_requests:
            return f"answered {served} requests"
        return self.when_idle()

    def when_idle(self) -> str:
        age = self.clock() - self.started
        return f"running for {age:.0f} s" if age >= self.max_age else ""


class _Connection(fcgi_base.Connection):  # type: ignore[misc]  # flup has no type hints
    """flup's connection, but a web server that goes away mid-request ends only that request."""

    def run(self) -> None:
        try:
            super().run()
        except OSError as exc:
            log.warning("gahk_web: FastCGI connection lost: %r", exc)
            try:
                self._sock.close()
            except OSError:
                pass


class Server(fcgi_single.WSGIServer):  # type: ignore[misc]  # flup has no type hints
    """flup's single-threaded FastCGI server with the restart policy and the idle housekeeping."""

    def __init__(
        self,
        app: WsgiApp,
        policy: RestartPolicy,
        database: Database | None = None,
        bind_address: str | None = None,
    ) -> None:
        super().__init__(self._counting(app), bindAddress=bind_address, debug=False)
        self._jobClass = _Connection
        self.policy = policy
        self.database = database
        self.served = 0
        self._checked = 0
        self.exit_reason = ""

    def _counting(self, app: WsgiApp) -> WsgiApp:
        def counted(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
            self.served += 1
            return app(environ, start_response)

        return counted

    def _mainloopPeriodic(self) -> None:
        """After each request, and every second while idle."""
        if self.served != self._checked:
            self._checked = self.served
            reason = self.policy.after_request(self.served)
        else:
            reason = self.policy.when_idle()
            if self.database is not None:
                self.database.close_if_idle()
        if reason and not self.exit_reason:
            self.exit_reason = reason
            self._exit()  # flup's: the main loop ends after this pass

    def error(self, req: Any) -> None:  # noqa: ANN401 - flup's request
        """An exception escaped the app before it wrote anything (flup has logged the traceback to
        the request's stderr, Apache's error log): a plain 500, never a traceback or cgitb."""
        req.stdout.write(SERVER_ERROR)


def _limit(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def serve(bind_address: str | None = None) -> str:
    """Answer requests until the process should end; returns why it ended."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.WARNING, format="gahk.fcgi[%(process)d]: %(message)s"
    )
    current = config_module.home_dir() / "gahk_web" / "CURRENT"
    policy = RestartPolicy(
        (current, config_module.config_path()),
        max_requests=int(_limit(MAX_REQUESTS_ENV, MAX_REQUESTS)),
        max_age=_limit(MAX_AGE_ENV, MAX_AGE),
    )
    # The stub read CURRENT before the stamps were taken: an update in between means this process
    # runs the release before it, and must give way after its first request.
    try:
        active = current.read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    if active and code_version() not in ("dev", active):
        policy.stale = f"CURRENT is {active}, this is {code_version()}"
    database: Database | None = None
    app: WsgiApp
    try:
        flask_app = create_app(load_config(), persistent=True, runner="fastcgi")
    except ConfigError as exc:
        app = config_error_app(str(exc))
    else:
        database = flask_app.extensions["gahk_db"]
        app = flask_app
    server = Server(app, policy, database, bind_address)
    try:
        server.run()
    except Exception as exc:  # the process served a while; mod_fcgid starts a new one
        log.exception("gahk_web: the FastCGI server failed")
        return f"the server failed: {exc!r}"
    finally:
        if database is not None:
            database.close()
    return server.exit_reason or "stopped by a signal"
