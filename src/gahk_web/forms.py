"""The two optagelse forms: request a tour (rundvisning) and apply for a sublet (fremleje).

The fields, labels, limits and choices mirror intern's admissions forms and Application model exactly,
because intern pulls each submission from the outbox and validates it again as a form post. A valid
submission is stored here (the applications outbox) and answered with a redirect to the thank-you
page; an invalid one gets the form back with the errors and the values, status 400.

Protection, in the order it is checked:
- the request must come from a page on this site (Origin, else Referer, names the site's host);
- the honeypot field "website" must be empty (people never see it, bots fill it in);
- at most RATE_LIMIT successful submissions per hour per client (an HMAC of the IP, never the IP);
- Cloudflare Turnstile, when a secret is configured. A network error counts as a failed check
  (fail closed), as on intern today.
"""

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from . import db
from .clock import now_utc
from .wsgi import BodyTooLarge

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT = 5
TURNSTILE_FIELD = "cf-turnstile-response"
HONEYPOT_FIELD = "website"
RATE_LIMIT = 5
RATE_WINDOW = timedelta(hours=1)
MAX_FORM_BYTES = 64 * 1024
MAX_FORM_FIELDS = 100

REQUIRED = "Dette felt er påkrævet."
INVALID_EMAIL = "Indtast en gyldig e-mail-adresse."
INVALID_CHOICE = "Vælg en gyldig mulighed. Det valgte er ikke en af de tilgængelige muligheder."
NULL_CHARACTERS = "Null-tegn er ikke tilladt."
SPAM = "Formularen kunne ikke sendes. Prøv igen."
TURNSTILE_FAILED = "Captcha-verifikation fejlede. Prøv igen."
WRONG_ORIGIN = "Formularen skal sendes fra kollegiets hjemmeside. Gå tilbage til siden, og prøv igen."
RATE_LIMITED = "Du har sendt mange henvendelser på kort tid. Prøv igen om en time."
NON_FIELD = "__all__"


def too_long(limit: int, length: int) -> str:
    return f"Sørg for, at denne værdi højst har {limit} tegn (den har {length})."


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    widget: str  # "text", "email", "select" or "textarea"
    required: bool = False
    max_length: int | None = None
    choices: tuple[tuple[str, str], ...] = ()


GENDER_CHOICES = (("male", "Mand"), ("female", "Kvinde"), ("other", "Andet"))
HEARD_CHOICES = (
    ("", "— Vælg —"),
    ("sociale medier", "På sociale medier (Facebook eller Instagram)"),
    ("hjemmeside", "På en hjemmeside"),
    ("beboere", "Fra beboere der fortalte om kollegiet"),
    ("avis", "Annonce i avis"),
    ("anbefaling", "Anbefalet af en jeg kender"),
    ("plakat", "Set en plakat"),
    ("selv", "Selv fundet frem til det"),
)

_FULL_NAME = Field("full_name", "Fulde navn", "text", required=True, max_length=255)
_EMAIL = Field("email", "E-mail", "email", required=True, max_length=254)
_GENDER = Field("gender", "Køn", "select", required=True, choices=GENDER_CHOICES)
_AGE = Field("age", "Alder", "text", max_length=50)
_HEARD = Field("heard_about_us", "Hvor hørte du om os?", "select", required=True, choices=HEARD_CHOICES)


@dataclass(frozen=True)
class FormSpec:
    kind: str  # the application kind intern knows: "rundvisning" or "fremleje"
    path: str  # where the form page lives (and posts to)
    legacy_post_path: str  # the old POST address, still accepted
    title: str
    intro: str
    show_criteria: bool
    fields: tuple[Field, ...]


RUNDVISNING = FormSpec(
    kind="rundvisning",
    path="optagelse/ansoeg",
    legacy_post_path="optagelse/send_rundvisning",
    title="Anmod om rundvisning",
    intro=(
        "Udfyld formularen for at anmode om en rundvisning. Indstillingen kontakter dig, "
        "hvis der er udsigt til ledige værelser, og din profil passer."
    ),
    show_criteria=True,
    fields=(
        _FULL_NAME,
        _EMAIL,
        _GENDER,
        _AGE,
        Field("study_year", "Antal år studeret", "text", max_length=255),
        Field("year_left", "Studieår tilbage", "text", max_length=255),
        Field("university", "Universitet", "text", max_length=255),
        Field("field_of_study", "Studieretning", "text", max_length=255),
        _HEARD,
        Field("motivation", "Motivation", "textarea", max_length=500),
    ),
)

FREMLEJE = FormSpec(
    kind="fremleje",
    path="optagelse/fremlej",
    legacy_post_path="optagelse/send_fremleje",
    title="Ansøg om fremleje",
    intro="Skal du fremleje et værelse midlertidigt? Udfyld formularen, så vender vi tilbage til dig.",
    show_criteria=False,
    fields=(
        _FULL_NAME,
        _EMAIL,
        _GENDER,
        _AGE,
        Field("occupation", "Beskæftigelse", "text", max_length=255),
        _HEARD,
        # A TextField on intern: no length limit there either (the request size limit caps it).
        Field("motivation", "Motivation", "textarea"),
    ),
)

