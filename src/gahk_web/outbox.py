"""The outbox intern drains: optagelse applications and visit counts.

GET /api/outbox lists what is waiting; POST /api/outbox/ack deletes what intern has stored. Today's
visit rows are never deleted by an ack, because they are still counting: intern upserts them on every
pull and acknowledges the day once it is over.
"""

import re
from datetime import date
from typing import Any

from . import db

MAX_APPLICATIONS_PER_PULL = 500
MAX_ACK_IDS = 5000
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AckError(ValueError):
    pass


def pending(con: db.Connection) -> dict[str, list[dict[str, Any]]]:
    applications = db.query(
        con,
        "SELECT id::text AS id, kind, submitted_at, fields FROM applications"
        " ORDER BY submitted_at, id LIMIT :limit",
        limit=MAX_APPLICATIONS_PER_PULL,
    )
    visits = db.query(con, "SELECT day, path, views, visitors FROM visits ORDER BY day, path")
    return {
        "applications": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "submitted_at": row["submitted_at"].isoformat(),
                "fields": row["fields"],
            }
            for row in applications
        ],
        "visits": [
            {
                "day": row["day"].isoformat(),
                "path": row["path"],
                "views": row["views"],
                "visitors": row["visitors"],
            }
            for row in visits
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


def ack(con: db.Connection, ids: list[str], visits_through: date | None, today: date) -> dict[str, int]:
    with db.transaction(con):
        deleted_applications = (
            db.execute(con, "DELETE FROM applications WHERE id = ANY(CAST(:ids AS uuid[]))", ids=ids)
            if ids
            else 0
        )
        deleted_visit_days = 0
        if visits_through is not None:
            rows = db.query(
                con,
                "DELETE FROM visits WHERE day <= :through AND day < :today RETURNING day",
                through=visits_through,
                today=today,
            )
            deleted_visit_days = len({row["day"] for row in rows})
    return {"deleted_applications": deleted_applications, "deleted_visit_days": deleted_visit_days}


def counts(con: db.Connection) -> dict[str, int]:
    row = db.query_one(
        con,
        "SELECT (SELECT count(*) FROM applications) AS applications,"
        " (SELECT count(DISTINCT day) FROM visits) AS visit_days",
    )
    return {"applications": int(row["applications"]), "visit_days": int(row["visit_days"])} if row else {}
