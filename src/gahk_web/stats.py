"""Visit statistics: the beacon every page sends (POST /s with the page path as the body).

Counted per Copenhagen day and page: views, and unique visitors. A visitor is HMAC(secret,
day|ip|user-agent): no IP is stored, and the hash changes every day, so visitors cannot be followed
from one day to the next. The hashes already counted live in visit_seen for two days, then go.
The whole site is counted once more under the path "*", so a visitor who reads five pages is one
visitor of the site that day.

The rows are an outbox: intern pulls them (GET /api/outbox) and acknowledges whole days.
Only addresses of generated pages count; 404s, bots and anything odd are dropped silently.
"""

import hashlib
import hmac
import re
from datetime import date
from urllib.parse import unquote

from . import db

SITE_PATH = "*"
MAX_BEACON_BYTES = 1024

BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|archiver|facebookexternalhit|embedly|preview|headless|lighthouse|pingdom|"
    r"uptime|monitor|curl|wget|python|httpclient|http-client|java/|go-http|okhttp|libwww|scrapy|"
    r"node-fetch|axios|phantomjs|selenium|playwright|puppeteer",
    re.IGNORECASE,
)


def is_bot(user_agent: str) -> bool:
    return not user_agent.strip() or bool(BOT_RE.search(user_agent))


def beacon_path(body: bytes) -> str | None:
    """The page path a beacon names ("" for the front page), or None when it is no page address."""
    try:
        text = unquote(body.decode("utf-8"), errors="strict").strip()
    except UnicodeDecodeError:
        return None
    if not text.startswith("/") or len(text) > 200:
        return None
    return text.strip("/")


def visitor_hash(secret: str, day: date, ip: str, user_agent: str) -> str:
    message = f"{day.isoformat()}|{ip}|{user_agent}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def is_page(con: db.Connection, path: str) -> bool:
    rel_path = f"{path}/index.html" if path else "index.html"
    return bool(
        db.query(con, "SELECT 1 FROM generated_files WHERE rel_path = :rel AND url IS NOT NULL", rel=rel_path)
    )


def record_view(con: db.Connection, secret: str, day: date, path: str, ip: str, user_agent: str) -> None:
    visitor = visitor_hash(secret, day, ip, user_agent)
    with db.transaction(con):
        for counted in (path, SITE_PATH):
            new = db.execute(
                con,
                "INSERT INTO visit_seen (day, path, visitor) VALUES (:day, :path, :visitor) ON CONFLICT DO NOTHING",
                day=day,
                path=counted,
                visitor=visitor,
            )
            db.execute(
                con,
                "INSERT INTO visits (day, path, views, visitors) VALUES (:day, :path, 1, :new)"
                " ON CONFLICT (day, path) DO UPDATE"
                " SET views = visits.views + 1, visitors = visits.visitors + EXCLUDED.visitors",
                day=day,
                path=counted,
                new=1 if new else 0,
            )
