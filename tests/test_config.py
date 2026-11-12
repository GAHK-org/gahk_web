"""config.ini: the database connection is encrypted unless a config says otherwise."""

from pathlib import Path

import pytest

from gahk_web import db
from gahk_web.config import ConfigError, DatabaseConfig, load_config

BASE = "[database]\nname = gahk\nuser = gahk\n{extra}[sync]\nkey = {key}\n[stats]\nsecret = {key}\n"


def config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.ini"
    path.write_text(BASE.format(extra=extra, key="k" * 40), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("extra", "sslmode"),
    [
        ("", "require"),
        ("sslrootcert = /home/gahk/sqlca.pem\n", "verify-full"),
        ("sslmode = require\nsslrootcert = /home/gahk/sqlca.pem\n", "require"),
        ("sslmode = verify-full\n", "verify-full"),
        ("sslmode = disable\n", "disable"),
    ],
)
def test_sslmode_defaults_to_encrypted(tmp_path: Path, extra: str, sslmode: str) -> None:
    assert load_config(config(tmp_path, extra)).database.sslmode == sslmode


def test_unknown_sslmode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="sslmode"):
        load_config(config(tmp_path, "sslmode = prefer\n"))


def test_only_disable_connects_without_tls() -> None:
    assert db.conninfo(DatabaseConfig("h", 5432, "n", "u", "", sslmode="disable"))["sslmode"] == "disable"
    assert db.conninfo(DatabaseConfig("h", 5432, "n", "u", ""))["sslmode"] == "require"


def test_conninfo_passes_the_certificate_only_where_it_is_checked() -> None:
    def info(sslmode: str, sslrootcert: str = "") -> dict[str, str | int]:
        return db.conninfo(DatabaseConfig("db.example", 5433, "gahk", "gahk", "pw", sslmode, sslrootcert))

    assert info("verify-full", "/home/gahk/sqlca.pem")["sslrootcert"] == "/home/gahk/sqlca.pem"
    assert info("verify-full")["sslrootcert"]  # the system's certificate authorities
    # libpq's "require" would check the certificate after all if it were given a root certificate.
    assert "sslrootcert" not in info("require", "/home/gahk/sqlca.pem")
    assert info("require") | {"connect_timeout": 0} == {
        "host": "db.example",
        "port": 5433,
        "dbname": "gahk",
        "user": "gahk",
        "password": "pw",
        "application_name": "gahk_web",
        "connect_timeout": 0,
        "sslmode": "require",
        "gssencmode": "disable",
        "options": "-c TimeZone=UTC",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 15000,
    }
    assert "password" not in db.conninfo(DatabaseConfig("h", 5432, "n", "u", ""))


def test_the_engines_connect_with_these_parameters() -> None:
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool, QueuePool

    cfg = DatabaseConfig("db.example", 5433, "gahk", "gahk", "pw")
    for engine in (db.make_engine(cfg), create_engine(db.URL, **db.engine_options(cfg, persistent=True))):
        seen: dict[str, object] = {}

        @event.listens_for(engine, "do_connect")
        def capture(dialect: object, record: object, cargs: object, cparams: dict[str, object]) -> None:
            seen.update(cparams)
            raise RuntimeError("no connection needed")

        with pytest.raises(RuntimeError, match="no connection needed"):
            engine.connect()
        seen.pop("context")  # SQLAlchemy's own (psycopg's type adapters)
        assert seen == db.conninfo(cfg)
        assert seen["sslmode"] == "require"
    # The FastCGI process keeps one connection, checked before each use; CGI and commands keep none.
    persistent = create_engine(db.URL, **db.engine_options(cfg, persistent=True)).pool
    assert isinstance(persistent, QueuePool) and persistent.size() == 1 and persistent._pre_ping
    assert isinstance(db.make_engine(cfg).pool, NullPool)
