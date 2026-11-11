"""A minimal request/response layer over WSGI (no framework: the CGI app must start fast)."""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote, urlsplit

HTML = "text/html; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
JSON = "application/json"


class BodyTooLarge(Exception):
    pass


def _wsgi_str(value: str) -> str:
    """PEP 3333 hands bytes over as latin-1 code points; the site's URLs are UTF-8."""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def request_path(environ: dict[str, Any]) -> tuple[str, str]:
    """(decoded path, raw query string) of the request as the browser sent it.

    Under Apache the .htaccess rewrites everything it cannot serve to /cgi-bin/gahk.cgi/<path>, so
    PATH_INFO is that path; REQUEST_URI (the original request line) is preferred when present
    because it is exactly what the client, and the signer on intern, used.
    """
    request_uri = environ.get("REQUEST_URI", "")
    if request_uri:
        parts = urlsplit(_wsgi_str(request_uri))
        path = unquote(parts.path, errors="replace")
        query = parts.query
    else:
        path = _wsgi_str(environ.get("PATH_INFO", ""))
        query = _wsgi_str(environ.get("QUERY_STRING", ""))
    return "/" + path.lstrip("/"), query


@dataclass
class Request:
    environ: dict[str, Any]
    method: str
    path: str
    query: str

    @classmethod
    def from_environ(cls, environ: dict[str, Any]) -> "Request":
        path, query = request_path(environ)
        return cls(environ, environ.get("REQUEST_METHOD", "GET").upper(), path, query)

    def header(self, name: str) -> str:
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return str(self.environ.get(key, ""))
        return _wsgi_str(str(self.environ.get("HTTP_" + key, "")))

    @property
    def remote_addr(self) -> str:
        return str(self.environ.get("REMOTE_ADDR", ""))

    def body(self, max_bytes: int) -> bytes:
        try:
            length = int(self.environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > max_bytes:
            raise BodyTooLarge
        if length <= 0:
            return b""
        data: bytes = self.environ["wsgi.input"].read(length)
        return data


@dataclass
class Response:
    status: int
    body: bytes | Iterable[bytes] = b""
    content_type: str = HTML
    headers: list[tuple[str, str]] = field(default_factory=list)

    def status_line(self) -> str:
        return f"{self.status} {HTTPStatus(self.status).phrase}"

    def header_list(self) -> list[tuple[str, str]]:
        headers = [("Content-Type", self.content_type), *self.headers]
        if isinstance(self.body, bytes):
            headers.append(("Content-Length", str(len(self.body))))
        if not any(name.lower() == "cache-control" for name, _value in headers):
            headers.append(("Cache-Control", "no-store"))
        return headers


def html(status: int, text: str, *headers: tuple[str, str]) -> Response:
    return Response(status, text.encode("utf-8"), HTML, list(headers))


def text(status: int, message: str, *headers: tuple[str, str]) -> Response:
    return Response(status, message.encode("utf-8"), TEXT, list(headers))


def json_response(status: int, data: object) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), JSON)


def redirect(status: int, location: str, query: str = "") -> Response:
    if query and "?" not in location:
        location = f"{location}?{query}"
    return Response(status, b"", TEXT, [("Location", location)])
