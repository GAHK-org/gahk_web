# Feature: Drift — how gahk.dk keeps itself running

gahk.dk runs on a small Hetzner webhost with no SSH and no scheduled jobs. It keeps itself up to
date, keeps serving when intern.gahk.dk or its storage is down, and can be rebuilt from nothing.
The runbook is DEPLOY.md.

## Updates

- Every merge into `main` that passes CI becomes a release on GitHub. Nobody uploads code to the
  webhost.
- The webhost installs a new release only when intern asks it to. intern asks every 15 minutes, and
  administrators can press **Opdatér gahk.dk nu** in the Hjemmeside overview to ask at once.
- An update only installs what CI published. Before going live, a release must:
  - match the size and checksum CI published,
  - unpack without anything that points outside its own folder,
  - get its packages for the webhost's Python, each matching the checksum the release names,
  - pass its own self-test on the webhost's Python with those packages,
  - apply its database changes, which must leave a database the running version still works with
    (they run while it is still live).
- When Hetzner upgrades Python, pages and pictures keep working, but the forms and everything else
  that needs Python answer "Siden opdateres" until the next update request, which reinstalls the
  packages of the running version for the new Python. Applications sent meanwhile are refused.
- If any check fails, the running version stays live. The overview shows the error.
- The switch to a new version is a single step, so visitors never meet half of an update. An update
  that takes long finishes after intern's request has been answered.
- Right after the switch the update asks the site which version answers. If the new version does
  not answer within a few seconds and the previous one does, the previous one goes back and the
  update counts as failed.
- The Hjemmeside overview shows the version gahk.dk runs, the newest one available, and how the last
  update went.
- An administrator can hold the site at an earlier version (rollback) with one line in the webhost's
  configuration. The last three versions stay on the webhost, and the last five on GitHub.

## How Python runs

- Forms, the visit count, redirects and intern's requests are answered by a program that keeps
  running between requests, so they take milliseconds. The web server starts more of it when
  requests overlap.
- It restarts by itself after an update, after a change to the webhost's configuration, after 500
  requests and after an hour. The first request after an update may still get the previous version.
- Update requests and large uploads (pictures, publications) start a new program each time, so an
  update can always repair a broken version.
- When the running version cannot start, those requests get *"Siden opdateres"*, and the program
  tries again every 30 seconds, without the web server ever giving up on it. Once an update has
  installed a version that can run, the next request after it already gets that version.
- An operator can send everything to a new program per request with one line in the webhost's
  configuration, should the running program misbehave.

## Independence

- Visitors never wait for intern. Pages and pictures are files on the webhost, and forms and visit
  counts are stored there until intern collects them.
- gahk.dk has no access to intern's database or storage, and calls nothing on intern that needs a
  login. intern starts every exchange, signed with a shared key. The only request the other way is
  the residents' calendar feeds, passed through as they are.
- When intern or its storage is down, gahk.dk serves the last published content. Edits made
  meanwhile appear once intern is back.
- With no scheduled jobs, the first visitor of a new day refreshes "upcoming events". intern also
  asks for this every night.

## Data loss bounds

- The webhost is a cache and an outbox, and holds nothing that matters only there.
  - Content, pictures and settings come from intern.
  - Code comes from GitHub.
- If the webhost is lost, intern republishes everything automatically within minutes of a new
  install. The only loss is form submissions and visit counts from the last couple of minutes,
  before intern collected them.
- Bringing it back takes a database, the configuration file, and four files uploaded once. The
  first update installs the rest.

## Decisions

- **No cron job on the webhost.** A forgotten or silently failing scheduled job is the classic way
  for a small site to rot. intern already runs a scheduler, so the webhost waits to be asked.
- **Pull from GitHub, not push to the webhost.** No deploy credentials exist anywhere. Even with
  the shared key, an update request can only make the webhost fetch the official release, never
  install code of its own.
- **Self-test on the real host before switching.** The webhost's Python changes with Hetzner's
  upgrades, so each release proves it runs on the Python it lands on.
- **Packages are installed on the webhost, for its own Python.** A release carries its exact,
  checksummed package list, and the webhost fetches those packages from PyPI when it installs the
  release. Compiled packages (database driver, image processing) are allowed. When Hetzner upgrades
  Python, the forms and the rest of what needs Python pause ("Siden opdateres") until the next
  update request reinstalls the packages for the new Python; no new release is needed. Pages and
  pictures are files and keep working.
- **A release whose packages do not fit stays out.** CI installs every release's packages on the
  host's Python and the next one before it is published, and the webhost installs them before the
  switch. When PyPI is down, the running version stays live; a release that changes only code reuses
  the packages it already has and needs no PyPI.
- **A program that keeps running (FastCGI), with a new program per request (CGI) as the rescue
  channel.** Starting Python, its libraries and a database connection for every form or page view
  cost more than answering it; the webhost keeps a FastCGI program running for free. Updates stay on
  CGI, which needs nothing but the standard library, so they work when the running version or
  FastCGI does not.
- **Large uploads go through CGI.** The webhost's FastCGI module refuses request bodies over 128 KiB,
  and that limit cannot be raised from the site.
- **A restart is a fresh process, never a reload inside one.** A process ends after a response that
  saw a new version or configuration, so no code or setting of two versions ever mixes.
- **Flask and flup.** With a program that keeps running, start-up time no longer argues for a
  hand-written web layer. flup is old but small, pure Python, and works on the webhost's Python and
  the next one; its one unsafe corner (an error page that shows tracebacks when debugging) is
  replaced.
- **The database through an established library, its changes as migrations.** The tables are
  described once, as typed models, and each schema change is a migration that is written from the
  difference and checked against the models by the tests, as in intern. SQLAlchemy with
  Flask-SQLAlchemy and Alembic fits the Flask app; SQLModel was not chosen because it is built for
  FastAPI. The release runs its migrations itself, without the developer tools.
- **A request never leaves a database lock or transaction behind.** The program that keeps running
  reuses its database connection, so whatever a failed request held is undone before the next
  request gets the connection, and an idle program gives its connection back.
- **Every release runs on a stand-in for the webhost before it is published.** Unit tests cannot
  show what Apache's rewrite rules, its FastCGI and CGI modules and the webhost's own Python do
  together. CI builds a copy of the webhost as close as it can get (the same Linux and Python
  versions, the same way of running scripts, its guesses where Hetzner's set-up is unknown),
  installs the release on it from the first-time upload, and uses it as visitors and intern would.
  What only the real webhost can show stays on a list in the runbook.
