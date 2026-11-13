"""The outbox intern drains: admission applications and visit counts.

GET /api/outbox lists what is waiting; POST /api/outbox/ack deletes what intern has stored. Today's
visit rows are never deleted by an ack, because they are still counting: intern upserts them on every
pull and acknowledges the day once it is over.
"""

import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from .models import Application, Visit

MAX_APPLICATIONS_PER_PULL = 500
MAX_ACK_IDS = 5000
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AckError(ValueError):
    pass


def pending(session: Session) -> dict[str, list[dict[str, Any]]]:
    applications = session.scalars(
        select(Application)
        .order_by(Application.submitted_at, Application.id)
        .limit(MAX_APPLICATIONS_PER_PULL)
    ).all()
    visits = session.scalars(select(Visit).order_by(Visit.day, Visit.path)).all()
    return {
        "applications": [
            {
                "id": str(application.id),
                "kind": application.kind,
                "submitted_at": application.submitted_at.isoformat(),
                "fields": application.fields,
            }
            for application in applications
        ],
        "visits": [
            {
                "day": visit.day.isoformat(),
                "path": visit.path,
                "views": visit.views,
                "visitors": visit.visitors,
            }
            for visit in visits
        ],
    }


def parse_ack(body: object) -> tuple[list[str], date | None]:
    if not isinstance(body, dict) or set(body) - {"applications", "visits_through"}:
        raise AckError('expected {"applications": [...], "visits_through": "YYYY-MM-DD"}')
    ids = body.get("applications", [])
    if not isinstance(ids, list) or len(ids) > MAX_ACK_IDS:
        raise AckError("applications: expected a list of ids")
    for application_id in ids:
        if not isinstance(application_id, str) or not UUID_RE.match(application_id):
            raise AckError(f"applications: {application_id!r} is not a uuid")
    through = body.get("visits_through")
    if through is None:
        return ids, None
    if not isinstance(through, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", through):
        raise AckError("visits_through: expected YYYY-MM-DD")
    try:
        return ids, date.fromisoformat(through)
    except ValueError as exc:
        raise AckError("visits_through: not a date") from exc


def ack(session: Session, ids: list[str], visits_through: date | None, today: date) -> dict[str, int]:
    """Delete what intern has stored, in one transaction."""
    deleted_applications = deleted_visit_days = 0
    if ids:
        deleted = session.scalars(
            delete(Application)
            .where(Application.id.in_([uuid.UUID(value) for value in ids]))
            .returning(Application.id)
        ).all()
        deleted_applications = len(deleted)
    if visits_through is not None:
        days = session.scalars(
            delete(Visit).where(Visit.day <= visits_through, Visit.day < today).returning(Visit.day)
        ).all()
        deleted_visit_days = len(set(days))
    session.commit()
    return {"deleted_applications": deleted_applications, "deleted_visit_days": deleted_visit_days}


def counts(session: Session) -> dict[str, int]:
    applications, visit_days = session.execute(
        select(
            select(func.count()).select_from(Application).scalar_subquery(),
            select(func.count(distinct(Visit.day))).scalar_subquery(),
        )
    ).one()
    return {"applications": applications, "visit_days": visit_days}
