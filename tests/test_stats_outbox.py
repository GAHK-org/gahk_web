from datetime import timedelta
from typing import Any

from conftest import Client, execute, publish, query
from sqlalchemy.orm import Session

from gahk_web.clock import copenhagen_today

BROWSER = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1"}


def beacon(client: Client, path: str, headers: dict[str, str] | None = None) -> int:
    return client.post("/s", path.encode(), headers=BROWSER if headers is None else headers).status


def visits(con: Session) -> dict[str, tuple[int, int]]:
    return {row["path"]: (row["views"], row["visitors"]) for row in query(con, "SELECT * FROM visits")}


def test_beacon_counts_views_and_unique_visitors(client: Client, con: Session) -> None:
    publish(client)
    other = Client(client.app, remote_addr="198.51.100.7")
    assert beacon(client, "/") == 204
    assert beacon(client, "/") == 204
    assert beacon(client, "/faciliteter") == 204
    assert beacon(other, "/faciliteter/") == 204
    assert beacon(client, "/optagelse/") == 204
    assert visits(con) == {"": (2, 1), "faciliteter": (2, 2), "optagelse": (1, 1), "*": (5, 2)}
    seen = query(con, "SELECT visitor FROM visit_seen")
    assert all("198.51" not in row["visitor"] for row in seen)


def test_beacon_ignores_bots_and_unknown_paths(client: Client, con: Session) -> None:
    publish(client)
    assert beacon(client, "/", {"User-Agent": "Googlebot/2.1"}) == 204
    assert beacon(client, "/", {}) == 204
    assert beacon(client, "/findes-ikke") == 204
    assert beacon(client, "not a path") == 204
    assert client.post("/s", b"/" * 5000, headers=BROWSER).status == 204
    assert visits(con) == {}
    assert client.get("/s").status == 405


def test_outbox_lists_and_acks(client: Client, con: Session) -> None:
    publish(client)
    today = copenhagen_today()
    yesterday = today - timedelta(days=1)
    execute(
        con,
        "INSERT INTO visits (day, path, views, visitors) VALUES (:d, '*', 7, 3), (:d, 'kontakt', 2, 1)",
        d=yesterday,
    )
    beacon(client, "/kontakt")
    execute(
        con,
        "INSERT INTO applications (id, kind, fields, submitted_at) VALUES"
        " ('11111111-1111-4111-8111-111111111111', 'sublet', '{\"full_name\": \"A\"}', now()),"
        " ('22222222-2222-4222-8222-222222222222', 'tour', '{\"full_name\": \"B\"}', now())",
    )
    data: dict[str, Any] = client.api("GET", "/api/outbox").json()
    assert [app["id"] for app in data["applications"]] == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    assert data["applications"][0]["fields"] == {"full_name": "A"}
    assert {(v["day"], v["path"]) for v in data["visits"]} == {
        (yesterday.isoformat(), "*"),
        (yesterday.isoformat(), "kontakt"),
        (today.isoformat(), "*"),
        (today.isoformat(), "kontakt"),
    }
    assert client.api("GET", "/api/status").json()["outbox"] == {"applications": 2, "visit_days": 2}

    result = client.api(
        "POST",
        "/api/outbox/ack",
        {"applications": ["11111111-1111-4111-8111-111111111111"], "visits_through": today.isoformat()},
    )
    assert result.json() == {"deleted_applications": 1, "deleted_visit_days": 1}
    left = client.api("GET", "/api/outbox").json()
    assert [app["id"] for app in left["applications"]] == ["22222222-2222-4222-8222-222222222222"]
    assert {v["day"] for v in left["visits"]} == {today.isoformat()}  # today stays: it is still counting


def test_ack_validates_its_input(client: Client) -> None:
    assert client.api("POST", "/api/outbox/ack", {"applications": ["nope"]}).status == 400
    assert client.api("POST", "/api/outbox/ack", {"visits_through": "2026-13-01"}).status == 400
    assert client.api("POST", "/api/outbox/ack", {"other": 1}).status == 400
    assert client.api("POST", "/api/outbox/ack", {}).json() == {
        "deleted_applications": 0,
        "deleted_visit_days": 0,
    }
