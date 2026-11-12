"""PostgreSQL access over psycopg 3 (its binary wheel, which brings libpq along).

The webhost's database is a cache and an outbox, never the source of truth: the cached snapshot,
the image index, the list of generated files, and form submissions plus visit counts until intern
pulls them. Losing it costs at most the not-yet-pulled outbox.

The SQL in this package names its parameters :name, and query()/execute() take them as keyword
arguments; rows come back as dicts. A connection is in autocommit mode: every statement commits on
its own unless it runs inside transaction(). psycopg is imported on the first connect(), so requests
that need no database do not pay for it.

The web app gets its connection from a SharedConnection: in the persistent FastCGI process one
connection serves request after request, checked before use and closed when idle; under CGI (and in
tests) each request gets its own.

The tables are described as typed SQLAlchemy models in models.py, and the schema is made and changed
by the Alembic revisions in gahk_web/migrations; migrate() applies them, from the updater (`python
-m gahk_web migrate`), the devserver and the tests. A revision runs before its release goes live,
while the previous release still serves the site: it must leave a schema the previous release's
code still works with.
"""

import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.pool import NullPool

from .config import DatabaseConfig

if TYPE_CHECKING:
    import psycopg
    from alembic.config import Config as AlembicConfig

type Row = dict[str, Any]
type Connection = psycopg.Connection[Row]

# SQLAlchemy's driver; everything else comes from conninfo(), in connect_args.
URL = "postgresql+psycopg://"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"

# Advisory lock keys (pg_advisory_lock takes a bigint). "gahk" in hex, then a purpose.
LOCK_MIGRATE = 0x6761686B_0001
LOCK_RENDER = 0x6761686B_0002
LOCK_VARIANTS = 0x6761686B_0003  # housekeeping's making of image variants

CONNECT_TIMEOUT = 10
# A reused connection that has been idle this long is asked "SELECT 1" before a request gets it, and
# one idle longer than IDLE_CLOSE is closed: Hetzner's PostgreSQL is across the network and may drop
# idle connections, and every idle FastCGI process would otherwise hold one of its slots.
PING_AFTER = 20.0
IDLE_CLOSE = 120.0
# A migration waits at most this long for a table a request is using (requests queue behind a
# waiting migration); then it fails, the old release keeps running and the next update tries again.
MIGRATION_LOCK_TIMEOUT = "10s"

log = logging.getLogger(__name__)

# :name outside a ::cast, and every % (which psycopg's own placeholders would otherwise claim).
_PARAM_RE = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)|%")


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


def connect(cfg: DatabaseConfig) -> Connection:
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    return psycopg.connect(make_conninfo("", **conninfo(cfg)), autocommit=True, row_factory=dict_row)


class SharedConnection:
    """The web app's database connection: opened on first use, then (keep=True) reused by the
    process's later requests, one at a time.

    Before a request gets a reused connection it is checked: a closed or broken one, or one idle for
    more than IDLE_CLOSE, is replaced; after PING_AFTER idle seconds it must answer SELECT 1 first.
    After the request, a transaction it left open is rolled back, and after a failed request its
    advisory locks are released too, so nothing carries over to the next request. With keep=False
    (CGI: one request per process; tests) the connection is closed after each request.
    """

    def __init__(
        self,
        cfg: DatabaseConfig,
        *,
        keep: bool,
        connector: "Callable[[DatabaseConfig], Connection] | None" = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.keep = keep
        self._connect = connector or connect
        self._clock = clock
        self._con: Connection | None = None
        self._last_used = 0.0
        self.connects = 0  # how many connections this process opened (tests, diagnostics)

    @property
    def connection(self) -> "Connection | None":
        return self._con

    def acquire(self) -> Connection:
        con = self._con
        if con is not None:
            idle = self._clock() - self._last_used
            if con.closed or con.broken or idle > IDLE_CLOSE:
                self.close()
            elif idle > PING_AFTER and not _answers(con):
                log.warning("gahk_web: the database connection stopped answering; reconnecting")
                self.close()
        if self._con is None:
            self._con = self._connect(self.cfg)
            self.connects += 1
        return self._con

    def release(self, *, failed: bool = False) -> None:
        con = self._con
        if con is None:
            return
        self._last_used = self._clock()
        if not self.keep or con.closed or con.broken:
            self.close()
            return
        try:
            from psycopg import pq

            if con.info.transaction_status != pq.TransactionStatus.IDLE:
                log.warning("gahk_web: a request left a transaction open; rolled it back")
                con.rollback()
            if failed:
                execute(con, "SELECT pg_advisory_unlock_all()")
        except Exception:
            log.exception("gahk_web: could not clean up the database connection; closing it")
            self.close()

    def close_if_idle(self) -> None:
        """Close the connection when no request used it for IDLE_CLOSE seconds."""
        if self._con is not None and self._clock() - self._last_used > IDLE_CLOSE:
            self.close()

    def close(self) -> None:
        con, self._con = self._con, None
        if con is not None:
            with contextlib.suppress(Exception):  # it may be gone already
                con.close()


def _answers(con: Connection) -> bool:
    try:
        con.execute("SELECT 1").fetchall()
    except Exception:
        return False
    return True


@cache
def _sql(sql: str) -> str:
    """:name placeholders as psycopg's %(name)s."""
    return _PARAM_RE.sub(lambda m: f"%({m.group(1)})s" if m.group(1) else "%%", sql)


def _run(con: Connection, sql: str, params: dict[str, object]) -> "psycopg.Cursor[Row]":
    return con.execute(_sql(sql), params) if params else con.execute(sql)


def query(con: Connection, sql: str, **params: object) -> list[Row]:
    return _run(con, sql, params).fetchall()


def query_one(con: Connection, sql: str, **params: object) -> Row | None:
    rows = query(con, sql, **params)
    return rows[0] if rows else None


def execute(con: Connection, sql: str, **params: object) -> int:
    """Run a statement and return the affected row count."""
    return max(_run(con, sql, params).rowcount, 0)


@contextmanager
def transaction(con: Connection) -> Iterator[Connection]:
    """Commits on exit, rolls back on an exception."""
    with con.transaction():
        yield con


@contextmanager
def advisory_lock(con: Connection, key: int) -> Iterator[None]:
    """Session-level lock; waits for it. Released on exit (and by Postgres if the process dies)."""
    execute(con, "SELECT pg_advisory_lock(:key)", key=key)
    try:
        yield
    finally:
        execute(con, "SELECT pg_advisory_unlock(:key)", key=key)


@contextmanager
def try_advisory_lock(con: Connection, key: int) -> Iterator[bool]:
    """Yields whether the lock was taken; never waits."""
    row = query_one(con, "SELECT pg_try_advisory_lock(:key) AS got", key=key)
    got = bool(row and row["got"])
    try:
        yield got
    finally:
        if got:
            execute(con, "SELECT pg_advisory_unlock(:key)", key=key)


def make_engine(cfg: DatabaseConfig) -> Engine:
    """A SQLAlchemy engine that keeps no connection, for migrations."""
    return create_engine(URL, poolclass=NullPool, connect_args=conninfo(cfg))


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
