# Deploying gahk.dk (gahk_web) on Hetzner Webhosting

gahk.dk runs on a Hetzner Webhosting S account: Apache with `.htaccess`, Python only as CGI, no SSH,
one PostgreSQL database. This is the complete runbook: the one-time setup, the cutover from the old
site, day-to-day updates, rollback, and what to do if the account is lost.

**What the webhost must provide** (checked on the account on 2026-11-12, with CGI scripts in
`public_html`): `/usr/bin/python3` is CPython 3.13 (3.13.5, x86_64); `python3 -m pip` works from a
CGI request and reaches PyPI over HTTPS; the account's home is `/usr/home/<login>`, with
`public_html` directly under it. The updater installs the site's Python packages (Jinja2,
MarkupSafe, psycopg with its libpq, Pillow, nh3) with that pip, into each release. Without pip on
the webhost no release can be installed (section 12).

**No cron job is used.** The webhost updates its code only when intern.gahk.dk asks it to:

- intern's beat task sends a signed `POST https://gahk.dk/api/update` every 15 minutes;
- an administrator can press **Opdatér gahk.dk nu** in intern's *Hjemmeside* overview to send one at
  once.

On every such request the webhost reads the manifest of the GitHub release `production`. If CI has
published a newer bundle, it downloads it, checks it and installs it. A request can only make the
webhost fetch what CI put on GitHub, and nobody uploads code to the webhost after the one-time
bootstrap. GitHub holds no secret for the webhost.

Daily housekeeping needs no cron job either. The first request of a new day re-renders the site, so
"upcoming events" stay right, and purges old rate-limit rows and visitor hashes. intern also sends
`POST /api/rebuild` every night at 00:05.

## Contents

