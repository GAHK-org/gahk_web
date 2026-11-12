"""Maintenance commands: python -m gahk_web <command>

migrate   apply pending database migrations
rebuild   make missing image variants, then re-render the site from the cached snapshot
version   print the code version of this release
"""

import sys

from . import db
from .clock import copenhagen_today
from .config import load_config
from .housekeeping import COMMAND_VARIANT_BUDGET, fill_variants, rebuild
from .render import load_snapshot
from .version import code_version


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "version":
        print(code_version())
        return 0
    if command not in ("migrate", "rebuild"):
        print(__doc__, file=sys.stderr)
        return 2
    cfg = load_config()
    con = db.connect(cfg.database)
    try:
        if command == "migrate":
            applied = db.migrate(con)
            print("applied: " + (", ".join(applied) or "nothing"))
            return 0
        snapshot = load_snapshot(con)
        if snapshot is not None:
            fill_variants(con, cfg, snapshot, COMMAND_VARIANT_BUDGET)
        with db.advisory_lock(con, db.LOCK_RENDER):
            written = rebuild(con, cfg, copenhagen_today())
        print("no snapshot yet" if written is None else f"files written: {written}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
