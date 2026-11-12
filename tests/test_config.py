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
    assert db._ssl_context(DatabaseConfig("h", 5432, "n", "u", "", sslmode="disable")) is None
    assert db._ssl_context(DatabaseConfig("h", 5432, "n", "u", "")) is not None
