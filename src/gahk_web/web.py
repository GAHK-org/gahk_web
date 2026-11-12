"""HTTP helpers shared by the Flask app's views: the request as the browser sent it, bodies with
limits, responses with the site's headers, and the request's database connection."""

import json
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import unquote_to_bytes

from flask import Response, current_app, g, request
from werkzeug.routing import BaseConverter

from . import db

HTML = "text/html; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
JSON = "application/json"

PAGE_CACHE = ("Cache-Control", "public, max-age=60")
# Every method a route may see. Routes take them all and refuse the wrong ones themselves, so a
# signature is checked before the method (as intern expects) and OPTIONS gets no automatic answer.
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]

type StartResponse = Callable[..., Any]
type WsgiApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


class BodyTooLarge(Exception):
    pass


class DatabaseUnavailable(Exception):
    pass


class AnyPath(BaseConverter):
    """Like <path:…>, but also empty and with leading slashes (/api//x is still an API address)."""

    regex = ".*"
    part_isolating = False


# --- the request ---


def use_browser_path(environ: dict[str, Any]) -> None:
    """Route on the path the browser asked for, not on the stub's.

    Under Apache the .htaccess rewrites what it cannot serve to /cgi-bin/gahk.fcgi/<path> (or
    gahk.cgi), so SCRIPT_NAME names the stub and PATH_INFO is the rewritten, decoded path.
    REQUEST_URI is the original request line: exactly what the client, and the signer on intern,
    used. The app sees it as PATH_INFO (decoded, leading slashes collapsed) with an empty
    SCRIPT_NAME, so neither routing nor any URL the app builds ever contains the stub's path.
    """
    request_uri = environ.get("REQUEST_URI", "")
    if request_uri:
        raw_path, has_query, query_string = request_uri.partition("?")
        # WSGI strings carry the request's bytes as latin-1; the bytes stay as they are, and
        # Werkzeug decodes them as UTF-8 (U+FFFD for what is not UTF-8).
        path = unquote_to_bytes(raw_path.encode("latin-1", "replace")).decode("latin-1")
        if has_query:
            environ["QUERY_STRING"] = query_string
    else:
        path = environ.get("PATH_INFO", "")
    environ["PATH_INFO"] = "/" + path.lstrip("/")
    environ["SCRIPT_NAME"] = ""


def header(name: str) -> str:
    """A request header as text (WSGI hands its bytes over as latin-1; the site's are UTF-8)."""
    value = request.headers.get(name, "")
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def query() -> str:
    """The raw query string, for redirects that keep it."""
    return request.query_string.decode("latin-1")


def read_body(limit: int) -> bytes:
    """The request body. BodyTooLarge when Content-Length says it is over limit; no Content-Length
    (or a broken one) is an empty body."""
    length = request.content_length or 0
    if length > limit:
        raise BodyTooLarge
    if length <= 0:
        return b""
    return request.get_data(cache=True)


# --- responses ---


def response(
    status: int, content: bytes | Iterable[bytes], content_type: str, *headers: tuple[str, str]
) -> Response:
    result = Response(content, status=status, content_type=content_type)
    for name, value in headers:
        result.headers[name] = value
    return result


def html(status: int, text: str, *headers: tuple[str, str]) -> Response:
    return response(status, text.encode("utf-8"), HTML, *headers)


def text(status: int, message: str, *headers: tuple[str, str]) -> Response:
    return response(status, message.encode("utf-8"), TEXT, *headers)


def json_response(status: int, data: object) -> Response:
    return response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), JSON)


def redirect(status: int, location: str, query_string: str = "") -> Response:
    if query_string and "?" not in location:
        location = f"{location}?{query_string}"
    return response(status, b"", TEXT, ("Location", location))


def method_not_allowed(allowed: str) -> Response:
    return text(405, "Metoden er ikke tilladt.", ("Allow", allowed))


# --- the database ---


def database() -> db.Connection:
    """The request's database connection, opened on first use. In a persistent process it is the
    process's one connection, reused from request to request (db.SharedConnection)."""
    con: db.Connection | None = g.get("gahk_con")
    if con is None:
        shared: db.SharedConnection = current_app.extensions["gahk_db"]
        try:
            con = shared.acquire()
        except Exception as exc:
            raise DatabaseUnavailable(str(exc)) from exc
        g.gahk_con = con
    return con


def release_database(failed: bool) -> None:
    """End of the request: no transaction stays open, and a per-request connection is closed."""
    if g.pop("gahk_con", None) is not None:
        shared: db.SharedConnection = current_app.extensions["gahk_db"]
        shared.release(failed=failed)
