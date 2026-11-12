"""Configuration: one INI file, written once by hand on the host and never kept in git.

Location: $GAHK_WEB_CONFIG, else <home>/gahk_web/config.ini, where <home> is $GAHK_WEB_HOME (the
cgi-bin stub sets it from its own location) or the user's home directory.

    [database]
    host = ...            ; the hostname konsoleH shows (localhost does not work on the webhost)
    port = 5432
    name = ...
    user = ...
    password = ...
    sslmode = verify-full ; require | verify-full | disable. Default: verify-full with sslrootcert,
                          ; else require. disable (no encryption) is for a local database only.
    sslrootcert = ...     ; for verify-full (Hetzner's sqlca.pem)

    [sync]
    key = ...             ; = GAHK_WEB_KEY on intern; signs every /api/* call

    [turnstile]           ; optional: no secret, no captcha
    site_key = ...
    secret_key = ...

    [site]
    base_url = https://gahk.dk
    intern_url = https://intern.gahk.dk

    [paths]
    public_html = /usr/home/USER/public_html   ; default <home>/public_html
    state_dir = /usr/home/USER/gahk_web/state  ; default <home>/gahk_web/state

    [stats]
    secret = ...          ; HMAC key for visitor and rate-limit hashes (no IP is ever stored)
"""

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MIN_SECRET_LENGTH = 32


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    sslmode: str = "require"  # encrypted unless a config says "disable"
    sslrootcert: str = ""


@dataclass(frozen=True)
class Config:
    database: DatabaseConfig
    sync_key: str
    stats_secret: str
    public_html: Path
    state_dir: Path
    base_url: str = "https://gahk.dk"
    intern_url: str = "https://intern.gahk.dk"
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    @property
    def site_netloc(self) -> str:
        """host[:port] of the public site, lower-case: what Origin/Referer must name."""
        return urlsplit(self.base_url).netloc.lower()

    @property
    def is_https(self) -> bool:
        return self.base_url.startswith("https://")


def home_dir() -> Path:
    return Path(os.environ.get("GAHK_WEB_HOME") or Path.home())


def config_path() -> Path:
    explicit = os.environ.get("GAHK_WEB_CONFIG")
    return Path(explicit) if explicit else home_dir() / "gahk_web" / "config.ini"


def _url(value: str, name: str) -> str:
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query:
        raise ConfigError(f"{name} must be a bare http(s) origin like https://gahk.dk, got {value!r}")
    return value


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        raise ConfigError(f"config file not found: {path}")

    def get(section: str, key: str, default: str = "") -> str:
        return parser.get(section, key, fallback=default).strip()

    # The database is reached over the network, so the connection is encrypted unless disabled.
    sslmode = get("database", "sslmode") or ("verify-full" if get("database", "sslrootcert") else "require")
    if sslmode not in ("disable", "require", "verify-full"):
        raise ConfigError(f"[database] sslmode must be require, verify-full or disable, got {sslmode!r}")
    try:
        port = int(get("database", "port", "5432"))
    except ValueError as exc:
        raise ConfigError("[database] port must be a number") from exc
    database = DatabaseConfig(
        host=get("database", "host", "localhost"),
        port=port,
        name=get("database", "name"),
        user=get("database", "user"),
        password=get("database", "password"),
        sslmode=sslmode,
        sslrootcert=get("database", "sslrootcert"),
    )
    if not database.name or not database.user:
        raise ConfigError("[database] name and user are required")

    sync_key = get("sync", "key")
    stats_secret = get("stats", "secret")
    for label, secret in (("[sync] key", sync_key), ("[stats] secret", stats_secret)):
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f"{label} must be at least {MIN_SECRET_LENGTH} characters")

    home = home_dir()
    return Config(
        database=database,
        sync_key=sync_key,
        stats_secret=stats_secret,
        public_html=Path(get("paths", "public_html") or home / "public_html"),
        state_dir=Path(get("paths", "state_dir") or home / "gahk_web" / "state"),
        base_url=_url(get("site", "base_url", "https://gahk.dk"), "[site] base_url"),
        intern_url=_url(get("site", "intern_url", "https://intern.gahk.dk"), "[site] intern_url"),
        turnstile_site_key=get("turnstile", "site_key"),
        turnstile_secret_key=get("turnstile", "secret_key"),
    )
