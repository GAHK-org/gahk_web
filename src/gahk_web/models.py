"""The webhost's tables, as typed SQLAlchemy 2 models.

Everything here is a cache or an outbox; intern.gahk.dk holds the truth. The schema itself is made
by the Alembic revisions in gahk_web/migrations: a change here needs a new revision (`task migration
-- "what changed"` writes one from the difference), and a test fails while the two disagree.

Constraint and index names follow PostgreSQL's own defaults (images_pkey, images_width_check,
form_rate_limit_client_at), so the names a revision uses are the names the database shows.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "ix": "%(table_name)s_%(column_0_N_name)s",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    # A check constraint is named after what it checks: CheckConstraint(..., name="width").
    "ck": "%(table_name)s_%(constraint_name)s_check",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # PostgreSQL's types: text (never varchar), timestamptz, and a JSON column holds jsonb.
    type_annotation_map = {
        str: Text(),
        datetime: DateTime(timezone=True),
        date: Date(),
        int: Integer(),
        uuid.UUID: Uuid(),
    }


class SiteState(Base):
    """The table's one row (id 1): the cached snapshot and when the site was last rendered from it."""

    __tablename__ = "site_state"
    __table_args__ = (CheckConstraint("id = 1", name="id"),)

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False, server_default=text("1")
    )
    # none_as_null: Python's None is SQL NULL here (no snapshot yet), never the JSON value null.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    revision: Mapped[str | None]
    published_at: Mapped[datetime | None]
    received_at: Mapped[datetime | None]
    rendered_at: Mapped[datetime | None]
    rendered_on: Mapped[date | None]
    rendered_version: Mapped[str | None]


class Image(Base):
    """Image bytes received with PUT /api/images/<sha256>, kept as blobs under state_dir/images, and
    their responsive WebP variants (images.py): the upright size, which variant widths exist as
    public_html/images/<sha16>/_<width>w.webp, and how often making them was tried.
    variant_widths None: not made yet. Empty: the image gets none (variant_note says why), and pages
    show the original."""

    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("width > 0", name="width"),
        CheckConstraint("height > 0", name="height"),
    )

    sha256: Mapped[str] = mapped_column(primary_key=True)
    content_type: Mapped[str]
    size: Mapped[int]
    stored_at: Mapped[datetime] = mapped_column(server_default=func.now())
    width: Mapped[int | None]
    height: Mapped[int | None]
    variant_widths: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    variant_note: Mapped[str | None]
    variant_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))


class ImageFile(Base):
    """The public copies of the blobs: public_html/images/<sha16>/<name>. The name comes from the
    snapshot, so one blob can have several public names."""

    __tablename__ = "image_files"

    rel_path: Mapped[str] = mapped_column(primary_key=True)
    sha256: Mapped[str]


class GeneratedFile(Base):
    """Every file the renderer wrote into public_html, so a page that disappears is deleted and
    nothing else ever is. url is the canonical address of an HTML page (None for other files): the
    fallback redirects the other slash form of a page there."""

    __tablename__ = "generated_files"

    rel_path: Mapped[str] = mapped_column(primary_key=True)
    url: Mapped[str | None]
    rendered_at: Mapped[datetime]


class Application(Base):
    """Outbox: optagelse form submissions until intern pulls and acknowledges them."""

    __tablename__ = "applications"
    __table_args__ = (CheckConstraint("kind IN ('tour', 'sublet')", name="kind"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str]
    fields: Mapped[dict[str, str]] = mapped_column(JSONB)
    submitted_at: Mapped[datetime]


class FormRateLimit(Base):
    """Form submissions per client (HMAC of the IP) for the hourly rate limit; purged after a day.

    The table has no primary key (two submissions may share one); the ORM identifies a row by both
    columns."""

    __tablename__ = "form_rate_limit"
    __table_args__ = (Index(None, "client", "at"),)

    client: Mapped[str] = mapped_column()
    at: Mapped[datetime] = mapped_column()

    __mapper_args__ = {"primary_key": [client, at]}


class Visit(Base):
    """Outbox: views and unique visitors per day and path ('*' = the whole site) until intern acks them."""

    __tablename__ = "visits"

    day: Mapped[date] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(primary_key=True)
    views: Mapped[int] = mapped_column(server_default=text("0"))
    visitors: Mapped[int] = mapped_column(server_default=text("0"))


class VisitSeen(Base):
    """Which visitor hashes were already counted per day and path; purged after two days."""

    __tablename__ = "visit_seen"

    day: Mapped[date] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(primary_key=True)
    visitor: Mapped[str] = mapped_column(primary_key=True)
