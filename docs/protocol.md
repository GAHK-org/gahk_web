# The gahk.dk sync protocol

This is the contract between **intern.gahk.dk** (gahk_intern, the source of all content) and
**gahk.dk** (gahk_web, the public site on the webhost). Both repos implement it: gahk_intern's
`cms` client sends the requests, and gahk_web's `api.py`, `snapshot.py` and the cgi-bin stub answer
them. Change both sides together, and keep each change backward compatible, so either side can be
released first.

## Principles

- **intern starts every exchange.** gahk.dk never calls an authenticated intern endpoint and holds no
  credentials for intern's database or its S3 storage. Its only request to intern is the
  unauthenticated calendar pass-through (`/kalender/<token>.ics`).
- **gahk.dk holds no durable data.** It keeps a cache (the last snapshot, images, rendered pages) and
  an outbox (form submissions, visit counts) until intern pulls it.
- **What intern receives from gahk.dk is untrusted input.** It validates applications and visit
  counts as if they were form posts.
- **Bodies are sanitised on both sides.** intern sanitises page, news and event bodies before it
  stores them; gahk.dk cleans them again with the same allowlist (nh3) whenever it renders them, as
  defence in depth. Anything intern's sanitiser allows survives unchanged, anything else is dropped.
  Change the allowlist in both repositories together. Titles, labels and menu text are plain text,
  escaped by gahk.dk's templates.

## Authentication

Every endpoint except `GET /api/health` needs two headers:

```
X-Gahk-Timestamp: <unix seconds>
X-Gahk-Signature: <hex of HMAC-SHA256(KEY, "{timestamp}\n{METHOD}\n{path}\n{sha256_hex(body)}")>
```

- `KEY` is `GAHK_WEB_KEY` in intern's environment and `[sync] key` in gahk.dk's `config.ini`, at
  least 32 characters.
- `METHOD` is upper case. `path` is the request path without the query string, e.g. `/api/publish`.
  `sha256_hex(body)` is the hex SHA-256 of the raw body; an empty body hashes the empty string.
- gahk.dk refuses a request whose timestamp is more than 300 seconds from its clock, or whose
  signature does not match (compared in constant time). The answer is `401 {"error": "<reason>"}`.

Example message for `POST /api/update` with body `{}` at 1794440583:

```
1794440583
POST
/api/update
44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
```

## Responses

Responses are JSON (`application/json`) unless noted. Errors look like `{"error": "<message>"}`,
sometimes with more keys. Common statuses: `400` invalid body, `401` bad or missing signature,
`404` unknown endpoint, `405` wrong method, `413` body too large, and `503` when gahk.dk's database
is unreachable (`{"error": "database unavailable"}`).

Body limits: 15 MB for an image, 32 MB for `publish` and `preview`, 1 MB for everything else.

## Endpoints

### GET /api/health (unsigned)

`200 {"ok": true, "version": "<code sha>", "revision": "<content revision or null>"}`. Without a
working database: `503 {"ok": false, "version": …, "revision": null}`. When the running release's
packages were installed for another Python than the webhost's (after Hetzner upgraded it), the
cgi-bin stub answers `503 {"ok": false, "version": …, "revision": null, "error": "<reason>"}` until
the next `POST /api/update` has reinstalled them. It exposes no secrets. `version` is the git sha of
the running release, or `"dev"` in a checkout.

### GET /api/status

```json
{
  "version": "<code sha>",
  "revision": "<revision of the cached snapshot, or null>",
  "published_at": "<the snapshot's published_at, or null>",
  "rendered_at": "<when the pages were last rendered, or null>",
  "outbox": {"applications": 0, "visit_days": 0},
  "update": {
    "checked_at": "<when the updater last ran, or null>",
    "available_version": "<the sha in the production manifest, or null>",
    "last_result": "updated | unchanged | failed | null",
    "last_error": "<message, or null>"
  }
}
```

intern compares `revision` with its own to decide whether to publish, and shows `version`,
`update.available_version` and `update.last_result` in the Hjemmeside overview.

While no release can run (none is installed yet, or the running one's packages were installed for
another Python), the cgi-bin stub answers a signed request itself: `503` with the same keys, plus
`error` naming the reason. `version` is the installed sha or null; `revision`, `published_at`,
`rendered_at` and `outbox` are null; `update` comes from the updater's state as usual. That way how
a slow first install or repair ended can still be read. intern treats every 5xx as gahk.dk being
unavailable and does not read the body, so its overview shows gahk.dk as unavailable until the
release runs again; the body is for an operator sending the request by hand (DEPLOY.md).

### POST /api/images/missing

Body `{"sha256": ["<64 hex>", …]}` (at most 10 000) → `200 {"missing": ["<64 hex>", …]}`: the images
gahk.dk does not have.

### PUT /api/images/<sha256>

