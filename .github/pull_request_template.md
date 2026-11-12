<!-- Keep the title short and imperative, e.g. "Show the next three events on the front page". -->

## Summary

<!-- What does this PR change and why? 1–3 sentences. Link any issues or the spec it follows. -->

-

## Type of change

<!-- Delete the ones that don't apply. -->

- Feature
- Bugfix
- Refactor / cleanup
- Tests / tooling
- Docs
- Infra / deploy

## How was this tested?

<!-- Describe how you verified the change. -->

- [ ] `task test` (needs `task db:create` once)
- [ ] `task lint` and `task typecheck`
- [ ] `task bundle` (build + smoke test), if the change touches the updater, the stub, `.htaccess` or dependencies
- [ ] Manually checked the affected pages with `task dev` (describe below)

<!-- Manual verification notes: -->

## Checklist

- [ ] A schema change is a model change in `models.py` plus a new Alembic revision in `src/gahk_web/migrations/versions/` (`task migration`), and still works with the previous release (the updater migrates before it switches)
- [ ] A change to the sync API or the snapshot is matched in gahk_intern and in `docs/protocol.md`
- [ ] A new runtime dependency has binary wheels for CPython 3.13 and 3.14 on manylinux x86_64 (the bundle job checks this)
- [ ] No secrets committed — configuration lives in `config.ini` on the webhost, never in git
- [ ] Docs updated if behaviour, config or deploy changed (README / DEPLOY.md / spec)
- [ ] User-facing text is in Danish

## Screenshots / notes

<!-- Optional: screenshots at phone and desktop width, follow-ups, or anything a reviewer should know. -->