SPECS = (RUNDVISNING, FREMLEJE)
SUCCESS_PATH = "optagelse/ansoeg/success"


def spec_for_post(path: str) -> FormSpec | None:
    """The form a POST to this request path submits, if any."""
    bare = path.strip("/")
    return next((spec for spec in SPECS if bare in (spec.path, spec.legacy_post_path)), None)


# Django's EmailValidator, simplified to the same accept/refuse decisions for ordinary addresses.
_USER_RE = re.compile(
    r"^[-!#$%&'*+/=?^_`{}|~0-9a-z]+(\.[-!#$%&'*+/=?^_`{}|~0-9a-z]+)*$"
    r'|^"([\001-\010\013\014\016-\037!#-\[\]-\177]|\\[\001-\011\013\014\016-\177])*"$',
    re.IGNORECASE,
)
_DOMAIN_RE = re.compile(
    r"^((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+)(?:[a-z0-9-]{2,63}(?<!-))$", re.IGNORECASE
)


def valid_email(value: str) -> bool:
    if "@" not in value or len(value) > 320:
        return False
    user, domain = value.rsplit("@", 1)
    if not _USER_RE.match(user):
        return False
    if _DOMAIN_RE.match(domain):
        return True
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return bool(_DOMAIN_RE.match(ascii_domain))


def parse_body(body: bytes) -> dict[str, str]:
    """application/x-www-form-urlencoded → the first value of each field.

    Raises BodyTooLarge for more than MAX_FORM_FIELDS fields."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    try:
        parsed = parse_qs(text, keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
    except ValueError as exc:  # "Max number of fields exceeded"
        raise BodyTooLarge(str(exc)) from exc
    return {name: values[0] for name, values in parsed.items()}


def validate(spec: FormSpec, data: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(cleaned values, errors by field name). Values are stripped, as Django's CharField does."""
    cleaned: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field in spec.fields:
        value = str(data.get(field.name, "")).strip()
        cleaned[field.name] = value
        if "\x00" in value:
            errors[field.name] = NULL_CHARACTERS
        elif not value:
            if field.required:
                errors[field.name] = REQUIRED
        elif field.max_length is not None and len(value) > field.max_length:
            errors[field.name] = too_long(field.max_length, len(value))
        elif field.widget == "email" and not valid_email(value):
            errors[field.name] = INVALID_EMAIL
        elif field.choices and value not in {key for key, _label in field.choices}:
            errors[field.name] = INVALID_CHOICE
    if str(data.get(HONEYPOT_FIELD, "")).strip():
        errors[NON_FIELD] = SPAM
    return cleaned, errors


def origin_ok(origin: str, referer: str, site_netloc: str) -> bool:
    """The browser says the form was posted from a page on this site."""
    if origin:
        return origin != "null" and urlsplit(origin).netloc.lower() == site_netloc
    if referer:
        return urlsplit(referer).netloc.lower() == site_netloc
    return False


def client_key(secret: str, ip: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"form|{ip}".encode(), hashlib.sha256).hexdigest()


def rate_limited(con: db.Connection, client: str) -> bool:
    row = db.query_one(
        con,
        "SELECT count(*) AS n FROM form_rate_limit WHERE client = :client AND at > :since",
        client=client,
        since=now_utc() - RATE_WINDOW,
    )
    return bool(row and row["n"] >= RATE_LIMIT)


def verify_turnstile(secret: str, token: str, ip: str) -> bool:
    """Cloudflare's verdict on the widget token. No secret configured: no check (development)."""
    if not secret:
        return True
    if not token:
        return False
    data = urlencode({"secret": secret, "response": token, "remoteip": ip}).encode()
    try:
        # A fixed https URL, not user input.
        with urlopen(TURNSTILE_VERIFY_URL, data=data, timeout=TURNSTILE_TIMEOUT) as response:
            return bool(json.loads(response.read()).get("success"))
    except Exception:  # network, TLS, bad JSON: fail closed
        return False


def store(con: db.Connection, spec: FormSpec, cleaned: Mapping[str, str], client: str) -> str:
    """Queue the application for intern and count it against the client's hourly limit."""
    application_id = str(uuid.uuid4())
    now = now_utc()
    with db.transaction(con):
        db.execute(
            con,
            "INSERT INTO applications (id, kind, fields, submitted_at)"
            " VALUES (CAST(:id AS uuid), :kind, CAST(:fields AS jsonb), :at)",
            id=application_id,
            kind=spec.kind,
            fields=json.dumps(dict(cleaned), ensure_ascii=False),
            at=now,
        )
        db.execute(
            con, "INSERT INTO form_rate_limit (client, at) VALUES (:client, :at)", client=client, at=now
        )
    return application_id