Body: the image's raw bytes, with `Content-Type` set to `image/jpeg`, `image/png`, `image/gif` or
`image/webp`. → `204`. It answers `400` if the bytes do not hash to `<sha256>`, are over 15 MB, or
have another type. Before answering, gahk.dk makes the image's responsive WebP variants (a fraction
of a second for a picture of 2400 px; see the snapshot's images below), so intern uploads one image
per request.

### POST /api/publish

Body: a [snapshot](#the-snapshot).

- `200 {"revision": "…", "pages_written": n}`. The site was re-rendered; `pages_written` counts the
  files whose content changed.
- `409 {"error": "missing images", "missing": ["<sha256>", …]}`: upload those, then publish again.
- `400 {"error": "…"}`: the snapshot is invalid (bad shape, reserved path, unknown key, duplicate
  ids). Nothing changes, and the site keeps the previous snapshot.

### POST /api/preview

```json
{"kind": "page" | "news" | "event", "item": {…as in the snapshot…}, "media_base": "https://intern.gahk.dk"}
```

→ `200 text/html`: the item rendered with the currently published menu and settings, marked
`noindex`. Preview items may reference `/media/cms/…` images, which get `media_base` in front, so the
editor's browser loads them from intern. A page that has not been saved yet needs a placeholder
`id` ≥ 1. A page with path `""` and an id other than `front_page_id` is a draft without an address:
it renders as an ordinary page and leaves the front page alone. Previews are never stored.

### GET /api/outbox

```json
{
  "applications": [
    {"id": "<uuid>", "kind": "rundvisning" | "fremleje", "submitted_at": "<iso>", "fields": {…}}
  ],
  "visits": [
    {"day": "2026-11-10", "path": "faciliteter", "views": 12, "visitors": 9},
    {"day": "2026-11-10", "path": "*", "views": 40, "visitors": 21}
  ]
}
```

- `applications`: the oldest 500 not yet acknowledged. `fields` uses the names of intern's
  admissions forms: `full_name`, `email`, `gender` (`male`/`female`/`other`), `age`,
  `heard_about_us`, `motivation`. A `rundvisning` adds `study_year`, `year_left`, `university` and
  `field_of_study`; a `fremleje` adds `occupation`.
- `visits`: every day not yet acknowledged, today included, though today is still counting. `path`
  is the page path without slashes (`""` is the front page), and `"*"` is the whole site that day.
  `visitors` are unique per day.

intern creates applications idempotently on `id`, and upserts visit rows (`day`, `path`) with the
counts as given.

### POST /api/outbox/ack

Body `{"applications": ["<uuid>", …], "visits_through": "2026-11-10"}`. Either key may be left out.
→ `200 {"deleted_applications": n, "deleted_visit_days": n}`. It deletes those applications, and
the visit rows with `day ≤ visits_through` and before today. Today's rows stay until the day is over.

### POST /api/rebuild

Body `{}`. → `200 {"pages_written": n}`: the pages re-rendered from the cached snapshot. intern sends
this nightly, so "upcoming events" change on the right day even when nobody visits. `409` means no
snapshot has been published yet.

### POST /api/update

Body `{}`. → `200 {"updated": bool, "version": "<running sha or null>", "available_version": "<sha
in the release manifest, or null>", "detail": "…"}`.

This runs the self-updater, and it is the only way the webhost updates: intern's beat task every 15
minutes, plus the "Opdatér gahk.dk nu" button. The cgi-bin stub checks the signature and runs
`~/gahk_web/bin/updater.py` itself, before any release is imported, so this endpoint also works
when no release is installed yet (bootstrap) or the active one is broken.

- `detail` is `installed <sha> (was <sha>)`, `already running <sha>` (with a note when pinned),
  `reinstalled packages for <python> (running <sha>)` (the webhost's Python changed, so the running
  release got its packages again; `updated` is `true`), `an update is already running`, `the update
  is still running; …` (the install outlasted the 45 seconds the request waits, and finishes in the
  background), or `failed: <reason>`, e.g. `failed: pip install: <pip's error>` or `failed: pip is
  not available on the webhost`.
- The update, including the pip install of a release's packages from PyPI, runs in a process of its
  own; the request waits for it at most 45 seconds, so intern's read timeout must be longer (it is
  60 seconds).
- A failure still answers `200`, with `updated: false`. `GET /api/status` reports the outcome too,
  with `503` while no release can run (see above).
- Before a release is installed, the stub answers `503` if `~/gahk_web/bin/updater.py` is missing.
  A checkout (`version` `"dev"`) answers `409`.

## The snapshot

