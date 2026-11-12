"""The WSGI application behind cgi-bin/gahk.cgi: whatever the static files cannot answer.

    /api/*                              the signed sync API (api.py)
    POST /optagelse/ansoeg, /optagelse/send_rundvisning,
         /optagelse/fremlej, /optagelse/send_fremleje        the optagelse forms (forms.py)
    POST /s                             the visit beacon (stats.py)
    GET  /kalender/<token>.ics          the calendar pass-through to intern (kalender.py)
    anything else (GET/HEAD)            the fallback: old addresses → 301/308, the other slash form
                                        of a page → 301, else the 404 page; no snapshot yet → 503

A database connection is opened only when a request needs one; it is the webhost's own cache/outbox,
never intern's.
"""

import contextlib
import mimetypes
import re
import sys
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import unquote

from . import api, db, forms, housekeeping, images, routing, stats
from .clock import copenhagen_today
from .config import Config, ConfigError, load_config
from .render import EMPTY_SNAPSHOT, Site, load_snapshot, page_file, quote_path
from .snapshot import PATH_RE
from .wsgi import BodyTooLarge, Request, Response, html, redirect, text

StartResponse = Callable[..., Any]
WsgiApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]

PAGE_CACHE = ("Cache-Control", "public, max-age=60")
# The shape of a generated page's folder (pages have two segments at most, optagelse/ansoeg/success three).
_PAGE_PATH_RE = re.compile(r"^[a-z0-9_-]+(?:/[a-z0-9_-]+){0,2}$")


class DatabaseUnavailable(Exception):
    pass


