"""Development server: python -m gahk_web.devserver --port 8810 --public-html <dir>

Werkzeug's development server with its reloader (the app restarts when a .py file changes), in front
of a layer that emulates public_html/.htaccess (routing.py): the redirects to intern, existing files
and <path>/index.html are served as files, scripts, cgi-bin/ and dot-files are refused, and
everything else goes to the Flask app as the stub would run it: as gahk.fcgi does (one reused
database connection), or for /api/update, large bodies and with --cgi-only as gahk.cgi does. Site
assets come from the package's static/ folder when public_html has no static/ of its own (in
production the release installs them there). Development only: no TLS, no hardening.
"""

import argparse
import dataclasses
import mimetypes
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from flask import Response
from werkzeug.serving import is_running_from_reloader, run_simple
from werkzeug.wrappers import Request

from . import db, housekeeping, routing
from .app import create_app
from .config import Config, load_config
from .render import STATIC_DIR, quote_path
from .web import StartResponse, WsgiApp, redirect, response, text, use_browser_path


def _package_static(path: str) -> Path | None:
    path = routing.ALIASES.get(path, path)
    if not path.startswith("/static/"):
        return None
    root = STATIC_DIR.resolve()
    candidate = (STATIC_DIR / path[len("/static/") :]).resolve()
    if root in candidate.parents and candidate.is_file():
        return candidate
    return None


def _file_response(path: Path) -> Response:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "application/xml"):
        content_type += "; charset=utf-8"
    return response(200, path.read_bytes(), content_type, ("Cache-Control", "no-cache"))


def static_response(cfg: Config, method: str, path: str, query: str) -> Response | None:
    """What Apache would answer before the app, or None to let the app answer."""
    if routing.is_hidden(path) or routing.is_script(path) or routing.is_stub(path):
        return text(403, "Forbidden")
    target = routing.intern_redirect(path)
    if target is not None:
        return redirect(308, cfg.intern_url + quote_path(target), query)
    target = routing.public_image_redirect(path)
    if target is not None:
        return redirect(301, quote_path(target), query)
    if method in ("GET", "HEAD"):
        file = routing.static_file(cfg.public_html, path) or _package_static(path)
        if file is not None:
            return _file_response(file)
    return None


def make_dev_app(
    cfg: Config, app: WsgiApp | None = None, fastcgi_app: WsgiApp | None = None, *, cgi_only: bool = False
) -> WsgiApp:
    """The .htaccess emulation in front of the app: app answers what gahk.cgi gets, fastcgi_app
    (default: app) what gahk.fcgi gets."""
    cgi = app or create_app(cfg, runner="cgi")
    fastcgi = fastcgi_app or (cgi if app else create_app(cfg, persistent=True, runner="fastcgi"))

    def dev_app(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        use_browser_path(environ)
        request = Request(environ)
        answer = static_response(cfg, request.method, request.path, request.query_string.decode("latin-1"))
        if answer is not None:
            return answer(environ, start_response)
        stub = routing.stub_for(request.path, environ.get("CONTENT_LENGTH") or "", cgi_only=cgi_only)
        return (cgi if stub == routing.CGI_STUB else fastcgi)(environ, start_response)

    return dev_app


def prepare(cfg: Config) -> None:
    """Create the folders, apply migrations, and re-render if the code or the day changed."""
    cfg.public_html.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    engine = db.make_engine(cfg.database)
    try:
        applied = db.migrate(engine)
        if applied:
            print(f"Applied migrations: {', '.join(applied)}")
        with db.session_for(engine) as session:
            housekeeping.maybe_rebuild(session, cfg)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="gahk.dk development server (emulates .htaccess)")
    parser.add_argument("--port", type=int, default=8810)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--public-html", type=Path, help="folder to render into and serve (default: [paths] public_html)"
    )
    parser.add_argument(
        "--config", type=Path, help="config.ini (default: $GAHK_WEB_CONFIG or ~/gahk_web/config.ini)"
    )
    parser.add_argument("--no-reload", action="store_true", help="do not restart when a .py file changes")
    parser.add_argument("--cgi-only", action="store_true", help="as .htaccess's CGI-only switch")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.public_html:
        cfg = dataclasses.replace(cfg, public_html=args.public_html.resolve())
    reload = not args.no_reload
    if is_running_from_reloader() or not reload:  # the reloader's own process only watches files
        prepare(cfg)
        print(f"Serving {cfg.public_html} at http://{args.host}:{args.port}/ (base_url {cfg.base_url})")
    run_simple(args.host, args.port, make_dev_app(cfg, cgi_only=args.cgi_only), use_reloader=reload)


if __name__ == "__main__":
    main()
