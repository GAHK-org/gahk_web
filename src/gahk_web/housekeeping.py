"""Daily upkeep without a cron job (the webhost runs none, by decision).

The site's day decides which events are "upcoming". When a request reaches the app (a page view
beacon, a form, a 404) and the site was last rendered on an earlier Copenhagen day, or by other code
(the first request a new release answers), it re-renders from the cached snapshot first. The check
is a query per request, so it works the same in a FastCGI process that lives for hours. A try-lock makes sure only one request does it while
the others carry on. intern also sends POST /api/rebuild every night, so the site is right even on a
day nobody visits it. The same pass purges rate-limit rows older than a day and visitor hashes older
than two days.

Responsive image variants a publish had no time for (images.py) are made the same way, by the visit
beacon (nobody waits for its answer): a beacon that finds a published image still waiting makes
variants for a few seconds, under a try-lock of its own, and renders the site again when it made any.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from . import db, images
from .clock import copenhagen_today, now_utc
from .config import Config
from .models import FormRateLimit, Image, ImageFile, SiteState, VisitSeen
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
class State:
    """What the site_state row says about the need to render again."""

    has_snapshot: bool
    rendered_on: date | None
    rendered_version: str | None
    variants_pending: bool = False


def site_state(session: Session) -> State:
    # Whether a published image still waits for its variants.
    variants_pending = exists().where(
        Image.variant_widths.is_(None),
        Image.variant_attempts < images.MAX_VARIANT_ATTEMPTS,
        Image.sha256.in_(select(ImageFile.sha256)),
    )
    row = session.execute(
        select(
            SiteState.snapshot.is_not(None),
            SiteState.rendered_on,
            SiteState.rendered_version,
            variants_pending,
        )
    ).first()
    if row is None:
        return State(False, None, None)
    has_snapshot, rendered_on, rendered_version, pending = row
    return State(bool(has_snapshot), rendered_on, rendered_version, bool(pending))


def is_stale(state: State, today: date) -> bool:
    return state.has_snapshot and (
        state.rendered_on is None or state.rendered_on < today or state.rendered_version != code_version()
    )


def purge(session: Session, today: date) -> None:
    session.execute(delete(FormRateLimit).where(FormRateLimit.at < now_utc() - RATE_LIMIT_KEEP))
    session.execute(delete(VisitSeen).where(VisitSeen.day <= today - timedelta(days=SEEN_KEEP_DAYS)))
    session.commit()


def rebuild(session: Session, cfg: Config, today: date) -> int | None:
    """Re-render from the cached snapshot (caller holds the render lock). None: no snapshot yet."""
    snapshot = load_snapshot(session)
    if snapshot is None:
        return None
    result = render_site(session, cfg, snapshot, today)
    purge(session, today)
    return result.pages_written


def fill_variants(
    session: Session, cfg: Config, snapshot: Snapshot, budget: float, *, retry: bool = True
) -> int:
    """Make the variants the snapshot's images lack, for at most about `budget` seconds. Returns how
    many images' links changed (the site needs rendering again when that is not 0). retry=False
    leaves images whose earlier attempt never finished to housekeeping (images.ensure_variants)."""
    return images.ensure_variants(session, cfg, images.snapshot_shas(snapshot), budget, retry=retry).changed


def maybe_rebuild(
    session: Session,
    cfg: Config,
    state: State | None = None,
    today: date | None = None,
    *,
    make_variants: bool = False,
) -> bool:
    """Re-render when the day (or the code) changed since the last render, or (with make_variants)
    after making variants that were still missing. Never waits for a lock."""
    today = today or copenhagen_today()
    state = state or site_state(session)
    stale = is_stale(state, today)
    changed = 0
    if make_variants and state.has_snapshot and state.variants_pending:
        with db.try_advisory_lock(session, db.LOCK_VARIANTS) as got:
            snapshot = load_snapshot(session) if got else None
            if snapshot is not None:
                changed = fill_variants(session, cfg, snapshot, HOUSEKEEPING_VARIANT_BUDGET)
    if not stale and not changed:
        return False
    with db.try_advisory_lock(session, db.LOCK_RENDER) as got:
        # Another request may have rendered while this one waited for its turn to ask.
        if not got or not (changed or is_stale(site_state(session), today)):
            return False
        return rebuild(session, cfg, today) is not None
