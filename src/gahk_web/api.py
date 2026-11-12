"""The signed sync API that intern.gahk.dk calls: /api/*, a Flask blueprint.

Every call but GET /api/health carries X-Gahk-Timestamp and X-Gahk-Signature (see signing.py). The
contract (shapes, status codes) is shared with intern's cms.gahk_web client; change both together.

Each endpoint takes every method and checks, in this order: the body's size (413; 400 for an image),
the signature over the exact path and raw body (401), then the method (405). An unknown /api/
address is 404 once signed. That order is the contract's, so @signed does all three.
"""

import functools
import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from flask import Blueprint, Response, current_app, request
from sqlalchemy import select

from . import db, images, outbox, signing
from .clock import copenhagen_today
from .config import Config, home_dir
from .housekeeping import (
    PUBLISH_VARIANT_BUDGET,
    REBUILD_VARIANT_BUDGET,
    Superseded,
    fill_variants,
    purge,
    rebuild,
    superseded,
)
from .models import SiteState
from .render import load_snapshot, preview_html, render_site
from .snapshot import HEX64_RE, MAX_IMAGE_BYTES, SnapshotError, validate, validate_preview
from .version import code_version
from .web import ALL_METHODS, BodyTooLarge, database, header, html, json_response, read_body

MAX_BODY = 1024 * 1024
MAX_SNAPSHOT_BODY = 32 * 1024 * 1024
MAX_MISSING_QUERY = 10000
# How the request reached the app ("fastcgi" or "cgi"), on GET /api/health only. Not part of the
# contract with intern: the updater reads it to see that FastCGI runs the new release.
RUNNER_HEADER = "X-Gahk-Runner"

bp = Blueprint("api", __name__)

type View = Callable[..., Response]


def cfg() -> Config:
    config: Config = current_app.config["GAHK"]
    return config


def today() -> date:
    return copenhagen_today()


def error(status: int, message: str, **extra: object) -> Response:
    return json_response(status, {"error": message, **extra})


def signed(*methods: str, limit: int = MAX_BODY, too_large: int = 413) -> Callable[[View], View]:
    """Read the body (at most limit bytes), check intern's signature over the request's path and
    that body, then the method (any, when none are named). The view gets the body as `body`."""

    def decorate(view: View) -> View:
        @functools.wraps(view)
        def wrapper(**kwargs: Any) -> Response:  # noqa: ANN401 - URL values
            try:
                body = read_body(limit)
            except BodyTooLarge:
                return error(too_large, "the body is too large")
            reason = signing.verify(
                cfg().sync_key,
                header("X-Gahk-Timestamp"),
                header("X-Gahk-Signature"),
                request.method,
                request.path,
                body,
            )
            if reason:
                return error(401, reason)
            if methods and request.method not in methods:
                return error(405, "method not allowed")
            try:
                return view(body=body, **kwargs)
            except SnapshotError as exc:
                return error(400, str(exc))

        return wrapper

    return decorate


