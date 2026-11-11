"""Development server: python -m gahk_web.devserver --port 8810 --public-html <dir>

It emulates public_html/.htaccess (routing.py): the redirects to intern, existing files and
<path>/index.html are served as files, and everything else goes to the CGI app. Site assets come
from the package's static/ folder when public_html has no static/ of its own (in production the
release installs them there). Development only: single-threaded, no TLS, no hardening.
"""

import argparse
import dataclasses
import mimetypes
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from wsgiref.simple_server import make_server

from . import db, housekeeping, routing
from .app import StartResponse, WsgiApp, make_app
from .config import Config, load_config
from .render import STATIC_DIR, quote_path
from .wsgi import Request, Response, redirect, text


def _package_static(path: str) -> Path | None:
    path = routing.ALIASES.get(path, path)
    if not path.startswith("/static/"):
        return None
    root = STATIC_DIR.resolve()
    candidate = (STATIC_DIR / path[len("/static/") :]).resolve()
    if root in candidate.parents and candidate.is_file():
        return candidate
    return None


def _file_response(path: Path, method: str) -> Response:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "application/xml"):
        content_type += "; charset=utf-8"
    body = b"" if method == "HEAD" else path.read_bytes()
    return Response(200, body, content_type, [("Cache-Control", "no-cache")])


def static_response(cfg: Config, request: Request) -> Response | None:
    """What Apache would answer before the CGI app, or None to let the app answer."""
    path = request.path
    if routing.is_hidden(path) or path.startswith("/cgi-bin/"):
        return text(403, "Forbidden")
    target = routing.intern_redirect(path)
    if target is not None:
        return redirect(308, cfg.intern_url + quote_path(target), request.query)
    target = routing.public_image_redirect(path)
    if target is not None:
        return redirect(301, quote_path(target), request.query)
    if request.method in ("GET", "HEAD"):
        file = routing.static_file(cfg.public_html, path) or _package_static(path)
        if file is not None:
            return _file_response(file, request.method)
    return None


def make_dev_app(cfg: Config, app: WsgiApp | None = None) -> WsgiApp:
    inner = app or make_app(cfg)

    def dev_app(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        request = Request.from_environ(environ)
        response = static_response(cfg, request)
        if response is None:
            return inner(environ, start_response)
        start_response(response.status_line(), response.header_list())
        return [response.body] if isinstance(response.body, bytes) else response.body

    return dev_app


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
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.public_html:
        cfg = dataclasses.replace(cfg, public_html=args.public_html.resolve())
    cfg.public_html.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    con = db.connect(cfg.database)
    try:
        applied = db.migrate(con)
        if applied:
            print(f"Applied migrations: {', '.join(applied)}")
        housekeeping.maybe_rebuild(con, cfg)
    finally:
        con.close()

    print("WARNING: development server only - never use it in production.", file=sys.stderr)
    print(f"Serving {cfg.public_html} at http://{args.host}:{args.port}/ (base_url {cfg.base_url})")
    with make_server(args.host, args.port, make_dev_app(cfg)) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
