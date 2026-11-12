"""PostgreSQL access over psycopg 3 (its binary wheel, which brings libpq along).

The webhost's database is a cache and an outbox, never the source of truth: the cached snapshot,
the image index, the list of generated files, and form submissions plus visit counts until intern
pulls them. Losing it costs at most the not-yet-pulled outbox.

The SQL in this package names its parameters :name, and query()/execute() take them as keyword
arguments; rows come back as dicts. A connection is in autocommit mode: every statement commits on
its own unless it runs inside transaction(). psycopg is imported on the first connect(), so requests
that need no database do not pay for it.

Schema changes are plain SQL files in gahk_web/migrations/NNNN_name.sql, applied in name order by
migrate() and recorded in schema_migrations. Each file runs in its own transaction.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from typing import TYPE_CHECKING, Any

from .config import DatabaseConfig

if TYPE_CHECKING:
    import psycopg

type Row = dict[str, Any]
type Connection = psycopg.Connection[Row]

# Advisory lock keys (pg_advisory_lock takes a bigint). "gahk" in hex, then a purpose.
LOCK_MIGRATE = 0x6761686B_0001
LOCK_RENDER = 0x6761686B_0002

CONNECT_TIMEOUT = 10

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
    from pathlib import Path

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


def migration_files() -> list[tuple[str, str]]:
    from importlib import resources

    folder = resources.files("gahk_web") / "migrations"
    files = sorted(entry.name for entry in folder.iterdir() if entry.name.endswith(".sql"))
    return [(name.removesuffix(".sql"), (folder / name).read_text(encoding="utf-8")) for name in files]


def migrate(con: Connection) -> list[str]:
    """Apply pending migrations; returns the names applied. Safe to run concurrently and repeatedly."""
    applied_now: list[str] = []
    with advisory_lock(con, LOCK_MIGRATE):
        execute(
            con,
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())",
        )
        done = {row["name"] for row in query(con, "SELECT name FROM schema_migrations")}
        for name, sql in migration_files():
            if name in done:
                continue
            with transaction(con):
                # Without parameters psycopg uses the simple-query protocol, so a migration file may
                # hold several statements.
                execute(con, sql)
                execute(con, "INSERT INTO schema_migrations (name) VALUES (:name)", name=name)
            applied_now.append(name)
    return applied_now
