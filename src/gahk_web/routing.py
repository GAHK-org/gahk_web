"""The routing rules of public_html/.htaccess, in Python.

Apache applies these in production before the app ever runs. The development server applies the
same rules (so `python -m gahk_web.devserver` behaves like the webhost), and the app's fallback
applies the redirects once more as a safety net. Keep this file and public_html/.htaccess in step;
the tests check that both name the same paths.
"""

import re
from pathlib import Path

# Paths of the old intranet and its tools, now on intern.gahk.dk. 308 keeps the method and body, so
# a form or API POST to an old address still arrives. /intern/x and /nyintern/x keep their prefix, so
# the link works whatever code intern.gahk.dk runs: the old serves /intern/…, the new sends /intern/x
# on to /x itself (308, the query kept).
_INTERN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/((?:intern|nyintern|admin|django-admin|wiki)(?:/.*)?)$"), r"/\1"),
    (re.compile(r"^/((?:photo-album|media)/.*)$"), r"/\1"),
    (re.compile(r"^/health(z?)/?$"), r"/health\1"),
    (re.compile(r"^/(optagelse/(?:listansoegninger|eksport))/?$"), r"/\1"),
    (re.compile(r"^/(optagelse/(?:showAnsoegning|setasreceived|kasser)/.+)$"), r"/\1"),
)

# The old intranet's static files, which the old gahk.dk served: its app manifest and icons, its
# build (/static/dist/), and the pictures its templates show. intern.gahk.dk serves them at the same
# path. Only when gahk.dk has no file of that name: its own static/icons/ come first.
_INTERN_STATIC_RE = re.compile(
    r"^/(static/(?:manifest\.json|icons/[^/]+|dist/.+|legacy/image/(?:elements|intern)/.+))$"
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


def intern_static_redirect(path: str) -> str | None:
    """The path on intern.gahk.dk of an old intranet static file gahk.dk has no file for, or None."""
    match = _INTERN_STATIC_RE.match(path)
    return "/" + match.group(1) if match else None


def legacy_image_key(path: str) -> str | None:
    """The key in the snapshot's legacy_images map for an old picture address, or None.

    The old site's pictures were at /static/legacy/image/..., and before that at /public/image/...
    (the same files). Both are only ever redirected, in one step, to the picture's /billeder/ address:
    gahk.dk never serves anything at an address with /legacy/ in it."""
    for prefix in (LEGACY_IMAGE_PREFIX, PUBLIC_IMAGE_PREFIX):
        if path.startswith(prefix) and len(path) > len(prefix):
            return LEGACY_IMAGE_PREFIX + path[len(prefix) :]
    return None


# The old site's logo (also its favicon), plain and with Django's content hash. Others may link it,
# so it redirects to the same logo among gahk.dk's own assets; it is never served at the old address.
OLD_LOGO_PATHS = (
    "/static/legacy/image/elements/lamassu.svg",
    "/static/legacy/image/elements/lamassu.b8dc291a4e43.svg",
)
LOGO_PATH = "/static/img/lamassu.svg"


def old_asset_redirect(path: str) -> str | None:
    """Where an old site asset gahk.dk still has now lives (301), or None."""
    return LOGO_PATH if path in OLD_LOGO_PATHS else None


def is_hidden(path: str) -> bool:
    """Dot-files and dot-folders are never served (except /.well-known/, for certificates)."""
    segments = path.strip("/").split("/")
    return any(segment.startswith(".") for segment in segments) and not path.startswith("/.well-known/")


# A name Apache would run (it maps handlers by every extension, case-insensitively: a.cgi.jpg too),
# as Python, CGI, FastCGI, or whatever the host maps PHP, Perl and server-side includes to.
_SCRIPT_RE = re.compile(r"(?:^|/)[^/]*\.(?:py|cgi|fcgi|php\d?|phtml|phar|pl|shtml)(?:[./]|$)", re.IGNORECASE)
# Content-Length with six digits or more (100 000 bytes and up): too large for mod_fcgid.
_LARGE_BODY_RE = re.compile(r"^[0-9]{6,}$")
UPDATE_PATH = "/api/update"
# Requests that may run longer than mod_fcgid waits (FcgidIOTimeout, 40 s by default): picture
# variants, then a render of the whole site.
LONG_PATHS = frozenset({"/api/publish", "/api/rebuild"})
CGI_STUB = "gahk.cgi"
FASTCGI_STUB = "gahk.fcgi"
# The site's fonts. Browsers fetch fonts with CORS, and intern's preview shows this site's HTML on
# intern.gahk.dk's origin, so Apache answers them to any origin (they are public files).
FONT_RE = re.compile(r"^/static/fonts/[^/]+$")


def is_stub(path: str) -> bool:
    """The cgi-bin folder: its stubs run only through the rewrites, never when asked for directly."""
    return path == "/cgi-bin" or path.startswith("/cgi-bin/")


def is_script(path: str) -> bool:
    """A Python, CGI, FastCGI, PHP, Perl or SSI script anywhere: the server would run it, so it is
    refused."""
    return bool(_SCRIPT_RE.search(path))


def is_font(path: str) -> bool:
    """A font file, which .htaccess answers with Access-Control-Allow-Origin: *."""
    return bool(FONT_RE.match(path))


def stub_for(path: str, content_length: str = "", *, cgi_only: bool = False) -> str:
    """Which stub .htaccess hands a request for the app to: gahk.cgi for /api/update (the updater
    must work when the release or FastCGI does not), for publishes and rebuilds (they may outlast
    mod_fcgid's FcgidIOTimeout), for bodies too large for mod_fcgid, and for everything while the
    operator's CGI-only switch is on; gahk.fcgi for the rest."""
    if path == UPDATE_PATH or path in LONG_PATHS or _LARGE_BODY_RE.match(content_length) or cgi_only:
        return CGI_STUB
    return FASTCGI_STUB


# Addresses .htaccess serves another file at (a rewrite, not a redirect): the icons browsers ask for
# at the root.
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
