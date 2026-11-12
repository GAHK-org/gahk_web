"""Render the published snapshot into static files in public_html.

Every page of the site is a pre-rendered file that Apache serves without starting Python:

    index.html                          the front page
    <path>/index.html                   each page (path has one or two segments)
    begivenheder/index.html             news and events
    optagelse/ansoeg/index.html         the tour form       (POSTs go to the CGI app)
    optagelse/fremlej/index.html        the sublet form
    optagelse/ansoeg/success/index.html the thank-you page
    404.html, 503.html, sitemap.xml, robots.txt, sw.js

Each file is written to a temporary name and moved into place, so a visitor never sees half a page,
and only when its content changed. The generated_files table lists what the last render wrote; a file
in that list that the new render does not produce (a deleted or moved page) is removed. Nothing else in
public_html is ever touched: static/, billeder/, cgi-bin/ and .htaccess belong to the release and the
image cache.

Callers hold the render advisory lock (db.LOCK_RENDER) around render_site().
"""

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import jinja2

from . import db
from .clock import copenhagen_today, now_utc, to_copenhagen
from .config import Config
from .forms import SPECS, SUCCESS_PATH, FormSpec
from .snapshot import Event, NewsItem, Page, Snapshot
from .version import code_version

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
# Written by the bundle build: {"css/site.css": "css/site.1a2b3c4d.css", ...}. Absent in a checkout.
STATIC_MANIFEST = STATIC_DIR / "static-manifest.json"

SITE_NAME = "G. A. Hagemanns Kollegium"
DEFAULT_DESCRIPTION = "Traditionsrigt kollegium på Østerbro, København"
BEGIVENHEDER_URL = "/begivenheder/"
HOME_EVENTS = 3
HOME_NEWS = 4
ARCHIVE_NEWS = 40
ARCHIVE_PAST_EVENTS = 20

# Files outside <path>/index.html that a render produces. Only these and page folders are ever deleted.
FIXED_FILES = ("404.html", "503.html", "sitemap.xml", "robots.txt", "sw.js")
_GENERATED_DIR_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*(?:/[a-z0-9]+(?:[-_][a-z0-9]+)*){0,2}$")
_NOT_PAGES = frozenset({"static", "billeder", "cgi-bin"})

MONTHS = (
    "januar",
    "februar",
    "marts",
    "april",
    "maj",
    "juni",
    "juli",
    "august",
    "september",
    "oktober",
    "november",
    "december",
)
MONTHS_SHORT = ("jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec")

EMPTY_SNAPSHOT: Snapshot = {
    "format": 1,
    "revision": "",
    "published_at": "",
    "site": {"front_page_id": 1, "default_background": ""},
    "legacy_images": {},
    "pages": [],
    "news": [],
    "events": [],
    "menu": [],
    "redirects": {},
    "images": [],
}

_MEDIA_ATTR_RE = re.compile(
    r"""((?:src|href|srcset|poster)\s*=\s*["']|url\(\s*["']?)/media/""", re.IGNORECASE
)


