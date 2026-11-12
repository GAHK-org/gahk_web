"""The routing rules of public_html/.htaccess, in Python.

Apache applies these in production before the CGI app ever runs. The development server applies the
same rules (so `python -m gahk_web.devserver` behaves like the webhost), and the CGI fallback applies
the redirects once more as a safety net. Keep this file and public_html/.htaccess in step; the tests
check that both name the same paths.
"""

import re
from pathlib import Path

# Paths of the old intranet and its tools, now on intern.gahk.dk. 308 keeps the method and body, so
# a form or API POST to an old address still arrives.
_INTERN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/(?:intern|nyintern)(?:/(.*))?$"), r"/\1"),
    (re.compile(r"^/((?:admin|django-admin|wiki)(?:/.*)?)$"), r"/\1"),
    (re.compile(r"^/((?:photo-album|media)/.*)$"), r"/\1"),
    (re.compile(r"^/health/?$"), "/health"),
    (re.compile(r"^/(optagelse/(?:listansoegninger|eksport))/?$"), r"/\1"),
    (re.compile(r"^/(optagelse/(?:showAnsoegning|setasreceived|kasser)/.+)$"), r"/\1"),
)

PUBLIC_IMAGE_PREFIX = "/public/image/"
LEGACY_IMAGE_PREFIX = "/static/legacy/image/"


def intern_redirect(path: str) -> str | None:
    """The path on intern.gahk.dk an old intranet address now lives at, or None."""
    for pattern, replacement in _INTERN_RULES:
        match = pattern.match(path)
        if match:
            return match.expand(replacement)
    return None


def public_image_redirect(path: str) -> str | None:
    """The oldest image addresses (/public/image/...) point at today's /static/legacy/image/..."""
    if path.startswith(PUBLIC_IMAGE_PREFIX) and len(path) > len(PUBLIC_IMAGE_PREFIX):
        return LEGACY_IMAGE_PREFIX + path[len(PUBLIC_IMAGE_PREFIX) :]
    return None


def is_hidden(path: str) -> bool:
    """Dot-files and dot-folders are never served (except /.well-known/, for certificates)."""
    segments = path.strip("/").split("/")
    return any(segment.startswith(".") for segment in segments) and not path.startswith("/.well-known/")


def is_stub(path: str) -> bool:
    """The cgi-bin folder: its stubs run only through the rewrites, never when asked for directly."""
    return path == "/cgi-bin" or path.startswith("/cgi-bin/")


def is_script(path: str) -> bool:
    """A CGI or FastCGI script outside cgi-bin/: Apache would run it, so it is refused."""
    return path.endswith((".cgi", ".fcgi"))


ALIASES = {
    "/favicon.ico": "/static/favicon.ico",
    "/apple-touch-icon.png": "/static/img/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png": "/static/img/apple-touch-icon.png",
}


def static_file(public_html: Path, path: str) -> Path | None:
    """The file Apache would serve for a GET of path: the file itself or <path>/index.html."""
    path = ALIASES.get(path, path)
    root = public_html.resolve()
    candidate = (public_html / path.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if candidate.is_file():
        return candidate
    index = candidate / "index.html"
    if index.is_file():
        return index
    return None
