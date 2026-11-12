"""The signed sync API that intern.gahk.dk calls: /api/*.

Every call but GET /api/health carries X-Gahk-Timestamp and X-Gahk-Signature (see signing.py). The
contract (shapes, status codes) is shared with intern's cms.gahk_web client; change both together.
"""

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from . import db, images, outbox, signing
from .clock import copenhagen_today
from .config import Config, home_dir
from .housekeeping import PUBLISH_VARIANT_BUDGET, REBUILD_VARIANT_BUDGET, fill_variants, purge, rebuild
from .render import load_snapshot, preview_html, render_site
from .snapshot import HEX64_RE, MAX_IMAGE_BYTES, SnapshotError, validate, validate_preview
from .version import code_version
from .wsgi import BodyTooLarge, Request, Response, html, json_response

MAX_BODY = 1024 * 1024
MAX_SNAPSHOT_BODY = 32 * 1024 * 1024
MAX_MISSING_QUERY = 10000
IMAGES_PREFIX = "/api/images/"


class Api:
    """One API request: the configuration and a database connection opened on first use."""

    def __init__(self, cfg: Config, connect: Callable[[], db.Connection]) -> None:
        self.cfg = cfg
        self._connect = connect

    @property
    def con(self) -> db.Connection:
        return self._connect()

    @staticmethod
    def today() -> date:
        return copenhagen_today()


def _error(status: int, message: str, **extra: object) -> Response:
    return json_response(status, {"error": message, **extra})