@cache
def _static_manifest() -> dict[str, str]:
    try:
        data = json.loads(STATIC_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def static_url(path: str) -> str:
    """The public URL of a site asset: its content-hashed name when the bundle has one."""
    return "/static/" + _static_manifest().get(path, path)


def to_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def to_local(value: str) -> datetime:
    return to_copenhagen(datetime.fromisoformat(value))


def da_date(value: str | date | datetime, style: str = "long") -> str:
    """Danish dates: long "5. december 2026", short "5. dec", medium "5. dec 2026", day "05",
    month "dec 2026", iso "2026-12-05". Strings are an ISO date or an ISO timestamp."""
    if isinstance(value, str):
        value = to_local(value) if "T" in value else to_date(value)
    if isinstance(value, datetime):
        value = value.date()
    formats = {
        "long": f"{value.day}. {MONTHS[value.month - 1]} {value.year}",
        "short": f"{value.day}. {MONTHS_SHORT[value.month - 1]}",
        "medium": f"{value.day}. {MONTHS_SHORT[value.month - 1]} {value.year}",
        "day": f"{value.day:02d}",
        "month": f"{MONTHS_SHORT[value.month - 1]} {value.year}",
        "iso": value.isoformat(),
    }
    return formats[style]


@cache
def environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals["static_url"] = static_url
    env.filters["da_date"] = da_date
    return env


@dataclass(frozen=True)
class SectionLink:
    url: str
    title: str
    current: bool


@dataclass
class Site:
    """The snapshot as the templates see it, for one render on one day."""

    cfg: Config
    snapshot: Snapshot
    today: date
    preview_media_base: str = ""  # set for POST /api/preview

    def __post_init__(self) -> None:
        front_page_id = self.snapshot["site"]["front_page_id"]
        self.pages_by_path = {
            page["path"]: page
            for page in self.snapshot["pages"]
            if page["path"] or page["id"] == front_page_id  # a pathless preview draft has no address
        }
        self.menu_urls = {item["url"] for item in self.snapshot["menu"]}

    @property
    def preview(self) -> bool:
        return bool(self.preview_media_base)

    # --- addresses ---

    def page_url(self, path: str) -> str:
        """Canonical address of a page: no trailing slash, unless the menu links it with one."""
        if not path:
            return "/"
        slashed = f"/{path}/"
        return slashed if slashed in self.menu_urls else f"/{path}"

    def absolute(self, url: str) -> str:
        return self.cfg.base_url + url

    def front_page(self) -> Page | None:
        return self.pages_by_path.get("")

    # --- lists ---

    def upcoming_events(self) -> list[Event]:
        events = [event for event in self.snapshot["events"] if to_date(event["starts_on"]) >= self.today]
        return sorted(events, key=lambda event: (event["starts_on"], event["id"]))

    def past_events(self) -> list[Event]:
        events = [event for event in self.snapshot["events"] if to_date(event["starts_on"]) < self.today]
        return sorted(events, key=lambda event: (event["starts_on"], event["id"]), reverse=True)

    def news(self) -> list[NewsItem]:
        return sorted(
            self.snapshot["news"], key=lambda item: (to_local(item["published_at"]), item["id"]), reverse=True
        )

    def section_nav(self, page: Page) -> tuple[str, list[SectionLink]]:
        """The sidebar of a section: the pages sharing the first path segment, when there are 2+."""
        if not page["path"]:
            return "", []
        section = page["path"].split("/", 1)[0]
        members = sorted(
            (
                other
                for other in self.snapshot["pages"]
                if other["path"] == section or other["path"].startswith(section + "/")
            ),
            key=lambda other: other["path"],
        )
        if len(members) < 2:
            return "", []
        title = next((other["title"] for other in members if other["path"] == section), page["title"])
        links = [
            SectionLink(self.page_url(other["path"]), other["title"], other["id"] == page["id"])
            for other in members
        ]
        return title, links

    # --- HTML ---

    def body(self, html: str) -> str:
        """A trusted body from the signed snapshot. In a preview, intern-only images load from intern."""
        if self.preview:
            return _MEDIA_ATTR_RE.sub(
                lambda match: match.group(1) + self.preview_media_base + "/media/", html
            )
        return html

    def image(self, url: str) -> str:
        if self.preview and url.startswith("/media/"):
            return self.preview_media_base + url
        return url

    def _context(self, **extra: object) -> dict[str, Any]:
        context: dict[str, Any] = {
            "site": self,
            "site_name": SITE_NAME,
            "base_url": self.cfg.base_url,
            "intern_url": self.cfg.intern_url,
            "https": self.cfg.is_https,
            "menu": self.snapshot["menu"],
            "title": "",
            "description": DEFAULT_DESCRIPTION,
            "canonical": "",
            "current_url": "",
            "og_image": "",
            "edit_url": self.cfg.intern_url + "/hjemmeside/",
            "noindex": self.preview,
            "beacon": not self.preview,
            "base_href": self.cfg.base_url + "/" if self.preview else "",
            "default_background": self.snapshot["site"]["default_background"],
        }
        context.update(extra)
        if context["canonical"] and not context["current_url"]:
            context["current_url"] = context["canonical"]
        if not context["og_image"]:
            background = context.get("background") or context["default_background"]
            context["og_image"] = (
                background
                if background.startswith("http")
                else self.absolute(background or static_url("img/apple-touch-icon.png"))
            )
        return context

    def _render(self, template: str, **extra: object) -> str:
        return environment().get_template(template).render(self._context(**extra))

    def page_html(self, page: Page) -> str:
        if not page["path"] and page["id"] == self.snapshot["site"]["front_page_id"]:
            return self.home_html(page)
        section_title, section_links = self.section_nav(page)
        url = self.page_url(page["path"])
        return self._render(
            "page.html",
            page=page,
            title=page["title"],
            description=page["description"] or DEFAULT_DESCRIPTION,
            canonical=self.absolute(url),
            current_url=url,
            background=self.image(page["background"]) or self.snapshot["site"]["default_background"],
            section_title=section_title,
            section_links=section_links,
            edit_url=f"{self.cfg.intern_url}/hjemmeside/sider/{page['id']}/",
        )

    def home_html(self, page: Page | None = None) -> str:
        page = page if page is not None else self.front_page()
        return self._render(
            "home.html",
            page=page,
            description=(page["description"] if page else "") or DEFAULT_DESCRIPTION,
            canonical=self.absolute("/"),
            current_url="/",
            background=self.image(page["background"]) if page else "",
            upcoming_events=self.upcoming_events()[:HOME_EVENTS],
            latest_news=self.news()[:HOME_NEWS],
            edit_url=f"{self.cfg.intern_url}/hjemmeside/sider/{page['id']}/"
            if page
            else self.cfg.intern_url + "/hjemmeside/",
        )

    def begivenheder_html(self) -> str:
        return self._render(
            "begivenheder.html",
            title="Nyheder & begivenheder",
            canonical=self.absolute(BEGIVENHEDER_URL),
            current_url=BEGIVENHEDER_URL,
            upcoming_events=self.upcoming_events(),
            past_events=self.past_events()[:ARCHIVE_PAST_EVENTS],
            news=self.news()[:ARCHIVE_NEWS],
        )

    def form_html(
        self,
        spec: FormSpec,
        values: Mapping[str, str] | None = None,
        errors: Mapping[str, str] | None = None,
    ) -> str:
        url = "/" + spec.path
        optagelse = self.pages_by_path.get("optagelse")
        return self._render(
            "form.html",
            spec=spec,
            title=spec.title,
            canonical=self.absolute(url),
            current_url=url,
            action=url,
            values=values or {},
            errors=errors or {},
            optagelse_url=self.page_url("optagelse") if optagelse else "",
            turnstile_site_key=self.cfg.turnstile_site_key,
        )

    def success_html(self) -> str:
        return self._render("success.html", title="Tak for din henvendelse", noindex=True)

    def error_html(self, status: int) -> str:
        return self._render(
            "error.html",
            status=status,
            title="Siden findes ikke" if status == 404 else "Siden opdateres",
            noindex=True,
        )

    def sitemap_xml(self) -> str:
        entries: list[tuple[str, str]] = []
        for page in sorted(self.snapshot["pages"], key=lambda page: page["path"]):
            entries.append((self.absolute(self.page_url(page["path"])), da_date(page["updated_at"], "iso")))
        entries.append((self.absolute(BEGIVENHEDER_URL), ""))
        entries.extend((self.absolute("/" + spec.path), "") for spec in SPECS)
        return environment().get_template("sitemap.xml").render(entries=entries)

    def robots_txt(self) -> str:
        return (
            environment()
            .get_template("robots.txt")
            .render(base_url=self.cfg.base_url, success_path=SUCCESS_PATH)
        )

    def files(self) -> dict[str, tuple[str, str | None]]:
        """Everything a render writes: relative path → (content, canonical URL path for HTML pages)."""
        out: dict[str, tuple[str, str | None]] = {}
        for page in self.snapshot["pages"]:
            rel = page_file(page["path"])
            out[rel] = (self.page_html(page), self.page_url(page["path"]))
        if "" not in self.pages_by_path:
            out["index.html"] = (self.home_html(None), "/")
        out[page_file("begivenheder")] = (self.begivenheder_html(), BEGIVENHEDER_URL)
        for spec in SPECS:
            out[page_file(spec.path)] = (self.form_html(spec), "/" + spec.path)
        out[page_file(SUCCESS_PATH)] = (self.success_html(), "/" + SUCCESS_PATH)
        out["404.html"] = (self.error_html(404), None)
        out["503.html"] = (self.error_html(503), None)
        out["sitemap.xml"] = (self.sitemap_xml(), None)
        out["robots.txt"] = (self.robots_txt(), None)
        out["sw.js"] = (environment().get_template("sw.js").render(), None)
        return out


def page_file(path: str) -> str:
    return f"{path}/index.html" if path else "index.html"


def is_generated_path(rel_path: str) -> bool:
    """Whether a path is one a render may produce, so it may also delete it."""
    if rel_path in FIXED_FILES or rel_path == "index.html":
        return True
    folder, _, name = rel_path.rpartition("/")
    return (
        name == "index.html"
        and bool(_GENERATED_DIR_RE.match(folder))
        and folder.split("/", 1)[0] not in _NOT_PAGES
    )


def write_if_changed(target: Path, content: bytes) -> bool:
    """Atomically replace target with content; False (and no write) when it already holds it."""
    with contextlib.suppress(OSError):
        if target.is_file() and target.read_bytes() == content:
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
    return True


def _remove(public_html: Path, rel_paths: Iterable[str]) -> None:
    root = public_html.resolve()
    for rel_path in rel_paths:
        if not is_generated_path(rel_path):
            continue
        target = public_html / rel_path
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        folder = target.parent
        while folder.resolve() != root and root in folder.resolve().parents:
            try:
                folder.rmdir()  # only when empty
            except OSError:
                break
            folder = folder.parent


@dataclass(frozen=True)
class RenderResult:
    pages_written: int  # files whose content changed (new or different)
    generated: int  # files the render produced in all


def render_site(
    con: db.Connection,
    cfg: Config,
    snapshot: Snapshot,
    today: date | None = None,
    *,
    store_snapshot: bool = False,
) -> RenderResult:
    """Write the whole site into public_html and record it. With store_snapshot, the snapshot becomes
    the cached one in the same transaction that records the render."""
    site = Site(cfg, snapshot, today or copenhagen_today())
    files = site.files()
    written = sum(
        write_if_changed(cfg.public_html / rel, content.encode("utf-8"))
        for rel, (content, _url) in files.items()
    )
    previous = {row["rel_path"] for row in db.query(con, "SELECT rel_path FROM generated_files")}
    _remove(cfg.public_html, sorted(previous - files.keys()))

    now = now_utc()
    with db.transaction(con):
        db.execute(con, "DELETE FROM generated_files")
        db.execute(
            con,
            "INSERT INTO generated_files (rel_path, url, rendered_at)"
            " SELECT rel_path, url, :at FROM unnest(CAST(:paths AS text[]), CAST(:urls AS text[])) AS f(rel_path, url)",
            paths=list(files),
            urls=[url for _content, url in files.values()],
            at=now,
        )
        db.execute(
            con,
            "UPDATE site_state SET rendered_at = :at, rendered_on = :day, rendered_version = :version",
            at=now,
            day=site.today,
            version=code_version(),
        )
        if store_snapshot:
            db.execute(
                con,
                "UPDATE site_state SET snapshot = CAST(:snapshot AS jsonb), revision = :revision,"
                " published_at = CAST(:published_at AS timestamptz), received_at = :at",
                snapshot=json.dumps(snapshot, ensure_ascii=False),
                revision=snapshot["revision"],
                published_at=snapshot["published_at"],
                at=now,
            )
    return RenderResult(pages_written=written, generated=len(files))


def load_snapshot(con: db.Connection) -> Snapshot | None:
    row = db.query_one(con, "SELECT snapshot FROM site_state WHERE id = 1")
    snapshot = row["snapshot"] if row else None
    if isinstance(snapshot, str):  # a driver that hands jsonb back as text
        snapshot = json.loads(snapshot)
    return cast(Snapshot | None, snapshot)


def preview_html(
    cfg: Config, snapshot: Snapshot | None, kind: str, item: dict[str, Any], media_base: str
) -> str:
    """One unsaved item rendered in the current site (menu, sidebar, lists). Never stored."""
    changed = cast(Snapshot, dict(snapshot or EMPTY_SNAPSHOT))
    today = copenhagen_today()
    if kind == "page":
        page = cast(Page, item)
        # A page with path "" that is not the front page is a draft without an address: it replaces
        # no other page, and it renders as an ordinary page.
        others = [
            old
            for old in changed["pages"]
            if old["id"] != page["id"] and (not page["path"] or old["path"] != page["path"])
        ]
        changed["pages"] = [*others, page]
        return Site(cfg, changed, today, preview_media_base=media_base).page_html(page)
    if kind == "news":
        news = cast(NewsItem, item)
        changed["news"] = [*(old for old in changed["news"] if old["id"] != news["id"]), news]
    else:
        event = cast(Event, item)
        changed["events"] = [*(old for old in changed["events"] if old["id"] != event["id"]), event]
    return Site(cfg, changed, today, preview_media_base=media_base).begivenheder_html()


def quote_path(url: str) -> str:
    """A URL path safe to put in a Location header."""
    return quote(url, safe="/:?=&%#~-._")
