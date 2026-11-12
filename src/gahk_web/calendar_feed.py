"""/kalender/<token>.ics: the residents' calendar feeds, passed through from intern.

Every feed URL handed out before the split reads https://gahk.dk/kalender/<token>.ics, and calendar
apps keep polling it for years. The feed lives on intern.gahk.dk now; this streams it through
unchanged (status, Content-Type, Cache-Control) and caches nothing. The token is the only credential
and is passed on as it is. It is the only request gahk.dk ever makes to intern, and it carries no
secret of gahk.dk's.
"""

import re
from collections.abc import Iterator
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Response

from .web import response, text

FEED_RE = re.compile(r"^/kalender/([A-Za-z0-9_-]{1,128})\.ics$")
TIMEOUT = 20
CHUNK = 64 * 1024
NO_STORE = "private, no-store"


def token(path: str) -> str | None:
    match = FEED_RE.match(path)
    return match.group(1) if match else None


def _stream(response: HTTPResponse | HTTPError) -> Iterator[bytes]:
    try:
        while chunk := response.read(CHUNK):
            yield chunk
    finally:
        response.close()


def pass_through(intern_url: str, feed_token: str, method: str) -> Response:
    url = f"{intern_url}/kalender/{feed_token}.ics"
    # intern_url is a configured http(s) origin (config.py checks it); the token matched FEED_RE.
    request_headers = {"User-Agent": "gahk.dk-calendar-feed", "Accept": "text/calendar, */*"}
    request = Request(url, method="GET", headers=request_headers)  # noqa: S310
    upstream: HTTPResponse | HTTPError
    try:
        upstream = urlopen(request, timeout=TIMEOUT)  # noqa: S310
    except HTTPError as error:
        upstream = error
    except (URLError, OSError, ValueError):
        return text(502, "Kalenderen kan ikke hentes lige nu. Prøv igen senere.", ("Cache-Control", NO_STORE))
    status = upstream.status if isinstance(upstream, HTTPResponse) else upstream.code
    headers = [
        ("Cache-Control", upstream.headers.get("Cache-Control") or NO_STORE),
        ("X-Robots-Tag", "noindex"),
        ("Referrer-Policy", "no-referrer"),
    ]
    content_type = upstream.headers.get("Content-Type") or "text/calendar; charset=utf-8"
    if method == "HEAD":
        upstream.close()
        return response(status, b"", content_type, *headers)
    return response(status, _stream(upstream), content_type, *headers)
