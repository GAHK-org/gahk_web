"""PostgreSQL through SQLAlchemy 2 (psycopg 3's binary wheel, which brings libpq along).

The webhost's database is a cache and an outbox, never the source of truth: the cached snapshot,
the image index, the list of generated files, and form submissions plus visit counts until intern
pulls them. Losing it costs at most the not-yet-pulled outbox.

The tables are the typed models in models.py, and the code queries them with SQLAlchemy's ORM and
Core statements. The schema is made and changed by the Alembic revisions in gahk_web/migrations;
migrate() applies them, from the updater (`python -m gahk_web migrate`), the devserver and the tests.
A revision runs before its release goes live, while the previous release still serves the site: it
must leave a schema the previous release's code still works with.

Sessions. A piece of work (a request, a command) gets a Session bound to ONE connection, which it
keeps until the work is done. Flask-SQLAlchemy's db.session would hand its connection back to the
pool at every commit, and the advisory locks below belong to the connection: they would leave with
it. Code commits after each write, as the autocommit of the hand-written layer before did, so no
write waits in an open transaction while a request renders or makes image variants.

Connections. In the persistent FastCGI process the web app's engine (made by Flask-SQLAlchemy from
engine_options()) keeps one connection for request after request: it is pinged before each request
gets it and replaced when it no longer answers, never older than RECYCLE, and closed once idle for
IDLE_CLOSE (Database.close_if_idle, from the FastCGI loop), so an idle process holds no slot of
Hetzner's PostgreSQL. libpq's keepalives and tcp_user_timeout notice a connection the network dropped
silently. Whenever the connection goes back to the pool, an open transaction is rolled back and every
advisory lock still held is released (pg_advisory_unlock_all), so a failed request can never leave a
lock behind for the process's later requests. Under CGI, on the command line and in the tests
(NullPool) each piece of work opens its own connection and closes it at the end.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .config import DatabaseConfig

if TYPE_CHECKING:
    from alembic.config import Config as AlembicConfig

# The driver; everything else comes from conninfo(), in connect_args.
URL = "postgresql+psycopg://"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"

# Advisory lock keys (pg_advisory_lock takes a bigint). "gahk" in hex, then a purpose.
LOCK_MIGRATE = 0x6761686B_0001
LOCK_RENDER = 0x6761686B_0002
LOCK_VARIANTS = 0x6761686B_0003  # housekeeping's making of image variants

CONNECT_TIMEOUT = 10
# The persistent process closes its connection once no request used it for IDLE_CLOSE seconds:
# Hetzner's PostgreSQL is across the network and may drop idle connections, and every idle FastCGI
# process would otherwise hold one of its slots. No connection is used for longer than RECYCLE.
IDLE_CLOSE = 120.0
RECYCLE = 600
# A migration waits at most this long for a table a request is using (requests queue behind a
# waiting migration); then it fails, the old release keeps running and the next update tries again.
MIGRATION_LOCK_TIMEOUT = "10s"


def conninfo(cfg: DatabaseConfig) -> dict[str, str | int]:
    """libpq connection parameters for config.ini's [database] section.

    sslmode "require" encrypts without checking the server's certificate (libpq's meaning, so a
    configured sslrootcert is not passed on: libpq would verify against it); "verify-full" checks it
    against sslrootcert, else against the system's certificate authorities; "disable" is for a
    local development database only."""
    params: dict[str, str | int] = {
        "host": cfg.host,
        "port": cfg.port,
        "dbname": cfg.name,
        "user": cfg.user,
        "application_name": "gahk_web",
        "connect_timeout": CONNECT_TIMEOUT,
        "sslmode": cfg.sslmode,
        "gssencmode": "disable",  # no Kerberos here; skips a round trip
        "options": "-c TimeZone=UTC",
        # A persistent process keeps its connection between requests. Should the network drop it
        # silently, keepalives find out while it is idle, and a request writing to it gives up
        # after tcp_user_timeout instead of waiting for TCP's own retries (minutes).
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 15000,
    }
    if cfg.password:
        params["password"] = cfg.password
    if cfg.sslmode == "verify-full":
        params["sslrootcert"] = cfg.sslrootcert or _system_cafile()
    return params


def _system_cafile() -> str:
    """The CA bundle Python's ssl module trusts. libpq's own "system" means the OpenSSL compiled
    into the psycopg wheel, whose default folders need not exist on this host."""
    import ssl

    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and Path(candidate).is_file():
            return candidate
    return "system"


def engine_options(cfg: DatabaseConfig, *, persistent: bool) -> dict[str, Any]:
    """create_engine()'s options (Flask-SQLAlchemy's SQLALCHEMY_ENGINE_OPTIONS). persistent: one
    pooled connection for the FastCGI process (single-threaded, one request at a time); otherwise
    none is kept (CGI, commands, tests)."""
    options: dict[str, Any] = {"connect_args": conninfo(cfg)}
    if persistent:
        options.update(
            pool_size=1,
            max_overflow=0,
            # Only a bug could ask for a second connection in this single-threaded process: fail
            # soon rather than wait the default 30 s for the one in use.
            pool_timeout=5,
            pool_pre_ping=True,
            pool_recycle=RECYCLE,
        )
    else:
        options["poolclass"] = NullPool
    return options


def make_engine(cfg: DatabaseConfig) -> Engine:
    """An engine that keeps no connection, for the command line and the tests."""
    return create_engine(URL, **engine_options(cfg, persistent=False))


def release_locks_on_return(engine: Engine) -> None:
    """Whenever a pooled connection goes back to the pool: roll back what is open and release every
    advisory lock it still holds, so nothing a request left carries over to the next one."""

    def reset(dbapi_connection: Any, record: Any, state: Any) -> None:  # noqa: ANN401 - DBAPI objects
        dbapi_connection.rollback()  # a failed transaction would refuse the next statement
        if state.terminate_only:
            return  # being closed: the server releases the locks itself
        # One round trip, outside a transaction. Should it fail, the pool discards the connection.
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock_all()")
        finally:
            dbapi_connection.autocommit = False

    event.listen(engine, "reset", reset)


class Database:
    """The web app's database: its engine, and a Session (on a connection of its own) per request.

    persistent=True is the FastCGI process: the pooled connection is reused from request to request
    (see the module's docstring). Otherwise each request's connection is closed at its end."""

    def __init__(
        self, engine: Engine, *, persistent: bool, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.engine = engine
        self.persistent = persistent
        self.clock = clock
        self.connects = 0  # connections this process opened (tests, diagnostics)
        self.open_connections = 0
        self._last_used = clock()
        if persistent:
            release_locks_on_return(engine)
        event.listen(engine, "connect", self._opened)
        event.listen(engine, "close", self._closed)
        event.listen(engine, "close_detached", self._closed_detached)

    def _opened(self, dbapi_connection: Any, record: Any) -> None:  # noqa: ANN401 - DBAPI objects
        self.connects += 1
        self.open_connections += 1

    def _closed(self, dbapi_connection: Any, record: Any) -> None:  # noqa: ANN401 - DBAPI objects
        self.open_connections -= 1

    def _closed_detached(self, dbapi_connection: Any) -> None:  # noqa: ANN401 - DBAPI object
        self.open_connections -= 1

    def open_session(self) -> Session:
        """A Session on a connection of its own; end it with close_session()."""
        return Session(bind=self.engine.connect())

    def close_session(self, session: Session) -> None:
        """Roll back whatever the session left open and give its connection back: closed, or (when
        persistent) to the pool, which releases its advisory locks."""
        connection = session.bind
        try:
            session.close()
        finally:
            if isinstance(connection, Connection):
                connection.close()
            self._last_used = self.clock()

    def close_if_idle(self) -> None:
        """Close the pooled connection when no request used it for IDLE_CLOSE seconds."""
        if self.open_connections and self.clock() - self._last_used > IDLE_CLOSE:
            self.close()

    def close(self) -> None:
        self.engine.dispose()


@contextmanager
def session_for(engine: Engine) -> Iterator[Session]:
    """A Session on a connection of its own, for a piece of work outside a request (a command)."""
    connection = engine.connect()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        connection.close()


# --- advisory locks (session-level: they belong to the connection, not to a transaction) ---


@contextmanager
def advisory_lock(session: Session, key: int) -> Iterator[None]:
    """Waits for the lock. Released on exit, also when the work under it fails."""
    session.execute(select(func.pg_advisory_lock(key)))
    try:
        yield
    except BaseException:
        _unlock_after_failure(session, key)
        raise
    session.execute(select(func.pg_advisory_unlock(key)))


@contextmanager
def try_advisory_lock(session: Session, key: int) -> Iterator[bool]:
    """Yields whether the lock was taken; never waits."""
    got = bool(session.scalar(select(func.pg_try_advisory_lock(key))))
    try:
        yield got
    except BaseException:
        if got:
            _unlock_after_failure(session, key)
        raise
    if got:
        session.execute(select(func.pg_advisory_unlock(key)))


def _unlock_after_failure(session: Session, key: int) -> None:
    """The work under the lock failed, and its transaction may refuse further statements: roll it
    back first. Should even that fail (the connection broke), the pool's reset releases the lock, or
    the server does when the connection ends."""
    with suppress(Exception):
        session.rollback()
        session.execute(select(func.pg_advisory_unlock(key)))


# --- migrations ---


def alembic_config() -> "AlembicConfig":
    """Alembic's Config for the migrations inside this package, found by its path (never by the
    current directory)."""
    from alembic.config import Config

    config = Config(MIGRATIONS / "alembic.ini")
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def migrate(engine: Engine) -> list[str]:
    """Apply pending migrations; returns the revisions applied. Safe to run concurrently and repeatedly."""
    from alembic import command

    applied: list[str] = []
    with engine.connect() as connection:
        connection.execute(select(func.set_config("lock_timeout", MIGRATION_LOCK_TIMEOUT, False)))
        connection.execute(select(func.pg_advisory_lock(LOCK_MIGRATE)))
        connection.commit()  # Alembic runs each revision in a transaction of its own
        try:
            config = alembic_config()
            config.attributes.update(connection=connection, applied=applied)
            command.upgrade(config, "head")
        finally:
            connection.rollback()
            connection.execute(select(func.pg_advisory_unlock(LOCK_MIGRATE)))
            connection.commit()
    return applied
