"""The content snapshot intern publishes, and its strict validation.

The snapshot is signed and intern sanitised its HTML bodies; they are kept as they are here and
sanitised once more when they are rendered (bodies.py). Everything else is checked hard: types,
sizes, path syntax, reserved paths, image URLs. Unknown keys are refused too, so a contract drift
between intern and gahk.dk shows up as a failed publish on intern instead of silently missing
content. A refused snapshot changes nothing: the site keeps serving the previous one.
"""

import re
import unicodedata
from datetime import date, datetime
from typing import Any, TypedDict, cast
from urllib.parse import quote, unquote

FORMAT = 1

PATH_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*(?:/[a-z0-9]+(?:[-_][a-z0-9]+)*)?$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# /billeder/<sha16>/<name>. The name is UTF-8 text: words of letters and digits ([^\W_]: any
# script, Danish letters included) joined by single "-" or "_", then one "." and the extension;
# billeder_match() checks the rest.
# It never starts with "_" or ".", so it cannot be one of gahk.dk's own variants (_960w.webp).
BILLEDER_RE = re.compile(r"/billeder/([0-9a-f]{16})/([^\W_]+(?:[-_.][^\W_]+)*)")
# Letters (lower-case or caseless) and decimal digits: what intern keeps in a name. "²", "½" and
# combining marks are not; upper-case letters are not either (the names are lower-case).
_NAME_CATEGORIES = frozenset({"Ll", "Lm", "Lo", "Nd"})
# A file name may have 255 bytes on the webhost's file system; intern cuts a stem at 120.
MAX_NAME_BYTES = 255
# Apache maps handlers and types by every extension in a name (a.php.jpg may run as PHP, a.cgi.jpg
# as CGI), so a name has exactly one: its image extension. intern never writes a "." in a stem.
LEGACY_IMAGE_PREFIX = "/static/legacy/image/"
# A picture in intern's library, which only a preview may show: /media/cms/<path>. intern sends the
# name percent-encoded (quote(name, safe="/")), so older names with other letters arrive as %XX.
MEDIA_RE = re.compile(r"^/media/cms(?:/(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)+$")
ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
URL_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f\"'<>\\`]")

RESERVED_FIRST_SEGMENTS = frozenset(
    {
        "admin",
        "api",
        "begivenheder",
        "billeder",
        "cgi-bin",
        "django-admin",
        "favicon.ico",
        "health",
        "intern",
        "kalender",
        "media",
        "nyintern",
        "photo-album",
        "robots.txt",
        "s",
        "sitemap.xml",
        "static",
        "sw.js",
        "wiki",
        "404",
    }
)
RESERVED_PATHS = frozenset(
    {
        "optagelse/ansoeg",
        "optagelse/fremlej",
        "optagelse/send_rundvisning",
        "optagelse/send_fremleje",
        "optagelse/listansoegninger",
        "optagelse/eksport",
    }
)

IMAGE_TYPES = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/gif": (".gif",),
    "image/webp": (".webp",),
}
MAX_IMAGE_BYTES = 15 * 1024 * 1024

MAX_PAGES = 1000
MAX_NEWS = 5000
MAX_EVENTS = 5000
MAX_MENU = 30
MAX_REDIRECTS = 5000
MAX_IMAGES = 10000
MAX_LEGACY_IMAGES = 5000
MAX_TITLE = 255
MAX_LABEL = 60
MAX_DESCRIPTION = 500
MAX_BODY = 1_000_000
MAX_URL = 500


class Page(TypedDict):
    id: int
    path: str
    title: str
    body: str
    background: str
    description: str
    updated_at: str


class NewsItem(TypedDict):
    id: int
    title: str
    body: str
    published_at: str


class Event(TypedDict):
    id: int
    title: str
    description: str
    starts_on: str


class MenuItem(TypedDict):
    label: str
    url: str


class Image(TypedDict):
    url: str
    sha256: str
    content_type: str
    size: int


class Site(TypedDict):
    front_page_id: int
    default_background: str


class Snapshot(TypedDict):
    format: int
    revision: str
    published_at: str
    site: Site
    pages: list[Page]
    news: list[NewsItem]
    events: list[Event]
    menu: list[MenuItem]
    redirects: dict[str, str]
    legacy_images: dict[str, str]
    images: list[Image]


class SnapshotError(ValueError):
    pass


def is_reserved(path: str) -> bool:
    return path.split("/", 1)[0] in RESERVED_FIRST_SEGMENTS or path in RESERVED_PATHS