def _json(body: bytes) -> Any:  # noqa: ANN401 - parsed JSON, validated by the caller
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotError("the body is not JSON") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def update_state(config: Config) -> dict[str, Any]:
    """What the self-updater last recorded in state_dir/update.json (it owns that file)."""
    state: dict[str, Any] = {
        "checked_at": None,
        "available_version": None,
        "last_result": None,
        "last_error": None,
    }
    try:
        data = json.loads((config.state_dir / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        state.update({key: data.get(key) for key in state})
    return state


# --- endpoints ---


@bp.route("/api/health", methods=ALL_METHODS)
def health() -> Response:
    if request.method not in ("GET", "HEAD"):
        return error(405, "method not allowed")
    try:
        revision = database().scalar(select(SiteState.revision))
    except Exception:
        result = json_response(503, {"ok": False, "version": code_version(), "revision": None})
    else:
        result = json_response(200, {"ok": True, "version": code_version(), "revision": revision})
    result.headers[RUNNER_HEADER] = current_app.config["GAHK_RUNNER"]
    return result


@bp.route("/api/status", methods=ALL_METHODS)
@signed("GET")
def status(body: bytes) -> Response:
    session = database()
    row = session.execute(select(SiteState.revision, SiteState.published_at, SiteState.rendered_at)).first()
    return json_response(
        200,
        {
            "version": code_version(),
            "revision": row.revision if row else None,
            "published_at": _iso(row.published_at if row else None),
            "rendered_at": _iso(row.rendered_at if row else None),
            "outbox": outbox.counts(session),
            "update": update_state(cfg()),
        },
    )


@bp.route("/api/images/missing", methods=ALL_METHODS)
@signed("POST")
def images_missing(body: bytes) -> Response:
    data = _json(body)
    shas = data.get("sha256") if isinstance(data, dict) else None
    if (
        not isinstance(shas, list)
        or len(shas) > MAX_MISSING_QUERY
        or not all(isinstance(sha, str) and HEX64_RE.match(sha) for sha in shas)
    ):
        return error(400, 'expected {"sha256": ["<64 hex>", ...]}')
    return json_response(200, {"missing": images.missing(cfg(), shas)})


@bp.route("/api/images/<anypath:sha256>", methods=ALL_METHODS)
@signed("PUT", limit=MAX_IMAGE_BYTES, too_large=400)
def image_put(body: bytes, sha256: str) -> Response:
    session = database()
    try:
        images.store(session, cfg(), sha256, body, header("Content-Type"))
    except images.ImageError as exc:
        return error(400, str(exc))
    # intern uploads one image per request, so making its variants now spreads the work out. An
    # earlier attempt that was killed is not repeated here (it would fail intern's request again).
    images.ensure_variants(session, cfg(), [sha256], retry=False)
    return Response(b"", status=204, content_type="text/plain")


@bp.route("/api/publish", methods=ALL_METHODS)
@signed("POST", limit=MAX_SNAPSHOT_BODY)
def publish(body: bytes) -> Response:
    snapshot = validate(_json(body))
    session = database()
    config = cfg()
    day = today()
    missing = images.missing_for(config, snapshot)
    if missing:
        return error(409, "missing images", missing=missing)
    # Before the lock: variants are cache files, and making them may take a while. Whatever the
    # budget leaves is made by later requests (housekeeping), which then render again; so are images
    # whose earlier attempt was killed, so one of those cannot make publish after publish fail.
    fill_variants(session, config, snapshot, PUBLISH_VARIANT_BUDGET, retry=False)
    with db.advisory_lock(session, db.LOCK_RENDER):
        active = superseded()
        if active:  # an old release's process: its templates must not replace the new ones' pages
            return error(503, f"this is {code_version()}, the active release is {active}; try again")
        missing = images.missing_for(config, snapshot)
        if missing:
            return error(409, "missing images", missing=missing)
        images.materialize(session, config, snapshot)
        result = render_site(session, config, snapshot, day, store_snapshot=True)
        images.collect_garbage(session, config, snapshot)
        purge(session, day)
    return json_response(200, {"revision": snapshot["revision"], "pages_written": result.pages_written})


@bp.route("/api/preview", methods=ALL_METHODS)
@signed("POST", limit=MAX_SNAPSHOT_BODY)
def preview(body: bytes) -> Response:
    preview_request = validate_preview(_json(body))
    session = database()
    snapshot = load_snapshot(session)
    page = preview_html(
        cfg(),
        snapshot,
        preview_request["kind"],
        preview_request["item"],
        preview_request["media_base"],
        images.pictures(session, snapshot) if snapshot else {},
    )
    return html(200, page, ("X-Robots-Tag", "noindex"))


@bp.route("/api/outbox", methods=ALL_METHODS)
@signed("GET")
def outbox_pending(body: bytes) -> Response:
    return json_response(200, outbox.pending(database()))


@bp.route("/api/outbox/ack", methods=ALL_METHODS)
@signed("POST")
def outbox_ack(body: bytes) -> Response:
    try:
        ids, through = outbox.parse_ack(_json(body))
    except outbox.AckError as exc:
        return error(400, str(exc))
    return json_response(200, outbox.ack(database(), ids, through, today()))


@bp.route("/api/rebuild", methods=ALL_METHODS)
@signed("POST")
def rebuild_now(body: bytes) -> Response:
    session = database()
    snapshot = load_snapshot(session)
    if snapshot is not None:
        fill_variants(session, cfg(), snapshot, REBUILD_VARIANT_BUDGET, retry=False)
    try:
        with db.advisory_lock(session, db.LOCK_RENDER):
            written = rebuild(session, cfg(), today())
    except Superseded as exc:
        return error(503, f"{exc}; try again")
    if written is None:
        return error(409, "no snapshot has been published yet")
    return json_response(200, {"pages_written": written})


@bp.route("/api/update", methods=ALL_METHODS)
@signed("POST")
def update(body: bytes) -> Response:
    """Run the self-updater and report what it did.

    On the webhost .htaccess sends /api/update to the cgi-bin stub gahk.cgi, which answers it
    before any release is imported (so it works with no release installed, or a broken one); this
    is the same call for a release reached some other way. A development checkout never replaces
    itself."""
    if code_version() == "dev":
        return error(409, "a development checkout does not update itself", version="dev")
    from . import updater  # rarely needed here, and it imports much of the standard library

    return json_response(200, updater.nudge(home_dir()))


@bp.route("/api", methods=ALL_METHODS)
@bp.route("/api/<anypath:rest>", methods=ALL_METHODS)
@signed()
def unknown(body: bytes, rest: str = "") -> Response:
    return error(404, "no such endpoint")
