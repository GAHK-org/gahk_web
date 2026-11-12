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
    }
    assert "password" not in db.conninfo(DatabaseConfig("h", 5432, "n", "u", ""))


def test_named_parameters_become_psycopg_placeholders() -> None:
    sql = "SELECT id::text, :a FROM t WHERE x LIKE '5%' AND y = :b_2 AND z = CAST(:a AS jsonb)"
    assert db._sql(sql) == (
        "SELECT id::text, %(a)s FROM t WHERE x LIKE '5%%' AND y = %(b_2)s AND z = CAST(%(a)s AS jsonb)"
    )
