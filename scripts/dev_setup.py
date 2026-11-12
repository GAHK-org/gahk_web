#!/usr/bin/env python3
"""Local development setup (used by the Taskfile; never on the webhost).

    python scripts/dev_setup.py config POSTGRES_URL DATABASE    write local/config.ini unless it exists
    python scripts/dev_setup.py databases POSTGRES_URL NAME...   create the databases if missing

POSTGRES_URL is a server URL without a database name, e.g. postgres://gahk:gahk@localhost:5432.
"""

import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pg8000.native

ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = ROOT / "local" / "config.ini"
DEV_PORT = 8810


def write_config(server: str, database: str) -> int:
    if LOCAL_CONFIG.exists():
        print(f"{LOCAL_CONFIG.relative_to(ROOT)} exists; leaving it as it is")
        return 0
    url = urlsplit(server)
    key = secrets.token_urlsafe(48)
    LOCAL_CONFIG.parent.mkdir(exist_ok=True)
    LOCAL_CONFIG.write_text(
        f"""# Development configuration for task dev (gitignored). The webhost's is in DEPLOY.md.
[database]
host = {url.hostname or "localhost"}
port = {url.port or 5432}
name = {database}
user = {url.username or ""}
password = {url.password or ""}
# The local dev PostgreSQL has no TLS; the webhost's config must not say this.
sslmode = disable

[sync]
# Put the same value in gahk_intern's app/.env as GAHK_WEB_KEY, with GAHK_WEB_URL=http://127.0.0.1:{DEV_PORT}
key = {key}

[stats]
secret = {secrets.token_urlsafe(48)}

[site]
base_url = http://127.0.0.1:{DEV_PORT}
intern_url = http://127.0.0.1:8800

[paths]
public_html = {ROOT / "var" / "public_html"}
state_dir = {ROOT / "var" / "state"}
""",
        encoding="utf-8",
    )
    print(f"wrote {LOCAL_CONFIG.relative_to(ROOT)}; for intern's app/.env:")
    print(f"  GAHK_WEB_URL=http://127.0.0.1:{DEV_PORT}")
    print(f"  GAHK_WEB_KEY={key}")
    return 0


def create_databases(server: str, names: list[str]) -> int:
    url = urlsplit(server)
    con = pg8000.native.Connection(
        user=url.username or "",
        password=url.password or None,
        host=url.hostname or "localhost",
        port=url.port or 5432,
        database="postgres",
    )
    try:
        existing = {row[0] for row in con.run("SELECT datname FROM pg_database")}
        for name in names:
            if name in existing:
                print(f"database {name} exists")
                continue
            con.run(f"CREATE DATABASE {pg8000.native.identifier(name)}")
            print(f"created database {name}")
    finally:
        con.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "config":
        return write_config(argv[1], argv[2])
    if len(argv) >= 3 and argv[0] == "databases":
        return create_databases(argv[1], argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