def _keys(obj: object, where: str, required: set[str]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise SnapshotError(f"{where}: expected an object")
    missing = required - obj.keys()
    extra = obj.keys() - required
    if missing:
        raise SnapshotError(f"{where}: missing {', '.join(sorted(missing))}")
    if extra:
        raise SnapshotError(f"{where}: unknown {', '.join(sorted(map(str, extra)))}")
    return obj


def _int(value: object, where: str) -> int:
    # bool is an int subclass; True is not an id.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 2**31 - 1:
        raise SnapshotError(f"{where}: expected a positive integer")
    return value


def _str(
    value: object, where: str, max_length: int, *, required: bool = False, single_line: bool = False
) -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{where}: expected a string")
    if len(value) > max_length:
        raise SnapshotError(f"{where}: longer than {max_length} characters")
    if required and not value.strip():
        raise SnapshotError(f"{where}: must not be empty")
    if "\x00" in value or (single_line and CONTROL_RE.search(value)):
        raise SnapshotError(f"{where}: contains control characters")
    return value


def _list(value: object, where: str, max_items: int) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotError(f"{where}: expected a list")
    if len(value) > max_items:
        raise SnapshotError(f"{where}: more than {max_items} items")
    return value


def _datetime(value: object, where: str) -> str:
    text = _str(value, where, 40, single_line=True)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotError(f"{where}: not an ISO 8601 timestamp") from exc
    if moment.tzinfo is None:
        raise SnapshotError(f"{where}: timestamp without a UTC offset")
    return text


def _date(value: object, where: str) -> str:
    text = _str(value, where, 10, single_line=True)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise SnapshotError(f"{where}: expected YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotError(f"{where}: not a date") from exc
    return text


def page_path(value: object, where: str) -> str:
    text = _str(value, where, 80, single_line=True)
    if text and not PATH_RE.match(text):
        raise SnapshotError(f"{where}: {text!r} is not a valid page path")
    return text


def legacy_image_url(text: str) -> bool:
    """An old public image URL as today's site serves it: /static/legacy/image/<rest>."""
    rest = text.removeprefix(LEGACY_IMAGE_PREFIX)
    return (
        text.startswith(LEGACY_IMAGE_PREFIX)
        and bool(rest)
        and not CONTROL_RE.search(text)
        and not any(ch in text for ch in "\"'<>\\?#")
        and ".." not in rest.split("/")
        and "//" not in rest
    )


def background(value: object, where: str, image_urls: set[str] | None, *, allow_media: bool = False) -> str:
    """ "" (no picture) or /billeder/<sha16>/<name>, which the snapshot must ship.

    A preview may also point at intern's own library (/media/...), which is not copied here yet.
    """
    text = _str(value, where, 255, single_line=True)
    if not text:
        return text
    if billeder_match(text) and (image_urls is None or text in image_urls):
        return text
    if allow_media and is_media_url(text):
        return text
    raise SnapshotError(f"{where}: {text!r} is not an image this snapshot ships")


def billeder_match(url: str) -> re.Match[str] | None:
    """The match of a /billeder/<sha16>/<name> URL (group 1 the hash, group 2 the name), or None.

    The name is NFC-normalized and lower-case: the same letters always make the same file and the
    same URL ("å" as one character, never "a" plus a combining ring)."""
    match = BILLEDER_RE.fullmatch(url)
    if match is None:
        return None
    name = match.group(2)
    if (
        unicodedata.normalize("NFC", name) != name
        or any(unicodedata.category(char) not in _NAME_CATEGORIES for char in name if char not in "-_.")
        or len(name.encode("utf-8")) > MAX_NAME_BYTES
        or name.count(".") > 1
    ):
        return None
    return match


def billeder_href(url: str) -> str:
    """An image URL as pages and redirects write it: a /billeder/ name percent-encoded from UTF-8
    (/billeder/<sha16>/tovtr%C3%A6kning.jpg), so neither a srcset, a CSS url() nor a Location
    header depends on what letters it has. Other URLs (intern's /media/, already encoded) as given."""
    return quote(url, safe="/") if url.startswith("/billeder/") else url


def is_media_url(text: str) -> bool:
    """A /media/cms/ address on intern, with no segment that is (or decodes to) "." or "..", a
    slash, a backslash or a control character."""
    if not MEDIA_RE.match(text):
        return False
    for segment in text.split("/")[3:]:
        name = unquote(segment, errors="replace")
        if name in (".", "..") or "/" in name or "\\" in name or CONTROL_RE.search(name):
            return False
    return True


def validate_page(obj: object, where: str, image_urls: set[str] | None, *, preview: bool = False) -> Page:
    item = _keys(obj, where, {"id", "path", "title", "body", "background", "description", "updated_at"})
    _int(item["id"], f"{where}.id")
    page_path(item["path"], f"{where}.path")
    _str(item["title"], f"{where}.title", MAX_TITLE, required=True, single_line=True)
    _str(item["body"], f"{where}.body", MAX_BODY)
    background(item["background"], f"{where}.background", image_urls, allow_media=preview)
    _str(item["description"], f"{where}.description", MAX_DESCRIPTION, single_line=True)
    _datetime(item["updated_at"], f"{where}.updated_at")
    return cast(Page, item)


def validate_news(obj: object, where: str) -> NewsItem:
    item = _keys(obj, where, {"id", "title", "body", "published_at"})
    _int(item["id"], f"{where}.id")
    _str(item["title"], f"{where}.title", MAX_TITLE, required=True, single_line=True)
    _str(item["body"], f"{where}.body", MAX_BODY)
    _datetime(item["published_at"], f"{where}.published_at")
    return cast(NewsItem, item)


def validate_event(obj: object, where: str) -> Event:
    item = _keys(obj, where, {"id", "title", "description", "starts_on"})
    _int(item["id"], f"{where}.id")
    _str(item["title"], f"{where}.title", MAX_TITLE, required=True, single_line=True)
    _str(item["description"], f"{where}.description", MAX_BODY)
    _date(item["starts_on"], f"{where}.starts_on")
    return cast(Event, item)


def menu_url(value: object, where: str) -> str:
    text = _str(value, where, MAX_URL, required=True, single_line=True)
    if URL_FORBIDDEN_RE.search(text):
        raise SnapshotError(f"{where}: {text!r} contains characters a link may not have")
    site_path = text.startswith("/") and not text.startswith("//")
    if not (site_path or re.match(r"^https?://[^/]", text) or text.startswith("mailto:")):
        raise SnapshotError(f"{where}: {text!r} must be a site path or an http(s) URL")
    return text


def validate_image(obj: object, where: str) -> Image:
    item = _keys(obj, where, {"url", "sha256", "content_type", "size"})
    url = _str(item["url"], f"{where}.url", 200, single_line=True)
    sha = _str(item["sha256"], f"{where}.sha256", 64, single_line=True)
    if not HEX64_RE.match(sha):
        raise SnapshotError(f"{where}.sha256: expected 64 lower-case hex digits")
    match = billeder_match(url)
    if not match:
        raise SnapshotError(f"{where}.url: {url!r} is not /billeder/<16 hex>/<name>")
    if match.group(1) != sha[:16]:
        raise SnapshotError(f"{where}.url: the hash in the URL does not match sha256")
    content_type = item["content_type"]
    if content_type not in IMAGE_TYPES:
        raise SnapshotError(f"{where}.content_type: {content_type!r} is not an allowed image type")
    if not match.group(2).endswith(IMAGE_TYPES[content_type]):
        raise SnapshotError(f"{where}.url: the file extension does not match {content_type}")
    size = item["size"]
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_IMAGE_BYTES:
        raise SnapshotError(f"{where}.size: expected 1..{MAX_IMAGE_BYTES} bytes")
    return cast(Image, item)


def validate(obj: object) -> Snapshot:
    """Return the snapshot unchanged if it is valid; raise SnapshotError naming the first problem."""
    snap = _keys(
        obj,
        "snapshot",
        {
            "format",
            "revision",
            "published_at",
            "site",
            "legacy_images",
            "pages",
            "news",
            "events",
            "menu",
            "redirects",
            "images",
        },
    )
    if snap["format"] != FORMAT or isinstance(snap["format"], bool):
        raise SnapshotError(f"snapshot.format: expected {FORMAT}")
    if not isinstance(snap["revision"], str) or not HEX64_RE.match(snap["revision"]):
        raise SnapshotError("snapshot.revision: expected 64 lower-case hex digits")
    _datetime(snap["published_at"], "snapshot.published_at")
    site = _keys(snap["site"], "snapshot.site", {"front_page_id", "default_background"})
    front_page_id = _int(site["front_page_id"], "site.front_page_id")

    images = [
        validate_image(item, f"images[{i}]")
        for i, item in enumerate(_list(snap["images"], "images", MAX_IMAGES))
    ]
    image_urls = {image["url"] for image in images}
    if len(image_urls) != len(images):
        raise SnapshotError("images: duplicate url")
    background(site["default_background"], "site.default_background", image_urls)

    legacy = snap["legacy_images"]
    if not isinstance(legacy, dict):
        raise SnapshotError("legacy_images: expected an object")
    if len(legacy) > MAX_LEGACY_IMAGES:
        raise SnapshotError(f"legacy_images: more than {MAX_LEGACY_IMAGES}")
    for old, new in legacy.items():
        if len(old) > MAX_URL or not legacy_image_url(old):
            raise SnapshotError(f"legacy_images: {old!r} is not a /static/legacy/image/ URL")
        if not isinstance(new, str) or new not in image_urls:
            raise SnapshotError(f"legacy_images[{old!r}]: {new!r} is not an image this snapshot ships")

    pages = [
        validate_page(item, f"pages[{i}]", image_urls)
        for i, item in enumerate(_list(snap["pages"], "pages", MAX_PAGES))
    ]
    ids: set[int] = set()
    paths: set[str] = set()
    for i, page in enumerate(pages):
        where = f"pages[{i}]"
        if page["id"] in ids:
            raise SnapshotError(f"{where}.id: duplicate id {page['id']}")
        if page["path"] in paths:
            raise SnapshotError(f"{where}.path: duplicate path {page['path']!r}")
        ids.add(page["id"])
        paths.add(page["path"])
        if (page["path"] == "") != (page["id"] == front_page_id):
            raise SnapshotError(
                f"{where}.path: only the front page (and every front page) has the empty path"
            )
        if page["path"] and is_reserved(page["path"]):
            raise SnapshotError(f"{where}.path: {page['path']!r} is reserved")

    news_ids = [
        validate_news(news, f"news[{i}]")["id"]
        for i, news in enumerate(_list(snap["news"], "news", MAX_NEWS))
    ]
    if len(set(news_ids)) != len(news_ids):
        raise SnapshotError("news: duplicate id")
    event_ids = [
        validate_event(event, f"events[{i}]")["id"]
        for i, event in enumerate(_list(snap["events"], "events", MAX_EVENTS))
    ]
    if len(set(event_ids)) != len(event_ids):
        raise SnapshotError("events: duplicate id")
    for i, entry in enumerate(_list(snap["menu"], "menu", MAX_MENU)):
        item = _keys(entry, f"menu[{i}]", {"label", "url"})
        _str(item["label"], f"menu[{i}].label", MAX_LABEL, required=True, single_line=True)
        menu_url(item["url"], f"menu[{i}].url")

    redirects = snap["redirects"]
    if not isinstance(redirects, dict):
        raise SnapshotError("redirects: expected an object")
    if len(redirects) > MAX_REDIRECTS:
        raise SnapshotError(f"redirects: more than {MAX_REDIRECTS}")
    for old, new in redirects.items():
        if not old or not PATH_RE.match(old):
            raise SnapshotError(f"redirects: {old!r} is not a valid page path")
        if is_reserved(old):
            raise SnapshotError(f"redirects: {old!r} is reserved")
        if not isinstance(new, str) or new not in paths:
            raise SnapshotError(f"redirects[{old!r}]: target {new!r} is not a page in this snapshot")
    return cast(Snapshot, snap)


PREVIEW_KINDS = ("page", "news", "event")


class Preview(TypedDict):
    kind: str
    item: dict[str, Any]
    media_base: str


def validate_preview(obj: object) -> Preview:
    """A POST /api/preview body: one unsaved item, shaped as in the snapshot, never stored.

    Its images may still live only on intern (/media/...); media_base (an origin like
    https://intern.gahk.dk) is put in front of those so the editor's browser loads them from there.
    """
    request = _keys(obj, "preview", {"kind", "item", "media_base"})
    kind = request["kind"]
    if kind not in PREVIEW_KINDS:
        raise SnapshotError(f"preview.kind: expected one of {', '.join(PREVIEW_KINDS)}")
    media_base = request["media_base"]
    if not isinstance(media_base, str) or not ORIGIN_RE.match(media_base.rstrip("/")):
        raise SnapshotError("preview.media_base: expected an origin like https://intern.gahk.dk")
    if kind == "page":
        validate_page(request["item"], "preview.item", None, preview=True)
    elif kind == "news":
        validate_news(request["item"], "preview.item")
    else:
        validate_event(request["item"], "preview.item")
    return Preview(kind=kind, item=request["item"], media_base=media_base.rstrip("/"))