```json
{
  "format": 1,
  "revision": "<64 hex>",
  "published_at": "2026-11-11T12:00:00+01:00",
  "site": {"front_page_id": 1, "default_background": "/billeder/<sha16>/<name>" | ""},
  "legacy_images": {"/static/legacy/image/upload/images/gang.jpg": "/billeder/<sha16>/gang.jpg"},
  "pages": [{"id": 1, "path": "", "title": "Velkommen", "body": "<p>…</p>",
             "background": "/billeder/<sha16>/<name>" | "", "description": "", "updated_at": "<iso>"}],
  "news": [{"id": 3, "title": "…", "body": "<p>…</p>", "published_at": "<iso>"}],
  "events": [{"id": 5, "title": "…", "description": "<p>…</p>", "starts_on": "2026-12-01"}],
  "menu": [{"label": "Hjem", "url": "/"}],
  "redirects": {"gammel-sti": "ny/sti"},
  "images": [{"url": "/billeder/<sha16>/<name>", "sha256": "<64 hex>",
              "content_type": "image/jpeg", "size": 12345}]
}
```

- `revision` is a SHA-256 of the content, computed by intern. gahk.dk stores it and reports it in
  `status`, so intern can tell whether gahk.dk is up to date. gahk.dk does not recompute it.
- **Paths** have no leading or trailing slash, one or two segments of `[a-z0-9]` joined by `-` or `_`.
  The front page has path `""`. Pages without an address are not sent.
- **Reserved paths** make gahk.dk refuse the snapshot, so intern refuses them as page paths too.
  - First segments: `admin`, `api`, `begivenheder`, `billeder`, `cgi-bin`, `django-admin`,
    `favicon.ico`, `health`, `intern`, `kalender`, `media`, `nyintern`, `photo-album`,
    `robots.txt`, `s`, `sitemap.xml`, `static`, `sw.js`, `wiki`, `404`.
  - Full paths: `optagelse/ansoeg`, `optagelse/fremlej`, `optagelse/send_rundvisning`,
    `optagelse/send_fremleje`, `optagelse/listansoegninger`, `optagelse/eksport`.
  - `optagelse` itself is an ordinary page.
- **Images** are content: they all live in intern's image library, and gahk_web's repository holds
  none.
  - intern rewrites `/media/cms/<name>` to `/billeder/<sha256[:16]>/<basename>`. The basename is
    slugified and lower-case, with its extension kept.
  - Every image a page, background or body uses is listed in `images`, with its type (jpeg, png,
    gif, webp) and size (≤ 15 MB). Page and default backgrounds must be `/billeder/` URLs listed
    there.
  - These URLs are immutable. gahk.dk serves them with a one-year cache.
  - gahk.dk adds WebP variants of each JPEG, PNG and WebP image next to it, as
    `/billeder/<sha16>/_<width>w.webp`, and its pages link those instead of the original. A name
    starting with `_` never matches an image name above, so the two cannot collide. The original
    stays at its URL. This changes nothing intern sends.
- **`legacy_images`** maps old public image URLs (`/static/legacy/image/<rest>`) to their
  `/billeder/` URL. gahk.dk answers `/static/legacy/image/<rest>` and `/public/image/<rest>` with a
  301 to the new URL.
- **Links** in bodies to `/intern/…` or `http(s)://(www.)gahk.dk/intern/…` are rewritten by intern
  to `https://intern.gahk.dk/…`.
- **Menu** URLs are site paths (`/vision`, `/begivenheder/`) or absolute URLs. A page's canonical
  address has no trailing slash, unless the menu links it with one.
- **`redirects`** maps old page paths to current ones. gahk.dk answers them with 301.
- Unknown keys anywhere, and duplicate page, news or event ids, make gahk.dk refuse the snapshot.
  This way a contract drift shows up as a failed publish, not as missing content.

## The edit link

Every page on gahk.dk carries a hidden **Rediger siden** link. A script on the page shows it when the
browser has the cookie below, which only intern sets. The cookie is a hint for the browser, not a
permission: intern checks the user's roles again when the link is followed.

- Cookie: `gahk_redaktoer=1`, with `Domain=.gahk.dk`, `Path=/`, `Secure`, `SameSite=Lax`, and **not**
  `HttpOnly`, because gahk.dk's script must read it. No other value shows the link.
- intern sets it at login, and again on each request, for a user who is a superuser or holds one of
  `CMS_EDITOR_ROLES` as a real role (not a role preview). It deletes it (same name, domain and path)
  at logout, and on the first request after the user has lost the role.
- The link points at intern, which must serve these addresses:
  - `{intern_url}/hjemmeside/sider/<page id>/`: edit that page (every page, the front page included).
  - `{intern_url}/hjemmeside/`: the Hjemmeside overview, for every other page (news and events, the
    forms, the 404 page).
- `intern_url` is `[site] intern_url` in gahk.dk's `config.ini`, `https://intern.gahk.dk` by default.

## Who sends what, when (intern's side)

| When | Requests |
|---|---|
| Every content save (after commit) | `images/missing` → `PUT images/…` for each missing → `publish` (retried once on 409) |
| Every 2 minutes | `status`; publish if the revision differs; `outbox` → store → `outbox/ack` |
| 00:05 every night | `rebuild` |
| Every 15 minutes, and the "Opdatér gahk.dk nu" button | `update` |
| The editor's preview | `preview` |

With `GAHK_WEB_KEY` empty, intern sends nothing, and its overview says *ikke forbundet*.
