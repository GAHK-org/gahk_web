"""Daily upkeep without a cron job (the webhost runs none, by decision).

The site's day decides which events are "upcoming". When a request reaches the CGI app (a page view
beacon, a form, a 404) and the site was last rendered on an earlier Copenhagen day, or by other code,
it re-renders from the cached snapshot first. A try-lock makes sure only one request does it while
the others carry on. intern also sends POST /api/rebuild every night, so the site is right even on a
day nobody visits it. The same pass purges rate-limit rows older than a day and visitor hashes older
than two days.

Responsive image variants a publish had no time for (images.py) are made the same way, by the visit
beacon (nobody waits for its answer): a beacon that finds a published image still waiting makes
variants for a few seconds, under a try-lock of its own, and renders the site again when it made any.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from . import db, images
from .clock import copenhagen_today, now_utc
from .config import Config
from .render import load_snapshot, render_site
from .snapshot import Snapshot
from .version import code_version

RATE_LIMIT_KEEP = timedelta(days=1)
SEEN_KEEP_DAYS = 2
# Seconds a request may spend making image variants. A publish or rebuild answers intern (which waits
# 60 s); housekeeping runs inside a visitor's request; the command line runs detached (the updater).
PUBLISH_VARIANT_BUDGET = 20.0
REBUILD_VARIANT_BUDGET = 30.0
HOUSEKEEPING_VARIANT_BUDGET = 3.0
COMMAND_VARIANT_BUDGET = 200.0


@dataclass(frozen=True)
class SiteState:
    has_snapshot: bool
    rendered_on: date | None
    rendered_version: str | None
    variants_pending: bool = False


def site_state(con: db.Connection) -> SiteState:
    row = db.query_one(
        con,
        "SELECT snapshot IS NOT NULL AS has_snapshot, rendered_on, rendered_version,"
        # Whether a published image still waits for its variants.
        " EXISTS (SELECT 1 FROM images WHERE variant_widths IS NULL AND variant_attempts < :attempts"
        " AND sha256 IN (SELECT sha256 FROM image_files)) AS variants_pending"
        " FROM site_state WHERE id = 1",
        attempts=images.MAX_VARIANT_ATTEMPTS,
    )
    if row is None:
        return SiteState(False, None, None)
    return SiteState(
        bool(row["has_snapshot"]),
        row["rendered_on"],
        row["rendered_version"],
        bool(row["variants_pending"]),
    )


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


def fill_variants(
    con: db.Connection, cfg: Config, snapshot: Snapshot, budget: float, *, retry: bool = True
) -> int:
    """Make the variants the snapshot's images lack, for at most about `budget` seconds. Returns how
    many images' links changed (the site needs rendering again when that is not 0). retry=False
    leaves images whose earlier attempt never finished to housekeeping (images.ensure_variants)."""
    return images.ensure_variants(con, cfg, images.snapshot_shas(snapshot), budget, retry=retry).changed


def maybe_rebuild(
    con: db.Connection,
    cfg: Config,
    state: SiteState | None = None,
    today: date | None = None,
    *,
    make_variants: bool = False,
) -> bool:
    """Re-render when the day (or the code) changed since the last render, or (with make_variants)
    after making variants that were still missing. Never waits for a lock."""
    today = today or copenhagen_today()
    state = state or site_state(con)
    stale = is_stale(state, today)
    changed = 0
    if make_variants and state.has_snapshot and state.variants_pending:
        with db.try_advisory_lock(con, db.LOCK_VARIANTS) as got:
            snapshot = load_snapshot(con) if got else None
            if snapshot is not None:
                changed = fill_variants(con, cfg, snapshot, HOUSEKEEPING_VARIANT_BUDGET)
    if not stale and not changed:
        return False
    with db.try_advisory_lock(con, db.LOCK_RENDER) as got:
        # Another request may have rendered while this one waited for its turn to ask.
        if not got or not (changed or is_stale(site_state(con), today)):
            return False
        return rebuild(con, cfg, today) is not None
