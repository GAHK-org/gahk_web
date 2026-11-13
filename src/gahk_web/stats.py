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

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import GeneratedFile, Visit, VisitSeen

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


def is_page(session: Session, path: str) -> bool:
    rel_path = f"{path}/index.html" if path else "index.html"
    return bool(
        session.scalar(
            select(exists().where(GeneratedFile.rel_path == rel_path, GeneratedFile.url.is_not(None)))
        )
    )


def record_view(session: Session, secret: str, day: date, path: str, ip: str, user_agent: str) -> None:
    """Count the view, and the visitor when new today, for the page and for the site; one transaction."""
    visitor = visitor_hash(secret, day, ip, user_agent)
    for counted in (path, SITE_PATH):
        seen = insert(VisitSeen).values(day=day, path=counted, visitor=visitor)
        new = session.scalar(seen.on_conflict_do_nothing().returning(VisitSeen.visitor)) is not None
        view = insert(Visit).values(day=day, path=counted, views=1, visitors=1 if new else 0)
        session.execute(
            view.on_conflict_do_update(
                index_elements=[Visit.day, Visit.path],
                set_={Visit.views: Visit.views + 1, Visit.visitors: Visit.visitors + view.excluded.visitors},
            )
        )
    session.commit()
