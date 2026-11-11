"""PostgreSQL access over pg8000 (pure Python, so it survives the host's Python upgrades).

The webhost's database is a cache and an outbox, never the source of truth: the cached snapshot,
the image index, the list of generated files, and form submissions plus visit counts until intern
pulls them. Losing it costs at most the not-yet-pulled outbox.

Schema changes are plain SQL files in gahk_web/migrations/NNNN_name.sql, applied in name order by
migrate() and recorded in schema_migrations. Each file runs in its own transaction.
"""

import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any, TypeAlias

import pg8000.native

from .config import DatabaseConfig

Connection: TypeAlias = pg8000.native.Connection
Row = dict[str, Any]

# Advisory lock keys (pg_advisory_lock takes a bigint). "gahk" in hex, then a purpose.
LOCK_MIGRATE = 0x6761686B_0001
LOCK_RENDER = 0x6761686B_0002

CONNECT_TIMEOUT = 10


def _ssl_context(cfg: DatabaseConfig) -> ssl.SSLContext | None:
    if cfg.sslmode in ("", "disable"):
        return None
    if cfg.sslmode == "verify-full":
        return ssl.create_default_context(cafile=cfg.sslrootcert or None)
    # "require": encrypted, but the server certificate is not checked (libpq's meaning of the word).
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def connect(cfg: DatabaseConfig) -> Connection:
    con = pg8000.native.Connection(
        user=cfg.user,
        password=cfg.password or None,
        host=cfg.host,
        port=cfg.port,
        database=cfg.name,
        ssl_context=_ssl_context(cfg),
        timeout=CONNECT_TIMEOUT,
        application_name="gahk_web",
    )
    con.run("SET TIME ZONE 'UTC'")
    return con


def query(con: Connection, sql: str, **params: object) -> list[Row]:
    rows = con.run(sql, **params) or []
    names = [column["name"] for column in (con.columns or [])]
    return [dict(zip(names, row, strict=True)) for row in rows]


def query_one(con: Connection, sql: str, **params: object) -> Row | None:
    rows = query(con, sql, **params)
    return rows[0] if rows else None


def execute(con: Connection, sql: str, **params: object) -> int:
    """Run a statement and return the affected row count."""
    con.run(sql, **params)
    return int(con.row_count or 0)


@contextmanager
def transaction(con: Connection) -> Iterator[Connection]:
    con.run("START TRANSACTION")
    try:
        yield con
    except BaseException:
        con.run("ROLLBACK")
        raise
    con.run("COMMIT")


@contextmanager
def advisory_lock(con: Connection, key: int) -> Iterator[None]:
    """Session-level lock; waits for it. Released on exit (and by Postgres if the process dies)."""
    con.run("SELECT pg_advisory_lock(:key)", key=key)
    try:
        yield
    finally:
        con.run("SELECT pg_advisory_unlock(:key)", key=key)


@contextmanager
def try_advisory_lock(con: Connection, key: int) -> Iterator[bool]:
    """Yields whether the lock was taken; never waits."""
    got = bool(con.run("SELECT pg_try_advisory_lock(:key)", key=key)[0][0])
    try:
        yield got
    finally:
        if got:
            con.run("SELECT pg_advisory_unlock(:key)", key=key)


def migration_files() -> list[tuple[str, str]]:
    folder = resources.files("gahk_web") / "migrations"
    files = sorted(entry.name for entry in folder.iterdir() if entry.name.endswith(".sql"))
    return [(name.removesuffix(".sql"), (folder / name).read_text(encoding="utf-8")) for name in files]


def migrate(con: Connection) -> list[str]:
    """Apply pending migrations; returns the names applied. Safe to run concurrently and repeatedly."""
    applied_now: list[str] = []
    with advisory_lock(con, LOCK_MIGRATE):
        con.run(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in con.run("SELECT name FROM schema_migrations")}
        for name, sql in migration_files():
            if name in done:
                continue
            with transaction(con):
                # Simple-query protocol: a migration file may hold several statements.
                con.execute_simple(sql)
                con.run("INSERT INTO schema_migrations (name) VALUES (:name)", name=name)
            applied_now.append(name)
    return applied_now