class Context:
    """One request: configuration plus a lazily opened database connection."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._con: db.Connection | None = None

    def db(self) -> db.Connection:
        if self._con is None:
            try:
                self._con = db.connect(self.cfg.database)
            except Exception as exc:
                raise DatabaseUnavailable(str(exc)) from exc
        return self._con

    def close(self) -> None:
        if self._con is not None:
            with contextlib.suppress(Exception):  # the connection is gone anyway
                self._con.close()
            self._con = None


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


def serve_file(path: Path, method: str) -> Response:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "application/xml"):
        content_type += "; charset=utf-8"
    body = b"" if method == "HEAD" else path.read_bytes()
    return Response(200, body, content_type, [PAGE_CACHE])


def method_not_allowed(allowed: str) -> Response:
    return text(405, "Metoden er ikke tilladt.", ("Allow", allowed))


# --- handlers ---


def handle_form(request: Request, ctx: Context, spec: forms.FormSpec) -> Response:
    cfg = ctx.cfg
    con = ctx.db()
    state = housekeeping.site_state(con)
    if not state.has_snapshot:
        return unavailable(cfg)
    housekeeping.maybe_rebuild(con, cfg, state)
    snapshot = load_snapshot(con) or EMPTY_SNAPSHOT
    site = Site(cfg, snapshot, copenhagen_today(), pictures=images.pictures(con, snapshot))

    if not forms.origin_ok(request.header("Origin"), request.header("Referer"), cfg.site_netloc):
        return html(403, site.form_html(spec, {}, {forms.NON_FIELD: forms.WRONG_ORIGIN}))
    try:
        data = forms.parse_body(request.body(forms.MAX_FORM_BYTES))
    except BodyTooLarge:
        return html(413, site.form_html(spec, {}, {forms.NON_FIELD: forms.SPAM}))
    cleaned, errors = forms.validate(spec, data)
    client = forms.client_key(cfg.stats_secret, request.remote_addr)
    if not errors and forms.rate_limited(con, client):
        return html(
            429, site.form_html(spec, cleaned, {forms.NON_FIELD: forms.RATE_LIMITED}), ("Retry-After", "3600")
        )
    if not errors and not forms.verify_turnstile(
        cfg.turnstile_secret_key, data.get(forms.TURNSTILE_FIELD, ""), request.remote_addr
    ):
        errors[forms.NON_FIELD] = forms.TURNSTILE_FAILED
    if errors:
        return html(400, site.form_html(spec, cleaned, errors))
    forms.store(con, spec, cleaned, client)
    return redirect(303, "/" + forms.SUCCESS_PATH)


def handle_beacon(request: Request, ctx: Context, errors: TextIO) -> Response:
    done = Response(204, b"", "text/plain")
    if request.method != "POST":
        return method_not_allowed("POST")
    try:
        path = stats.beacon_path(request.body(stats.MAX_BEACON_BYTES))
    except BodyTooLarge:
        return done
    user_agent = request.header("User-Agent")
    if path is None or stats.is_bot(user_agent):
        return done
    try:
        con = ctx.db()
        housekeeping.maybe_rebuild(con, ctx.cfg, make_variants=True)
        if stats.is_page(con, path):
            stats.record_view(
                con, ctx.cfg.stats_secret, copenhagen_today(), path, request.remote_addr, user_agent
            )
    except Exception:  # a lost page view must never show up as an error in the visitor's console
        traceback.print_exc(file=errors)
    return done


def _legacy_image(con: db.Connection, path: str) -> str | None:
    row = db.query_one(con, "SELECT snapshot->'legacy_images' AS legacy FROM site_state WHERE id = 1")
    legacy = row["legacy"] if row else None
    if not isinstance(legacy, dict):
        return None
    for old, new in legacy.items():
        if old == path or unquote(old) == path:
            return str(new)
    return None


def _page_url(con: db.Connection, path: str) -> str | None:
    row = db.query_one(con, "SELECT url FROM generated_files WHERE rel_path = :rel", rel=page_file(path))
    return str(row["url"]) if row and row["url"] else None


def fallback(request: Request, ctx: Context) -> Response:
    """GET/HEAD for a path Apache had no file for."""
    cfg = ctx.cfg
    path = request.path
    target = routing.intern_redirect(path)
    if target is not None:
        return redirect(308, cfg.intern_url + quote_path(target), request.query)
    target = routing.public_image_redirect(path)
    if target is not None:
        return redirect(301, quote_path(target), request.query)

    con = ctx.db()
    state = housekeeping.site_state(con)
    if not state.has_snapshot:
        return unavailable(cfg)
    housekeeping.maybe_rebuild(con, cfg, state)

    if path.startswith(routing.LEGACY_IMAGE_PREFIX):
        image = _legacy_image(con, path)
        return redirect(301, image) if image else not_found(cfg)

    bare = path.strip("/")
    if bare == "" or _PAGE_PATH_RE.match(bare):
        url = _page_url(con, bare)
        if url is not None:
            if url == path:
                # Apache should have served this file; serve it here rather than fail.
                file = cfg.public_html / page_file(bare)
                return serve_file(file, request.method) if file.is_file() else not_found(cfg)
            return redirect(301, quote_path(url), request.query)

    spec = forms.spec_for_post(path)
    if spec is not None and bare == spec.legacy_post_path:
        return redirect(301, "/" + spec.path, request.query)

    if PATH_RE.match(bare):
        row = db.query_one(
            con,
            "SELECT snapshot->'redirects'->>CAST(:old AS text) AS target FROM site_state WHERE id = 1",
            old=bare,
        )
        new_path = row["target"] if row else None
        if isinstance(new_path, str):
            url = _page_url(con, new_path)
            if url is not None:
                return redirect(301, quote_path(url), request.query)
    return not_found(cfg)


def dispatch(request: Request, ctx: Context, errors: TextIO) -> Response:
    path = request.path
    if path == "/api" or path.startswith("/api/"):
        return api.handle(request, ctx.cfg, ctx.db)
    if request.method == "POST":
        spec = forms.spec_for_post(path)
        if spec is not None:
            return handle_form(request, ctx, spec)
    if path == "/s":
        return handle_beacon(request, ctx, errors)
    if path.startswith("/kalender/"):
        from . import kalender  # the only request that calls out (urllib is slow to import)

        feed = kalender.token(path)
        if feed is not None:
            if request.method not in ("GET", "HEAD"):
                return method_not_allowed("GET, HEAD")
            return kalender.pass_through(ctx.cfg.intern_url, feed, request.method)
    if request.method not in ("GET", "HEAD"):
        return method_not_allowed("GET, HEAD")
    return fallback(request, ctx)


def make_app(cfg: Config | None = None) -> WsgiApp:
    """The WSGI app. Without cfg, the config file is read on the first request."""
    loaded: list[Config] = [cfg] if cfg is not None else []

    def application(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        errors: TextIO = environ.get("wsgi.errors") or sys.stderr
        request = Request.from_environ(environ)
        response: Response
        ctx: Context | None = None
        try:
            if not loaded:
                loaded.append(load_config())
            ctx = Context(loaded[0])
            response = dispatch(request, ctx, errors)
        except ConfigError as exc:
            print(f"gahk_web: {exc}", file=errors)
            response = text(503, "Siden er midlertidigt utilgængelig.", ("Retry-After", "300"))
        except DatabaseUnavailable as exc:
            print(f"gahk_web: database unavailable: {exc}", file=errors)
            if request.path.startswith("/api/"):
                response = Response(503, b'{"error": "database unavailable"}', "application/json")
            else:
                response = unavailable(loaded[0])
        except Exception:
            traceback.print_exc(file=errors)
            response = text(500, "Der skete en fejl. Prøv igen om lidt.")
        finally:
            if ctx is not None:
                ctx.close()
        start_response(response.status_line(), response.header_list())
        if request.method == "HEAD":
            return [b""]
        if isinstance(response.body, bytes):
            return [response.body]
        return response.body

    return application
