"""Daily upkeep without a cron job (the webhost runs none, by decision).

The site's day decides which events are "upcoming". When a request reaches the CGI app (a page view
beacon, a form, a 404) and the site was last rendered on an earlier Copenhagen day, or by other code,
it re-renders from the cached snapshot first. A try-lock makes sure only one request does it while
the others carry on. intern also sends POST /api/rebuild every night, so the site is right even on a
day nobody visits it. The same pass purges rate-limit rows older than a day and visitor hashes older
than two days.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from . import db
from .clock import copenhagen_today, now_utc
from .config import Config
from .render import load_snapshot, render_site
from .version import code_version

RATE_LIMIT_KEEP = timedelta(days=1)
SEEN_KEEP_DAYS = 2


@dataclass(frozen=True)
class SiteState:
    has_snapshot: bool
    rendered_on: date | None
    rendered_version: str | None


def site_state(con: db.Connection) -> SiteState:
    row = db.query_one(
        con,
        "SELECT snapshot IS NOT NULL AS has_snapshot, rendered_on, rendered_version FROM site_state WHERE id = 1",
    )
    if row is None:
        return SiteState(False, None, None)
    return SiteState(bool(row["has_snapshot"]), row["rendered_on"], row["rendered_version"])


def is_stale(state: SiteState, today: date) -> bool:
    return state.has_snapshot and (
        state.rendered_on is None or state.rendered_on < today or state.rendered_version != code_version()
    )


def purge(con: db.Connection, today: date) -> None:
    db.execute(con, "DELETE FROM form_rate_limit WHERE at < :cutoff", cutoff=now_utc() - RATE_LIMIT_KEEP)
    db.execute(
        con, "DELETE FROM visit_seen WHERE day <= :cutoff", cutoff=today - timedelta(days=SEEN_KEEP_DAYS)
    )


def rebuild(con: db.Connection, cfg: Config, today: date) -> int | None:
    """Re-render from the cached snapshot (caller holds the render lock). None: no snapshot yet."""
    snapshot = load_snapshot(con)
    if snapshot is None:
        return None
    result = render_site(con, cfg, snapshot, today)
    purge(con, today)
    return result.pages_written


def maybe_rebuild(
    con: db.Connection, cfg: Config, state: SiteState | None = None, today: date | None = None
) -> bool:
    """Re-render when the day (or the code) changed since the last render. Never waits for a lock."""
    today = today or copenhagen_today()
    if not is_stale(state or site_state(con), today):
        return False
    with db.try_advisory_lock(con, db.LOCK_RENDER) as got:
        # Another request may have rendered while this one waited for its turn to ask.
        if not got or not is_stale(site_state(con), today):
            return False
        return rebuild(con, cfg, today) is not None
