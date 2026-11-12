"""The Flask app: whatever the static files cannot answer.

    /api/*                              the signed sync API (api.py, a blueprint of its own)
    POST /optagelse/ansoeg, /optagelse/send_rundvisning,
         /optagelse/fremlej, /optagelse/send_fremleje        the optagelse forms (forms.py)
    POST /s                             the visit beacon (stats.py)
    GET  /kalender/<token>.ics          the calendar pass-through to intern (kalender.py)
    anything else (GET/HEAD)            the fallback: old addresses → 301/308, the other slash form
                                        of a page → 301, else the 404 page; no snapshot yet → 503

On the webhost it runs as a persistent FastCGI process (fcgi_entry.py, behind cgi-bin/gahk.fcgi),
or one request per process through CGI (cgi_entry.py, behind cgi-bin/gahk.cgi). The database is
the webhost's own cache/outbox, never intern's: Flask-SQLAlchemy makes the engine, a request opens
its session on first use, and in the persistent process the connection is reused (db.Database).
"""

import logging
import mimetypes
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from flask import Blueprint, Flask, Response, current_app, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.exceptions import HTTPException, InternalServerError

from . import api, db, forms, housekeeping, images, routing, stats
from .clock import copenhagen_today
from .config import Config
from .models import Base, GeneratedFile, SiteState
from .render import EMPTY_SNAPSHOT, Site, load_snapshot, page_file, quote_path
from .snapshot import PATH_RE
from .web import (
    ALL_METHODS,
    PAGE_CACHE,
    AnyPath,
    BodyTooLarge,
    DatabaseUnavailable,
    StartResponse,
    WsgiApp,
    database,
    header,
    html,
    json_response,
    method_not_allowed,
    query,
    read_body,
    redirect,
    release_database,
    response,
    text,
    use_browser_path,
)

# The shape of a generated page's folder (pages have two segments at most, optagelse/ansoeg/success three).
_PAGE_PATH_RE = re.compile(r"^[a-z0-9_-]+(?:/[a-z0-9_-]+){0,2}$")
# The hash ManifestStaticFilesStorage put into the old site's static names: gang.b8dc291a4e43.jpg.
_STATIC_HASH_RE = re.compile(r"\.[0-9a-f]{12}(?=\.[^./]+$)")
CONFIG_ERROR_TEXT = "Siden er midlertidigt utilgængelig."

# Flask-SQLAlchemy: the engine of each app made by create_app(), from SQLALCHEMY_ENGINE_OPTIONS. The
# requests use their own sessions (db.Database), not orm.session: see db.py.
orm = SQLAlchemy(metadata=Base.metadata)

log = logging.getLogger("gahk_web")
site = Blueprint("site", __name__)


def _cfg() -> Config:
    cfg: Config = current_app.config["GAHK"]
    return cfg


# --- pages served by the app ---


def _public_file(cfg: Config, rel_path: str) -> str | None:
    try:
        return (cfg.public_html / rel_path).read_text(encoding="utf-8")
    except OSError:
        return None


def not_found(cfg: Config) -> Response:
    page = _public_file(cfg, "404.html") or Site(cfg, EMPTY_SNAPSHOT, copenhagen_today()).error_html(404)
    return html(404, page, PAGE_CACHE)


def unavailable(cfg: Config) -> Response:
    page = _public_file(cfg, "503.html") or Site(cfg, EMPTY_SNAPSHOT, copenhagen_today()).error_html(503)
    return html(503, page, ("Retry-After", "300"))


def serve_file(path: Path) -> Response:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "application/xml"):
        content_type += "; charset=utf-8"
    return response(200, path.read_bytes(), content_type, PAGE_CACHE)


# --- the optagelse forms (both slash forms; strict_slashes=False never redirects) ---