def _json(body: bytes) -> Any:  # noqa: ANN401 - parsed JSON, validated by the caller
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotError("the body is not JSON") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def update_state(cfg: Config) -> dict[str, Any]:
    """What the self-updater last recorded in state_dir/update.json (it owns that file)."""
    state: dict[str, Any] = {
        "checked_at": None,
        "available_version": None,
        "last_result": None,
        "last_error": None,
    }
    try:
        data = json.loads((cfg.state_dir / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        state.update({key: data.get(key) for key in state})
    return state


def health(api: Api) -> Response:
    try:
        row = db.query_one(api.con, "SELECT revision FROM site_state WHERE id = 1")
    except Exception:
        return json_response(503, {"ok": False, "version": code_version(), "revision": None})
    return json_response(
        200, {"ok": True, "version": code_version(), "revision": row["revision"] if row else None}
    )


def status(api: Api) -> Response:
    row = (
        db.query_one(api.con, "SELECT revision, published_at, rendered_at FROM site_state WHERE id = 1") or {}
    )
    return json_response(
        200,
        {
            "version": code_version(),
            "revision": row.get("revision"),
            "published_at": _iso(row.get("published_at")),
            "rendered_at": _iso(row.get("rendered_at")),
            "outbox": outbox.counts(api.con),
            "update": update_state(api.cfg),
        },
    )


def images_missing(api: Api, body: bytes) -> Response:
    data = _json(body)
    shas = data.get("sha256") if isinstance(data, dict) else None
    if (
        not isinstance(shas, list)
        or len(shas) > MAX_MISSING_QUERY
        or not all(isinstance(sha, str) and HEX64_RE.match(sha) for sha in shas)
    ):
        return _error(400, 'expected {"sha256": ["<64 hex>", ...]}')
    return json_response(200, {"missing": images.missing(api.cfg, shas)})


def image_put(api: Api, request: Request, sha256: str, body: bytes) -> Response:
    try:
        images.store(api.con, api.cfg, sha256, body, request.header("Content-Type"))
    except images.ImageError as exc:
        return _error(400, str(exc))
    # intern uploads one image per request, so making its variants now spreads the work out. An
    # earlier attempt that was killed is not repeated here (it would fail intern's request again).
    images.ensure_variants(api.con, api.cfg, [sha256], retry=False)
    return Response(204, b"", "text/plain")


def publish(api: Api, body: bytes) -> Response:
    snapshot = validate(_json(body))
    con = api.con
    today = api.today()
    missing = images.missing_for(api.cfg, snapshot)
    if missing:
        return _error(409, "missing images", missing=missing)
    # Before the lock: variants are cache files, and making them may take a while. Whatever the
    # budget leaves is made by later requests (housekeeping), which then render again; so are images
    # whose earlier attempt was killed, so one of those cannot make publish after publish fail.
    fill_variants(con, api.cfg, snapshot, PUBLISH_VARIANT_BUDGET, retry=False)
    with db.advisory_lock(con, db.LOCK_RENDER):
        missing = images.missing_for(api.cfg, snapshot)
        if missing:
            return _error(409, "missing images", missing=missing)
        images.materialize(con, api.cfg, snapshot)
        result = render_site(con, api.cfg, snapshot, today, store_snapshot=True)
        images.collect_garbage(con, api.cfg, snapshot)
        purge(con, today)
    return json_response(200, {"revision": snapshot["revision"], "pages_written": result.pages_written})


def preview(api: Api, body: bytes) -> Response:
    request = validate_preview(_json(body))
    snapshot = load_snapshot(api.con)
    page = preview_html(
        api.cfg,
        snapshot,
        request["kind"],
        request["item"],
        request["media_base"],
        images.pictures(api.con, snapshot) if snapshot else {},
    )
    return html(200, page, ("X-Robots-Tag", "noindex"))


def outbox_ack(api: Api, body: bytes) -> Response:
    try:
        ids, through = outbox.parse_ack(_json(body))
    except outbox.AckError as exc:
        return _error(400, str(exc))
    return json_response(200, outbox.ack(api.con, ids, through, api.today()))


def rebuild_now(api: Api) -> Response:
    con = api.con
    snapshot = load_snapshot(con)
    if snapshot is not None:
        fill_variants(con, api.cfg, snapshot, REBUILD_VARIANT_BUDGET, retry=False)
    with db.advisory_lock(con, db.LOCK_RENDER):
        written = rebuild(con, api.cfg, api.today())
    if written is None:
        return _error(409, "no snapshot has been published yet")
    return json_response(200, {"pages_written": written})


def update(api: Api) -> Response:
    """Run the self-updater and report what it did.

    On the webhost the cgi-bin stub answers POST /api/update itself, before any release is imported
    (so it works with no release installed, or a broken one); this is the same call for a release
    started some other way. A development checkout never replaces itself."""
    if code_version() == "dev":
        return _error(409, "a development checkout does not update itself", version="dev")
    from . import updater  # rarely needed here, and it imports much of the standard library

    return json_response(200, updater.nudge(home_dir()))


Handler = Callable[[Api, Request, bytes], Response]
ROUTES: dict[tuple[str, str], Handler] = {
    ("POST", "/api/images/missing"): lambda api, _req, body: images_missing(api, body),
    ("GET", "/api/status"): lambda api, _req, _body: status(api),
    ("POST", "/api/publish"): lambda api, _req, body: publish(api, body),
    ("POST", "/api/preview"): lambda api, _req, body: preview(api, body),
    ("GET", "/api/outbox"): lambda api, _req, _body: json_response(200, outbox.pending(api.con)),
    ("POST", "/api/outbox/ack"): lambda api, _req, body: outbox_ack(api, body),
    ("POST", "/api/rebuild"): lambda api, _req, _body: rebuild_now(api),
    ("POST", "/api/update"): lambda api, _req, _body: update(api),
}


def handle(request: Request, cfg: Config, connect: Callable[[], db.Connection]) -> Response:
    api = Api(cfg, connect)
    path = request.path
    if path == "/api/health":
        return health(api) if request.method in ("GET", "HEAD") else _error(405, "method not allowed")

    is_image_put = path.startswith(IMAGES_PREFIX) and path != "/api/images/missing"
    if is_image_put:
        limit = MAX_IMAGE_BYTES
    elif path in ("/api/publish", "/api/preview"):
        limit = MAX_SNAPSHOT_BODY
    else:
        limit = MAX_BODY
    try:
        body = request.body(limit)
    except BodyTooLarge:
        return _error(400 if is_image_put else 413, "the body is too large")

    reason = signing.verify(
        cfg.sync_key,
        request.header("X-Gahk-Timestamp"),
        request.header("X-Gahk-Signature"),
        request.method,
        path,
        body,
    )
    if reason:
        return _error(401, reason)

    try:
        if is_image_put:
            if request.method != "PUT":
                return _error(405, "method not allowed")
            return image_put(api, request, path[len(IMAGES_PREFIX) :], body)
        route = ROUTES.get((request.method, path))
        if route is None:
            known = any(route_path == path for _method, route_path in ROUTES)
            return _error(405, "method not allowed") if known else _error(404, "no such endpoint")
        return route(api, request, body)
    except SnapshotError as exc:
        return _error(400, str(exc))
