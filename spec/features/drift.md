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
  - pass its own self-test on the webhost's Python,
  - apply its database changes.
- If any check fails, the running version stays live. The overview shows the error.
- The switch to a new version is a single step, so visitors never meet half of an update. An update
  that takes long finishes after intern's request has been answered.
- The Hjemmeside overview shows the version gahk.dk runs, the newest one available, and how the last
  update went.
- An administrator can hold the site at an earlier version (rollback) with one line in the webhost's
  configuration. The last three versions stay on the webhost, and the last five on GitHub.

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
- Bringing it back takes a database, the configuration file, and three files uploaded once. The
  first update installs the rest.

## Decisions

- **No cron job on the webhost.** A forgotten or silently failing scheduled job is the classic way
  for a small site to rot. intern already runs a scheduler, so the webhost waits to be asked.
- **Pull from GitHub, not push to the webhost.** No deploy credentials exist anywhere. Even with
  the shared key, an update request can only make the webhost fetch the official release, never
  install code of its own.
- **Self-test on the real host before switching.** The webhost's Python changes with Hetzner's
  upgrades (3.11 to 3.13 in 2026), so each release proves it runs on the Python it lands on.
- **Pure-Python dependencies only.** Nothing compiled, so an upgrade of the host's Python cannot
  break a release that is already installed.