1. [How it fits together](#1-how-it-fits-together)
2. [The PostgreSQL database](#2-the-postgresql-database)
3. [config.ini](#3-configini)
4. [The one-time bootstrap upload](#4-the-one-time-bootstrap-upload)
5. [TLS certificate](#5-tls-certificate)
6. [Domains and DNS](#6-domains-and-dns)
7. [Varnish: leave it off](#7-varnish-leave-it-off)
8. [The cutover](#8-the-cutover)
9. [Verification checklist](#9-verification-checklist)
10. [Updates and rollback](#10-updates-and-rollback)
11. [If the webhost explodes](#11-if-the-webhost-explodes)
12. [Logs and troubleshooting](#12-logs-and-troubleshooting)
13. [Things to verify on Hetzner](#13-things-to-verify-on-hetzner)

## 1. How it fits together

```
 intern.gahk.dk (Coolify)                         gahk.dk (Hetzner Webhosting S)
 ┌──────────────────────────┐   signed HTTPS     ┌──────────────────────────────────────┐
 │ Hjemmeside editor        │ ─────────────────▶ │ /api/*   (cgi-bin/gahk.cgi)          │
 │ beat: sync every 2 min   │  publish, images,  │ renders static pages into            │
 │       update every 15 min│  outbox, update    │ ~/public_html, queues form posts     │
 │       rebuild at 00:05   │ ◀───────────────── │ and visit counts until intern pulls  │
 └──────────────────────────┘   (responses only) └───────────────┬──────────────────────┘
                                                                 │ on POST /api/update
                                                                 ▼
                              github.com/GAHK-org/gahk_web/releases/tag/production
                              manifest.json + gahk_web-<sha>.tar.gz (built by CI)
```

What lives where on the webhost (`~` is the FTP home):

```
~/gahk_web/config.ini            secrets; written once by hand (section 3), never overwritten
~/gahk_web/bin/updater.py        the self-updater; uploaded once, then replaced by each release
~/gahk_web/CURRENT               the active release (a git sha)
~/gahk_web/releases/<sha>/       the 3 most recently installed releases, each with requirements.lock
  └ <sha>/vendor/                its Python packages, installed by the updater with pip for this
                                 python3 (vendor/.abi names that python); about 50 MB
~/gahk_web/state/                cache: image blobs, update.json (the last update's result)
~/gahk_web/var/                  update.log, the update lock
~/gahk_web/tmp/                  pip's temporary files during an install; deleted afterwards
~/public_html/.htaccess          from the release, overwritten by each install except its LOCAL block
~/public_html/cgi-bin/gahk.cgi   from the release (the stub that starts the active release)
~/public_html/static/            from the release (css with hashed names, fonts, icons)
~/public_html/billeder/          images, copied from intern's library, and their WebP variants
                                 (`<sha16>/_<width>w.webp`), made here; both are cache
~/public_html/…                  the rendered pages: index.html, <path>/index.html, 404.html, …
```

Nothing here is the only copy of anything that matters: content and images come from intern, code
from GitHub. The database holds a cache of the last snapshot, plus form submissions and visit counts
until intern pulls them, which it does every 2 minutes.

## 2. The PostgreSQL database

1. konsoleH → *Databases* → *PostgreSQL* → create a database. Webhosting S has one database slot.
2. Note the **hostname** konsoleH shows (`localhost` does not work on webhosting), the port (5432),
   the database name, the user and the password. Use a long random password: Hetzner's database port
   is reachable from the internet and cannot be closed.
3. Optional but recommended: switch on **Require SSL** for the database and download Hetzner's CA
   certificate `sqlca.pem` from the same page. Upload it to `~/gahk_web/sqlca.pem` and use
   `sslmode = verify-full` with `sslrootcert` in config.ini (section 3). Without Require SSL, use
   `sslmode = require`. That encrypts the connection but does not check the certificate, the same as
   libpq's `require`. Without an `sslmode` line gahk.dk uses `require` (`verify-full` when
   `sslrootcert` is set); `disable` is for a local development database only.

The updater creates the tables (migrations) on the first install. Nothing else is needed.

## 3. config.ini

Write `~/gahk_web/config.ini` with the konsoleH file manager or by SFTP, then set its mode to
**600**. It is outside `public_html`, so Apache can never serve it. The `.htaccess` also refuses
`*.ini` everywhere. No release ever overwrites it.

Generate each secret on your own machine with:

```sh
python3 -c 'import secrets;print(secrets.token_urlsafe(48))'
```

```ini
; ~/gahk_web/config.ini: gahk.dk on Hetzner. Mode 600. Never commit this file.

[database]
; From konsoleH → Databases (section 2). localhost does NOT work on the webhost.
host = <the hostname konsoleH shows>
port = 5432
name = <database name>
user = <database user>
password = <database password>
; With "Require SSL" on: verify-full plus Hetzner's CA certificate. Otherwise: require.
sslmode = verify-full
sslrootcert = /usr/home/<login>/gahk_web/sqlca.pem

[sync]
; Signs every /api/* request between intern and gahk.dk (HMAC-SHA256), at least 32 characters.
; It MUST equal GAHK_WEB_KEY in intern's environment (Coolify). Change both together.
key = <secret 1>

[stats]
; HMAC key for visitor counting and the form rate limit; no IP address is ever stored.
; Only this host needs it. Changing it only makes today's visitors count twice.
secret = <secret 2>

[site]
; The public address (forms check that posts come from it) and intern's address (links, calendar).
base_url = https://gahk.dk
intern_url = https://intern.gahk.dk

[turnstile]
; Optional: Cloudflare Turnstile on the two optagelse forms. Leave out for no captcha.
; site_key = <public site key>
; secret_key = <secret key>

[update]
; Optional. Where the updater looks for new releases (default: GitHub's production release).
; manifest_url = https://github.com/GAHK-org/gahk_web/releases/download/production/manifest.json
; Optional: hold this exact version (a full 40-character git sha) instead of following main.
; Used for rollback (section 10). Remove the line to follow main again.
; pin = <sha>

; [paths] is optional; the defaults are right on Hetzner:
; public_html = /usr/home/<login>/public_html
; state_dir = /usr/home/<login>/gahk_web/state
```

On intern (Coolify → the app's environment variables):

```
GAHK_WEB_URL=https://gahk.dk
GAHK_WEB_KEY=<secret 1>          # the [sync] key above
PUBLIC_SITE_URL=https://gahk.dk
```

## 4. The one-time bootstrap upload

This is the only manual upload. Afterwards every update comes from GitHub by itself.

1. Open <https://github.com/GAHK-org/gahk_web/releases/tag/production> and download three assets:
   `updater.py`, `gahk.cgi` and `htaccess`.
2. Upload them by SFTP (host `wwwNNN.your-server.de` from konsoleH → *Access details*, port 22, main
   FTP user) or with the konsoleH file manager:

   | Download | Upload to | Mode |
   |---|---|---|
   | `updater.py` | `~/gahk_web/bin/updater.py` | 644 |
   | `gahk.cgi` | `~/public_html/cgi-bin/gahk.cgi` | **755** (the folder `cgi-bin` 755 too) |
   | `htaccess` | `~/public_html/.htaccess` (note the dot) | 644 |

   Together with `~/gahk_web/config.ini` (mode 600), that is all. The updater creates `releases/`,
   `state/`, `var/` and `CURRENT` itself.
3. Delete any placeholder Hetzner put in `~/public_html` (e.g. `index.html`). Delete the files left
   from testing the webhost in November 2026 as well: `~/public_html/install.py`,
   `~/public_html/test_numpy.py` (`install.py` lets anyone on the internet start pip runs on the
   account), the folder `~/public_html/fcgitest` (a FastCGI test app, `test.fcgi`), and the folder
   `~/python_packages`. gahk.dk needs none of them: the updater installs its own packages into each
   release. The `.htaccess` refuses every `*.py` and `*.fcgi`, and every `*.cgi` outside
   `cgi-bin/`, but delete them all the same.
4. The CGI script must not be group- or world-writable, or suEXEC refuses to run it. The same goes
   for its folder.
5. Every install overwrites `~/public_html/.htaccess` with the release's, keeping only the lines
   between `# BEGIN LOCAL` and `# END LOCAL`. Put host-specific lines there, such as
   `SetEnv GAHK_WEB_HOME /usr/home/<login>` should the stub ever fail to find the home (section 12).
   Make any other change in the repository's `public_html/.htaccess` and let it release. If the
   first install needs it (section 13 item 1), upload that edited file as the bootstrap `htaccess`.
   A change made only on the server outside the LOCAL block is lost at the next install.

The first install happens on the first signed `POST /api/update`. Normally that is intern's beat task
(at most 15 minutes) or the **Opdatér gahk.dk nu** button. Before the DNS switch, though, intern's
requests to `https://gahk.dk` still reach the old site, so for the first install send the request by
hand, straight to the webhost (section 8 step 3).

### Sending a signed request by hand

It needs only `openssl` and `curl`, and works in bash and zsh (Linux, macOS). Paste the function into the shell, then call it with the `[sync]` key:

```sh
gahk_api() {  # gahk_api METHOD PATH [BODY]   (uses $GAHK_WEB_KEY, and $RESOLVE when set)
  local api_method="$1" api_path="$2" api_body="${3:-}" api_ts api_sig
  api_ts=$(date +%s)
  api_sig=$(printf '%s\n%s\n%s\n%s' "$api_ts" "$api_method" "$api_path" \
              "$(printf '%s' "$api_body" | openssl dgst -sha256 -r | cut -d' ' -f1)" \
            | openssl dgst -sha256 -hmac "$GAHK_WEB_KEY" -r | cut -d' ' -f1)
  set -- -sS -X "$api_method" "https://gahk.dk$api_path" -H "X-Gahk-Timestamp: $api_ts" \
    -H "X-Gahk-Signature: $api_sig" -H 'Content-Type: application/json'
  if [ -n "${RESOLVE:-}" ]; then set -- "$@" --resolve "$RESOLVE"; fi
  if [ -n "$api_body" ]; then set -- "$@" --data "$api_body"; fi
  curl "$@"
  echo
}

export GAHK_WEB_KEY='<secret 1>'
gahk_api POST /api/update '{}'      # check for a new release and install it now
gahk_api GET /api/status            # running version, content revision, outbox, last update
```

To talk to the webhost before DNS points at it, set `RESOLVE=gahk.dk:443:<webhost IPv4>` (the
address konsoleH shows for the account). The clock of the machine sending the request must be within
5 minutes of the right time.

## 5. TLS certificate

konsoleH → *Services* → *SSL Manager* → Let's Encrypt, for `gahk.dk` and `www.gahk.dk`.

- **Before the DNS switch**, choose **DNS validation (TXT)**, for this first certificate only.
  konsoleH shows a `_acme-challenge` TXT value for each name. Add them at gahk.dk's DNS provider
  (Punktum), wait for them to resolve, and confirm in konsoleH. The certificate is then ready before
  any visitor arrives.
- DNS validation cannot renew by itself. Every renewal gets a new `_acme-challenge` value, and
  Hetzner cannot write records at Punktum, so the certificate would expire. Browsers that have seen
  `Strict-Transport-Security` then refuse the site outright.
- **Right after the switch**, once `@` and `www` resolve to the webhost, re-issue the certificate in
  the SSL Manager (or switch it) with **file (HTTP) validation**. Hetzner then renews it
  unattended while the account is active. The `.htaccess` lets `/.well-known/` through, and Let's
  Encrypt follows the http→https redirect. Then delete the `_acme-challenge` TXT records at Punktum.
- Do not switch on HSTS separately in konsoleH. The `.htaccess` sends
  `Strict-Transport-Security` on https responses once the certificate works.

## 6. Domains and DNS

**In konsoleH**, add `gahk.dk` and `www.gahk.dk` to the account as an external domain, with the
document root `public_html`. DNS stays at Punktum, so konsoleH's own DNS settings do not matter.
Do not create `intern.gahk.dk` in konsoleH: a konsoleH subdomain can only point at a folder on the
webhost.

**At Punktum (gahk.dk's DNS)**:

| Name | Type | Value | When |
|---|---|---|---|
| `_acme-challenge`, `_acme-challenge.www` | TXT | from konsoleH's SSL Manager | before the switch; delete once the certificate uses file validation (section 5) |
| `intern` | A / AAAA | the Coolify server (today's gahk.dk addresses) | before the switch |
| `@` (gahk.dk) | A / AAAA | the webhost's IPv4 / IPv6 from konsoleH | **the switch** |
| `www` | A / AAAA (or CNAME `gahk.dk.`) | the webhost | the switch |
| MX, SPF (TXT), DKIM, DMARC | unchanged | mail stays on one.com | never touch |

Lower the TTL of `@` and `www` to 300 seconds a day before the switch, and raise it again a few days
after. `www.gahk.dk` answers with a 301 to `https://gahk.dk` from the `.htaccess`.

## 7. Varnish: leave it off

konsoleH offers Varnish in front of Apache. Leave it **off**:

- Apache already serves every page, image and asset as a plain file from disk. Python starts only
  for the API, the forms, the visit beacon, the calendar feeds, redirects and 404s, none of which
  may be cached anyway.
- The `.htaccess` sets the cache headers browsers need: a minute for pages, so an edit shows within a
  minute, and a year, immutable, for `/billeder/` and hashed assets. A Varnish TTL on top would only
  delay edits, and Hetzner documents no way to purge it.
- Behind Varnish, `REMOTE_ADDR` may become Varnish's address. The rate limit on the forms and the
  daily visitor count both key on the client address, and `X-Forwarded-For` is deliberately not
  trusted.

## 8. The cutover

Until step 4, the old site keeps serving gahk.dk from Coolify.

1. **Prepare the webhost**: database (section 2), `config.ini` (section 3), the bootstrap upload
   (section 4), the TLS certificate by DNS validation (section 5), and `gahk.dk` and `www` in
   konsoleH (section 6).
2. **Create `intern.gahk.dk`** at Punktum, pointing at the Coolify server, and add the domain in
   Coolify. This changes nothing for visitors yet.
3. **Install gahk_web by hand**, straight to the webhost:

   ```sh
   export GAHK_WEB_KEY='<secret 1>' RESOLVE='gahk.dk:443:<webhost IPv4>'
   gahk_api POST /api/update '{}'
   # {"updated": true, "version": "<sha>", "available_version": "<sha>", "detail": "installed <sha>"}
   curl -sS --resolve "$RESOLVE" https://gahk.dk/api/health
   # {"ok": true, "version": "<sha>", "revision": null}
   ```

   An answer with `"still running"` means the install took over 45 seconds and is finishing in the
   background; the first install also downloads the packages from PyPI, which usually takes well
   under a minute. Ask `gahk_api GET /api/status` a minute later: until the release runs it answers
   503 with the reason and the updater's `update.last_result` and `last_error`. gahk_web is now live
   but empty: pages answer the 503 page *"Siden opdateres"*.
4. **Switch DNS**: `@` and `www` to the webhost (section 6). As resolvers pick it up, visitors see
   *"Siden opdateres"* for a few minutes. Once both names resolve to the webhost, re-issue the
   certificate with file validation (section 5), or it will not renew.
5. **Deploy the new intern** with `GAHK_WEB_URL`, `GAHK_WEB_KEY` and `PUBLIC_SITE_URL` set (section 3).
   Within about 2 minutes its sync task sees that gahk.dk has no content and publishes everything:
   it first converts the old `static/legacy` images into the image library, then uploads the images
   and the snapshot. The site is complete from that moment. The *Hjemmeside* overview on intern
   shows the publication and gahk.dk's version.
6. **Check** everything in section 9. Old intranet links (`gahk.dk/intern/…`) now redirect to
   `intern.gahk.dk/…`, and the old calendar feed URLs keep working through gahk.dk.

## 9. Verification checklist

```sh
curl -sI https://gahk.dk/                  # 200, Cache-Control: public, max-age=60
curl -sI http://gahk.dk/                   # 301 → https://gahk.dk/
curl -sI https://www.gahk.dk/faciliteter   # 301 → https://gahk.dk/faciliteter
curl -sI https://gahk.dk/faciliteter/      # 200 (both slash forms are served, no redirect loop)
curl -sS https://gahk.dk/api/health        # {"ok": true, "version": "<sha>", "revision": "<64 hex>"}
curl -sI https://gahk.dk/intern/           # 308 → https://intern.gahk.dk/
curl -sI https://gahk.dk/wiki/             # 308 → https://intern.gahk.dk/wiki/
curl -sI https://gahk.dk/findes-ikke       # 404 with the site's 404 page
curl -sI https://gahk.dk/.htaccess         # 403
curl -sI https://gahk.dk/fcgitest/test.fcgi   # 403 (and delete the folder, section 4 step 3)
curl -sI https://gahk.dk/favicon.ico       # 200
curl -s  https://gahk.dk/ | grep -o '/static/css/site\.[0-9a-f]*\.css'   # the hashed stylesheet …
curl -sI https://gahk.dk/static/css/site.<hash>.css   # … 200, max-age=31536000, immutable
curl -sI https://gahk.dk/billeder/<sha16>/<name>      # an image from a page: 200, immutable
curl -sI https://gahk.dk/sw.js             # 200, Cache-Control: no-cache
gahk_api GET /api/status                   # version, revision, outbox counts, update.last_result
```

In a browser:

- Send the tour form (*Optagelse → Ansøg om rundvisning*). The thank-you page appears, and within
  2 minutes the application shows up on intern, and the applicant gets intern's auto-reply.
- Open a few pages on a phone. Logged in to intern as an editor, the pages show **Rediger siden**.
- intern's *Hjemmeside* overview shows the running version equal to the available one, and the last
  update as `updated` or `unchanged`.
- konsoleH's SSL Manager shows **file validation** for `gahk.dk` and `www.gahk.dk`, not DNS (section
  5), so the certificate renews by itself.

## 10. Updates and rollback

**Normal updates.** Merging into `main` runs CI. If every check passes and the commit is still the
tip of `main`, the `release` job uploads the bundle and then `manifest.json` to the `production`
release. The webhost installs it on intern's next nudge (at most 15 minutes) or when someone presses
**Opdatér gahk.dk nu**. The updater:

1. downloads the bundle (redirects followed, at most 50 MB) and checks its size and sha256;
2. unpacks it into `releases/<sha>`, refusing absolute paths, `..`, links and devices;
3. installs the release's packages for the webhost's `python3`: `python3 -I -m pip install --target …
   --require-hashes --only-binary=:all: --no-deps -r requirements.lock`, isolated from any pip
   configuration on the account, into a temporary folder. Every file pip downloads must match the
   sha256 in the release. When a kept release has the same `requirements.lock` and was installed
   for the same python, its packages are copied as hard links instead, so a release that changes
   only code needs neither PyPI nor extra disk;
4. runs the release's `selftest.py` with the webhost's `python3` and those packages, and only then
   makes them the release's `vendor/`;
5. applies database migrations with the new code;
6. installs the new static files, `cgi-bin/gahk.cgi` and `.htaccess`. When the `.htaccess` changed,
   it asks `{base_url}/api/health`; if that answers 5xx and the previous `.htaccess` cures it, it puts
   the previous one back and fails the update;
7. switches `CURRENT`. Up to this point the old release kept running untouched;
8. takes the release's `bin/updater.py` if it passes its own self-test, re-renders the site, deletes
   hashed static files that neither this nor the previous release uses, and keeps 3 releases.

If any step up to 7 fails, the old release stays live, and intern's overview shows the error. The
same error is in `update.log` (section 12). A schema migration runs before the switch, so migrations
must keep working with the previous release.

CI publishes a bundle only when the `lint`, `typecheck`, `test (Python 3.13, PostgreSQL 15)`,
`test (Python 3.14, PostgreSQL 17)` and `bundle` checks pass. The `bundle` job installs the
release's packages on x86_64 Linux for Python 3.13 and 3.14 with the updater's own pip command, so a
package without a wheel for the webhost's python fails there, not on the webhost. That holds as long
as the webhost's glibc is not older than the CI runner's (section 13 item 14).

**When PyPI is down.** A release with new packages cannot install: the update fails with
`failed: pip install: …`, the old release keeps running, and the next nudge (15 minutes later) tries
again. A release with the same `requirements.lock` as a kept one installs without PyPI.

**When Hetzner upgrades Python.** Packages with compiled parts (psycopg, Pillow, nh3, MarkupSafe)
fit only the python they were installed for, so after an upgrade nothing that needs Python runs.
Pages and pictures keep working, because Apache serves them as files. Everything that reaches the
CGI app answers 503 instead: the forms (applications are refused until the repair, with *"Siden
opdateres"*), the visit beacon, redirects of old addresses, 404s and the API, except
`POST /api/update`. `GET /api/health` says why. The next `POST /api/update` (at most 15 minutes, or
**Opdatér gahk.dk nu**) reinstalls the running release's packages for the new python, runs its
self-test, and everything is back; no new release is needed. The overview shows `reinstalled
packages for cpython-314 (running <sha>)`. Should the new python have no wheels for a package yet,
no pin helps, since every release needs its packages for that python; the forms and the API stay
down until a release with a newer version of that package (CI tests the next Python, 3.14,
already).

**Disk use.** A release's packages take about 50 MB (Pillow and psycopg's libpq most of it). The
kept releases share them as hard links while their `requirements.lock` is the same, so three
releases usually take about 50 MB, at most about 150 MB, plus about 50 MB while an install runs.
Interrupted installs leave folders named `.tmp-*` and `~/gahk_web/tmp`; the next update deletes them.
The images take their size twice: the blob in `~/gahk_web/state/images` (hard-linked into
`~/public_html/billeder` where hard links work) and the WebP variants beside it, which add about as
much again (the old site's 66 pictures: 18 MB of originals, 20 MB of variants).

**Rollback.** Add a pin to `~/gahk_web/config.ini` with the file manager:

```ini
[update]
pin = <full 40-character sha of the good commit>
```

Then press **Opdatér gahk.dk nu**, or run `gahk_api POST /api/update '{}'`. A release among the 3
kept on the webhost switches back without a download. An older one is fetched from GitHub, which
keeps the newest 5 bundles. While the pin is there, new releases are not installed; the overview
shows them as available. Fix forward on `main`, then remove the `pin` line to follow `main` again.

## 11. If the webhost explodes

The webhost holds nothing that exists only there, except what intern has not pulled yet.

- **Lost**: form submissions and visit counts from the last ≤ 2 minutes before the loss, because
  intern pulls the outbox every 2 minutes. Content, images, pages and code all come back by
  themselves.
- **Recovery** on a restored, new or different account:
  1. Create the database (section 2) and write `config.ini` (section 3). Reuse the old `[sync]`
     key, or set a new one and update `GAHK_WEB_KEY` on intern.
  2. Do the bootstrap upload (section 4), and the TLS and DNS steps if the address changed.
  3. Wait for intern's next nudge (≤ 15 minutes), or press **Opdatér gahk.dk nu**. That installs the
     current release and creates the tables.
  4. Within about 2 minutes intern's sync task sees that gahk.dk has no content and publishes
     everything again, images included.

Hetzner's nightly backups (14 days) can restore files and the database too, but nothing depends on
them.

## 12. Logs and troubleshooting

- `~/gahk_web/var/update.log` (and `update.log.1`, rotated at 1 MB) logs every update step: the
  versions, downloads, self-test and migration output, the switch, and failures. Read it with the
  file manager or SFTP.
- `~/gahk_web/state/update.json` holds the last update's result, which intern's overview shows.
- Fix `.htaccess` problems in the repository, not on the server: every install overwrites
  `~/public_html/.htaccess` except its LOCAL block (section 4). A fix made only on the server breaks
  again with the next release.
- konsoleH → *Statistics / Logs* → **error log**: Apache's error log. Tracebacks from the CGI app go
  there, as do suEXEC complaints, such as "script is writable by others" or a wrong shebang.
  `~/www_logs/` holds the daily access logs (anonymised IPs).
- intern's *Hjemmeside* overview shows the last publications with their errors, gahk.dk's running and
  available versions, and the last update result.

| Symptom | Likely cause |
|---|---|
| Every page answers *"gahk.dk er ved at blive installeret"* | No release installed yet: send `POST /api/update` (section 4). |
| Every page answers *"Siden opdateres"* | Installed, but intern has not published yet: check `GAHK_WEB_URL` and `GAHK_WEB_KEY` on intern. |
| `/api/*` answers 401 `bad signature` | The `[sync]` key and `GAHK_WEB_KEY` differ, or the sender's clock is off by more than 5 minutes. |
| `/api/*` answers 503 `database unavailable` | The `[database]` settings, or the database host is down. |
| The update result says `self-test failed` | The new release does not run on this python. The old one keeps running. See `update.log`. |
| The update says `failed: pip is not available on the webhost` | `python3 -I -m pip` does not work on the account: if it never did, see section 13 item 14; otherwise Hetzner changed its python. Ask Hetzner support for pip for `/usr/bin/python3`; the old release keeps running meanwhile. |
| A new release's update says `failed: pip install: ERROR: Could not find a version that satisfies …` or `No matching distribution` | PyPI has no wheel of a package the new release changed for the webhost's python (or for its glibc, section 13 item 14). The old release keeps running, so nothing is down: bump or replace the package in the repo and release again. A pin (section 10) only stops the retries every 15 minutes. |
| After a Python upgrade (`/api/health` says `the release's packages are for …`), the update says `failed: pip install: … No matching distribution` | The new python has no wheel yet for one of the running release's packages. No pin helps: every release needs its packages for this python. Release a version with a package that has wheels for it; CI's `bundle` job must pass on that Python. Until then forms and the API answer 503 (pages keep working). |
| The update says `failed: pip install:` with a network error, or `pip install took longer than 600 s` | PyPI was unreachable or slow. Nothing to do: the next nudge tries again, and the old release keeps running. |
| The update says `failed: pip install: ERROR: THESE PACKAGES DO NOT MATCH THE HASHES …` | A downloaded file is not the one the release names (a broken download, or something between the webhost and PyPI). It is refused; the next nudge tries again. If it persists, find out why before anything else. |
| `/api/health` answers 503 `the release's packages are for …`; forms answer *"Siden opdateres"* | Hetzner upgraded python. Pages and pictures still work; forms, redirects, 404s and the API answer 503. Send `POST /api/update` (or press **Opdatér gahk.dk nu**); it reinstalls the packages for the new python (section 10). |
| The update fails with `No space left on device` or `Disk quota exceeded` | The account's disk is full. Each release's packages take about 50 MB (section 10); delete old files in `~/www_logs` or images no longer used, then update again. |
| Pages show a picture's original (no `srcset`) | Its WebP variants are not made yet (a big first publish: the visit beacon, the nightly rebuild and the next update make the rest), or it gets none (GIF, animation, unreadable): the `images` table's `variant_note` says which. Pages are re-rendered when they are made. |
| The update says `an update is already running` | Another update holds the lock; it is released when that run ends. |
| The update says `failed: cannot take the update lock: …` | The file system refuses the lock on `~/gahk_web/var/update.lock` (permissions, or no lock support). No update installs until that is fixed. |
| The update says `the new .htaccess breaks the site` | Apache rejected the release's `.htaccess` (a syntax error, or a directive Hetzner does not allow), so the updater put the previous one back. Fix `public_html/.htaccess` in the repo; the next release installs. |
| The whole site, `/api/update` included, answers 500 right after an update | The new `.htaccess` breaks the site and the check did not catch it (it could not reach the site). Upload the previous release's `htaccess` asset to `~/public_html/.htaccess` by SFTP, then fix it in the repo. |
| `gahk.cgi` is shown as text, or 500 with "Premature end of script headers" | The CGI mode or permissions (section 4), or see section 13 item 1. |
| Every page answers *"gahk.dk er ved at blive installeret"* although a release is installed | The stub looks in the wrong home. Add `SetEnv GAHK_WEB_HOME /usr/home/<login>` between the `# BEGIN LOCAL` and `# END LOCAL` lines of `~/public_html/.htaccess`; installs keep that block. |

## 13. Things to verify on Hetzner

These are assumptions that could not be tested before the site ran on the webhost. The same items
are marked `[VERIFY ON HETZNER]` in `.htaccess`. Settled on 2026-11-12: `python3` is CPython 3.13.5
on x86_64, its pip installs binary wheels from PyPI from a CGI request, outbound HTTPS works, and
the home is `/usr/home/<login>` with `public_html` directly under it.

1. `*.cgi` under `public_html` runs as CGI without `Options +ExecCGI`/`AddHandler`. If not, uncomment
   those two lines in the repository's `public_html/.htaccess`, merge it and let it release. For the
   first install, upload that edited file as the bootstrap `htaccess`. Uncommenting them only on the
   server lasts until the next install, and then `POST /api/update` stops working too. It runs as
   the account user (suEXEC) with `/usr/bin/env python3`.
2. mod_rewrite supports the lookahead in the dot-file rule. `%{HTTPS}` is `on` for TLS requests, so
   the http→https rule does not loop.
3. `RewriteCond %{REQUEST_FILENAME}/index.html -f` also matches the trailing-slash form, so both
   `/faciliteter` and `/faciliteter/` are served without a redirect.
4. A POST to an existing file reaches the CGI app, because the file rules only apply to GET and HEAD.
5. The rewrite to `cgi-bin/gahk.cgi/$1` keeps the original `REQUEST_URI`. The API signature covers
   that path.
6. mod_headers with `<If>` expressions works in `.htaccess`.
7. HSTS is sent only once https works for `gahk.dk` and `www`.
8. `REMOTE_ADDR` is the real client (Varnish off).
9. The database is reachable with konsoleH's hostname, and `verify-full` works with `sqlca.pem`.
10. Hardlinks work in the home: from `~/gahk_web/state` to `~/public_html/billeder`, and between
    releases' `vendor/` folders. If they do not, the code copies instead (more disk, section 10).
11. A CGI process may start a child in its own session that outlives the request. The updater does
    this for installs slower than 45 seconds, and pip runs in that child. If Hetzner kills it, the
    install stops before the switch, or after it with only cleanup left, and the next nudge carries
    on. In tests an install takes a few seconds, plus the pip download on the first install.
12. One Python process per request is fast enough. Pages are static, so only API calls, forms,
    beacons, 404s and redirects start Python.
13. A CGI process may use the memory Pillow needs for the WebP variants: about 50 MB above the
    process for a 2400 px picture (what intern sends), about 200 MB for a 10 MP one. If Hetzner
    kills it, that request fails once: a killed `PUT` makes intern's publish fail, and the next
    sync publishes again. The `PUT`, `publish` and `rebuild` requests never retry an image whose
    attempt was killed; the visit beacon and the updater's re-render do, where a kill costs nobody
    an answer. After three killed attempts the image gets no variants, and pages show its
    original. A full disk does not count as an attempt.
14. The updater runs pip as `python3 -I -m pip` (isolated: no user site-packages, no `PYTHON*`
    variables), while the check on 2026-11-12 ran `python3 -m pip`. And the host's glibc is only
    known to be 2.28 or newer (numpy's `manylinux_2_28` wheels loaded), while CI's `bundle` job
    checks the wheels on Ubuntu with glibc 2.39. Check both once: upload this as
    `~/public_html/cgi-bin/hostcheck.cgi` (mode 755), open `https://gahk.dk/cgi-bin/hostcheck.cgi`,
    record the answers here, and delete the file.

    ```python
    #!/usr/bin/python3
    import os, subprocess

    print("Content-Type: text/plain\n", flush=True)
    subprocess.run(["/usr/bin/python3", "-I", "-m", "pip", "--version"], stderr=subprocess.STDOUT)
    print("glibc", os.confstr("CS_GNU_LIBC_VERSION"))
    ```

    - If pip answers `No module named pip` (it lives only in the user site), the updater must run
      it with `-E -P` instead of `-I`, keeping `PIP_CONFIG_FILE=/dev/null` and `--target`. Until
      then every install fails with `failed: pip is not available on the webhost`.
    - If glibc is older than 2.39, the `bundle` job must install with pip's
      `--platform manylinux_<major>_<minor>_x86_64` for the host's glibc, so that a wheel needing a
      newer glibc fails in CI. Until then such a wheel passes CI and fails only on the webhost:
      harmless for a new release (the old one keeps running), but it would block the repair after
      a Python upgrade.
