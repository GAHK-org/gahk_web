import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from conftest import Client, Result, execute, publish, query
from sqlalchemy.orm import Session

from gahk_web import forms
from gahk_web.app import create_app
from gahk_web.config import Config

ORIGIN = {"Origin": "https://gahk.dk", "Content-Type": "application/x-www-form-urlencoded"}
VALID = {
    "full_name": "Anna Jensen",
    "email": "anna@example.dk",
    "gender": "female",
    "age": "22",
    "study_year": "2",
    "year_left": "3",
    "university": "DTU",
    "field_of_study": "Byggeteknologi",
    "heard_about_us": "residents",
    "motivation": "Jeg vil gerne bo i et fællesskab.",
    "website": "",
}


def post(client: Client, path: str, data: dict[str, str], headers: dict[str, str] | None = None) -> Result:
    return client.post(path, urlencode(data).encode(), headers=ORIGIN if headers is None else headers)


def applications(con: Session) -> list[dict[str, Any]]:
    return query(con, "SELECT kind, fields FROM applications ORDER BY submitted_at")


def test_form_pages_render_the_fields(client: Client, cfg: Config) -> None:
    publish(client)
    html = (cfg.public_html / "optagelse/ansoeg/index.html").read_text()
    assert '<form method="post" action="/optagelse/ansoeg"' in html
    assert '<label for="id_study_year">Antal år studeret</label>' in html
    assert 'name="motivation" id="id_motivation" rows="5" maxlength="500"' in html
    assert '<option value="social_media">På sociale medier (Facebook eller Instagram)</option>' in html
    assert 'type="hidden" name="website"' in html
    assert "Udfyld formularen for at anmode om en rundvisning." in html
    assert 'href="/optagelse/"' in html  # "Før du søger" points at the optagelse page
    assert "challenges.cloudflare.com" not in html
    sublet = (cfg.public_html / "optagelse/fremlej/index.html").read_text()
    assert '<label for="id_occupation">Beskæftigelse</label>' in sublet
    assert 'name="motivation" id="id_motivation" rows="5">' in sublet  # no length cap


def test_valid_submission_is_queued_and_redirects(client: Client, con: Session) -> None:
    publish(client)
    result = post(client, "/optagelse/ansoeg", VALID)
    assert result.status == 303
    assert result.headers["Location"] == "/optagelse/ansoeg/success"
    rows = applications(con)
    assert len(rows) == 1
    assert rows[0]["kind"] == "tour"
    assert rows[0]["fields"]["full_name"] == "Anna Jensen"
    assert "website" not in rows[0]["fields"]


def test_legacy_post_addresses_still_work(client: Client, con: Session) -> None:
    publish(client)
    sublet = {
        k: v for k, v in VALID.items() if k not in ("study_year", "year_left", "university", "field_of_study")
    }
    assert post(client, "/optagelse/send_fremleje", sublet | {"occupation": "Studerende"}).status == 303
    assert post(client, "/optagelse/send_rundvisning", VALID).status == 303
    assert [row["kind"] for row in applications(con)] == ["sublet", "tour"]


def test_missing_and_invalid_fields_rerender_with_values(client: Client, con: Session) -> None:
    publish(client)
    data = VALID | {
        "full_name": "  ",
        "email": "ikke-en-mail",
        "gender": "x",
        "motivation": "m" * 501,
        "age": "<b>22</b>",
    }
    result = post(client, "/optagelse/ansoeg", data)
    assert result.status == 400
    assert "Dette felt er påkrævet." in result.text
    assert forms.INVALID_EMAIL in result.text
    assert "højst har 500 tegn (den har 501)" in result.text
    assert 'value="&lt;b&gt;22&lt;/b&gt;"' in result.text
    assert 'value="ikke-en-mail"' in result.text
    assert applications(con) == []


def test_heard_about_us_codes_are_the_protocols() -> None:
    """Every answer is an English code from docs/protocol.md's table, which maps each one to what
    intern stores; the table lists no code the form lacks."""
    table = (Path(__file__).parents[1] / "docs" / "protocol.md").read_text(encoding="utf-8")
    documented = dict(re.findall(r"^  \| `([a-z_]+)` +\| `([^`]+)` +\|$", table, re.MULTILINE))
    codes = [code for code, _label in forms.HEARD_CHOICES if code]
    assert codes == ["social_media", "website", "residents", "newspaper", "recommendation", "poster", "self"]
    assert list(documented) == codes
    assert len(set(documented.values())) == len(codes)  # one stored value per code