@site.post("/optagelse/ansoeg/", strict_slashes=False)
@site.post("/optagelse/send_rundvisning/", strict_slashes=False)
@site.post("/optagelse/fremlej/", strict_slashes=False)
@site.post("/optagelse/send_fremleje/", strict_slashes=False)
def form_post() -> Response:
    spec = forms.spec_for_post(request.path)
    if spec is None:
        return fallback()
    cfg = _cfg()
    session = database()
    state = housekeeping.site_state(session)
    if not state.has_snapshot:
        return unavailable(cfg)
    housekeeping.maybe_rebuild(session, cfg, state)
    snapshot = load_snapshot(session) or EMPTY_SNAPSHOT
    page = Site(cfg, snapshot, copenhagen_today(), pictures=images.pictures(session, snapshot))

    if not forms.origin_ok(header("Origin"), header("Referer"), cfg.site_netloc):
        return html(403, page.form_html(spec, {}, {forms.NON_FIELD: forms.WRONG_ORIGIN}))
    try:
        data = forms.parse_body(read_body(forms.MAX_FORM_BYTES))
    except (BodyTooLarge, forms.TooManyFields):
        return html(413, page.form_html(spec, {}, {forms.NON_FIELD: forms.SPAM}))
    cleaned, errors = forms.validate(spec, data)
    remote_addr = request.remote_addr or ""
    client = forms.client_key(cfg.stats_secret, remote_addr)
    if not errors and forms.rate_limited(session, client):
        return html(
            429, page.form_html(spec, cleaned, {forms.NON_FIELD: forms.RATE_LIMITED}), ("Retry-After", "3600")
        )
    if not errors and not forms.verify_turnstile(
        cfg.turnstile_secret_key, data.get(forms.TURNSTILE_FIELD, ""), remote_addr
    ):
        errors[forms.NON_FIELD] = forms.TURNSTILE_FAILED
    if errors:
        return html(400, page.form_html(spec, cleaned, errors))
    forms.store(session, spec, cleaned, client)
    return redirect(303, "/" + forms.SUCCESS_PATH)


# --- the visit beacon ---


@site.route("/s", methods=ALL_METHODS)
def beacon() -> Response:
    done = response(204, b"", "text/plain")
    if request.method != "POST":
        return method_not_allowed("POST")
    try:
        path = stats.beacon_path(read_body(stats.MAX_BEACON_BYTES))
    except BodyTooLarge:
        return done
    user_agent = header("User-Agent")
    if path is None or stats.is_bot(user_agent):
        return done
    try:
        cfg = _cfg()
        session = database()
        housekeeping.maybe_rebuild(session, cfg, make_variants=True)
        if stats.is_page(session, path):
            stats.record_view(
                session, cfg.stats_secret, copenhagen_today(), path, request.remote_addr or "", user_agent
            )
    except Exception:  # a lost page view must never show up as an error in the visitor's console
        log.exception("gahk_web: the visit beacon failed")
    return done


# --- the calendar feeds ---


@site.route("/kalender/<anypath:rest>", methods=ALL_METHODS)
def calendar(rest: str) -> Response:
    from . import kalender  # the only request that calls out, with urllib

    feed = kalender.token(request.path)
    if feed is None:
        return fallback()
    if request.method not in ("GET", "HEAD"):
        return method_not_allowed("GET, HEAD")
    return kalender.pass_through(_cfg().intern_url, feed, request.method)


# --- everything else ---


def _legacy_image(session: Session, path: str) -> str | None:
    """The /billeder/ address of an old picture address, from the snapshot's legacy_images map.

    The old site served these files through Django's ManifestStaticFilesStorage too, under names
    with a 12-hex-digit hash before the extension (gang.b8dc291a4e43.jpg), and pages elsewhere may
    still link those; without an exact match, the name without the hash is looked up."""
    legacy = session.scalar(select(SiteState.snapshot["legacy_images"]))
    if not isinstance(legacy, dict):
        return None
    for candidate in dict.fromkeys((path, _STATIC_HASH_RE.sub("", path))):
        for old, new in legacy.items():
            if old == candidate or unquote(old) == candidate:
                return str(new)
    return None


def _page_url(session: Session, path: str) -> str | None:
    url = session.scalar(select(GeneratedFile.url).where(GeneratedFile.rel_path == page_file(path)))
    return url or None