def test_heard_about_us_refuses_the_stored_values() -> None:
    """intern's stored Danish values are not codes: the form takes only the codes."""
    _cleaned, errors = forms.validate(forms.TOUR, VALID | {"heard_about_us": "beboere"})
    assert errors == {"heard_about_us": forms.INVALID_CHOICE}


def test_motivation_of_exactly_500_is_fine() -> None:
    _cleaned, errors = forms.validate(forms.TOUR, VALID | {"motivation": "m" * 500})
    assert errors == {}
    _cleaned, errors = forms.validate(forms.SUBLET, VALID | {"motivation": "m" * 5000})
    assert errors == {}


def test_honeypot(client: Client, con: Session) -> None:
    publish(client)
    result = post(client, "/optagelse/ansoeg", VALID | {"website": "http://spam.example"})
    assert result.status == 400
    assert forms.SPAM in result.text
    assert applications(con) == []


def test_too_many_fields_answers_with_the_form(client: Client, con: Session) -> None:
    publish(client)
    junk = {f"x{number}": "1" for number in range(120)}
    result = post(client, "/optagelse/ansoeg", VALID | junk)
    assert result.status == 413
    assert '<form method="post" action="/optagelse/ansoeg"' in result.text
    assert forms.SPAM in result.text
    assert applications(con) == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Referer": "https://evil.example/optagelse/ansoeg"},
        {},
    ],
)
def test_origin_must_be_the_site(client: Client, con: Session, headers: dict[str, str]) -> None:
    publish(client)
    result = post(client, "/optagelse/ansoeg", VALID, headers=headers)
    assert result.status == 403
    assert applications(con) == []


def test_referer_alone_is_enough(client: Client, con: Session) -> None:
    publish(client)
    assert (
        post(
            client, "/optagelse/ansoeg", VALID, headers={"Referer": "https://gahk.dk/optagelse/ansoeg"}
        ).status
        == 303
    )


def test_rate_limit_five_per_hour(client: Client, con: Session) -> None:
    publish(client)
    for _ in range(5):
        assert post(client, "/optagelse/ansoeg", VALID).status == 303
    result = post(client, "/optagelse/ansoeg", VALID)
    assert result.status == 429
    assert forms.RATE_LIMITED in result.text
    other = Client(client.app, remote_addr="192.0.2.99")
    assert post(other, "/optagelse/ansoeg", VALID).status == 303
    stored = query(con, "SELECT client FROM form_rate_limit")
    assert all("192.0.2" not in row["client"] for row in stored)
    execute(con, "UPDATE form_rate_limit SET at = at - interval '61 minutes'")
    assert post(client, "/optagelse/ansoeg", VALID).status == 303


def test_no_snapshot_means_no_form(client: Client) -> None:
    assert post(client, "/optagelse/ansoeg", VALID).status == 503


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@pytest.fixture
def turnstile_client(cfg: Config, con: Session) -> Client:
    from dataclasses import replace

    return Client(create_app(replace(cfg, turnstile_site_key="site-key", turnstile_secret_key="secret")))


def test_turnstile_success(turnstile_client: Client, con: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    publish(turnstile_client)
    calls: list[bytes] = []

    def fake_urlopen(url: str, data: bytes, timeout: float) -> FakeResponse:
        calls.append(data)
        return FakeResponse(json.dumps({"success": True}).encode())

    monkeypatch.setattr(forms, "urlopen", fake_urlopen)
    result = post(turnstile_client, "/optagelse/ansoeg", VALID | {"cf-turnstile-response": "token"})
    assert result.status == 303
    assert b"secret=secret" in calls[0] and b"response=token" in calls[0]


def test_turnstile_fails_closed(
    turnstile_client: Client, con: Session, monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    publish(turnstile_client)
    assert (
        "challenges.cloudflare.com/turnstile" in (cfg.public_html / "optagelse/ansoeg/index.html").read_text()
    )

    def unreachable(url: str, data: bytes, timeout: float) -> FakeResponse:
        raise OSError("network down")

    monkeypatch.setattr(forms, "urlopen", unreachable)
    result = post(turnstile_client, "/optagelse/ansoeg", VALID | {"cf-turnstile-response": "token"})
    assert result.status == 400
    assert forms.TURNSTILE_FAILED in result.text

    monkeypatch.setattr(forms, "urlopen", lambda url, data, timeout: FakeResponse(b'{"success": false}'))
    assert (
        post(turnstile_client, "/optagelse/ansoeg", VALID | {"cf-turnstile-response": "token"}).status == 400
    )
    assert post(turnstile_client, "/optagelse/ansoeg", VALID).status == 400  # no token at all
    assert applications(con) == []