@site.route("/", methods=ALL_METHODS, defaults={"path": ""})
@site.route("/<path:path>", methods=ALL_METHODS)
def fallback(path: str = "") -> Response:
    """GET/HEAD for a path Apache had no file for."""
    if request.method not in ("GET", "HEAD"):
        return method_not_allowed("GET, HEAD")
    cfg = _cfg()
    path = request.path
    target = routing.intern_redirect(path)
    if target is not None:
        return redirect(308, cfg.intern_url + quote_path(target), query())
    target = routing.public_image_redirect(path)
    if target is not None:
        return redirect(301, quote_path(target), query())

    session = database()
    state = housekeeping.site_state(session)
    if not state.has_snapshot:
        return unavailable(cfg)
    housekeeping.maybe_rebuild(session, cfg, state)

    if path.startswith(routing.LEGACY_IMAGE_PREFIX):
        image = _legacy_image(session, path)
        return redirect(301, image) if image else not_found(cfg)

    bare = path.strip("/")
    if bare == "" or _PAGE_PATH_RE.match(bare):
        url = _page_url(session, bare)
        if url is not None:
            if url == path:
                # Apache should have served this file; serve it here rather than fail.
                file = cfg.public_html / page_file(bare)
                return serve_file(file) if file.is_file() else not_found(cfg)
            return redirect(301, quote_path(url), query())

    spec = forms.spec_for_post(path)
    if spec is not None and bare == spec.legacy_post_path:
        return redirect(301, "/" + spec.path, query())

    if PATH_RE.match(bare):
        new_path = session.scalar(select(SiteState.snapshot["redirects"][bare].astext))
        if isinstance(new_path, str):
            url = _page_url(session, new_path)
            if url is not None:
                return redirect(301, quote_path(url), query())
    return not_found(cfg)


# --- errors ---


def _is_api() -> bool:
    return request.path == "/api" or request.path.startswith("/api/")


def database_unavailable(exc: DatabaseUnavailable) -> Response:
    log.error("gahk_web: database unavailable: %s", exc)
    if _is_api():
        return response(503, b'{"error": "database unavailable"}', "application/json")
    return unavailable(_cfg())


def server_error(exc: InternalServerError) -> Response:
    """An exception no view caught. Flask has logged its traceback (to Apache's error log); the
    visitor sees only this."""
    return text(500, "Der skete en fejl. Prøv igen om lidt.")


def http_error(exc: HTTPException) -> Response:
    """What Werkzeug refuses before a view runs: an unusual method, a body over MAX_CONTENT_LENGTH."""
    status = exc.code or 400
    if _is_api():
        return json_response(status, {"error": (exc.name or "error").lower()})
    if status == 405:
        return method_not_allowed("GET, HEAD")
    if status == 404:
        return not_found(_cfg())
    return text(status, "Forespørgslen kunne ikke behandles.")


# --- the app ---


def create_app(cfg: Config, *, persistent: bool = False, runner: str = "cgi") -> Flask:
    """The site's Flask app. persistent=True keeps one database connection for request after
    request (the FastCGI process); otherwise each request opens and closes its own. runner names
    the way requests reach the app ("fastcgi" or "cgi"); GET /api/health reports it."""
    app = Flask("gahk_web", static_folder=None, template_folder=None)
    app.config.update(
        GAHK=cfg,
        GAHK_RUNNER=runner,
        # OPTIONS gets no automatic answer: every route takes every method and refuses the wrong ones.
        PROVIDE_AUTOMATIC_OPTIONS=False,
        # A backstop: each route reads at most its own, smaller limit.
        MAX_CONTENT_LENGTH=api.MAX_SNAPSHOT_BODY,
        SQLALCHEMY_DATABASE_URI=db.URL,
        SQLALCHEMY_ENGINE_OPTIONS=db.engine_options(cfg.database, persistent=persistent),
    )
    app.url_map.converters["anypath"] = AnyPath
    app.url_map.merge_slashes = False  # never redirect /a//b; old addresses are matched as sent
    orm.init_app(app)
    with app.app_context():
        app.extensions["gahk_db"] = db.Database(orm.engine, persistent=persistent)

    app.register_blueprint(api.bp)
    app.register_blueprint(site)
    app.register_error_handler(DatabaseUnavailable, database_unavailable)
    app.register_error_handler(InternalServerError, server_error)
    app.register_error_handler(HTTPException, http_error)

    @app.after_request
    def default_cache_control(result: Response) -> Response:
        if "Cache-Control" not in result.headers:
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.teardown_request
    def end_request(exc: BaseException | None) -> None:
        release_database()

    inner = app.wsgi_app

    def browser_path_app(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        use_browser_path(environ)
        return inner(environ, start_response)

    app.wsgi_app = browser_path_app  # type: ignore[method-assign]
    return app


def config_error_app(message: str) -> WsgiApp:
    """What answers while config.ini is missing or broken: 503 for everything, the reason in the log."""

    def application(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        log.error("gahk_web: %s", message)
        body = CONFIG_ERROR_TEXT.encode("utf-8")
        start_response(
            "503 Service Unavailable",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Retry-After", "300"),
                ("Cache-Control", "no-store"),
            ],
        )
        return [] if environ.get("REQUEST_METHOD") == "HEAD" else [body]

    return application
