# Changelog

Staying on `0.x` until the project's sprints are done.

## v0.2.0 — 2026-10-25

Marks sprint 2 complete: the app is standalone, and the standalone install is
now *proven* rather than asserted — `curl compose.yml && docker compose up -d`
on a clean Debian 13 VM with docker and no knowledge of the homelab catalog.
HTTP 200 in 2s, the volume resolved to `transcribe_data`, the version label read
back off the image, and `compose stop` exited 0 immediately instead of 137 after
the grace period.

### Fixed
- The release workflow published a `latest` tag despite a comment saying it
  would not: `docker/metadata-action` defaults to `latest=auto`, which ignores
  what the `tags` list asks for. This is the first release without one.
- `make smoke` could not run without a venv, because `PY :=` is a simple
  assignment and make does not let the environment override those — so CI's
  `PY: python` was silently discarded. The guards used `test -x`, which cannot
  answer for a bare command name either; both now use `command -v`.
- README documented `GET /api/transcript/<id>` as returning `text`. The key is
  `transcript`. (The error was inherited from the old project notes, and found
  by a test asserting the documented name.)

## v0.1.0 — 2026-10-25

First release as a standalone app. Previously it lived inside the `homelab`
catalog as `apps/transcribe/files/`, where it was built from source on the
target machine and tagged `:latest`.

### Fixed
- **Could only ever be SIGKILLed.** `python app.py` runs as PID 1, and PID 1
  ignores any signal with no registered handler — so `docker stop` sent SIGTERM,
  nothing happened, and after the 10s grace period the container was killed:
  exit 137 every time, with no clean SQLite close. Now installs SIGTERM/SIGINT
  handlers, and `compose.yml` sets `init: true` and `stop_grace_period`.
- `OMP_NUM_THREADS` was an `ENV` with no matching `ARG` in the Dockerfile, so it
  baked one fixed value into the image instead of following `WHISPER_THREADS`.
  It is set at runtime now.

### Removed
- `Response` from the Flask imports — it was imported and never used.
  (`requests` looks unused too, but is not: faster-whisper 1.1.0 imports it in
  `utils.py` without declaring it, so dropping it breaks startup.)

### Known issues
- The v0.1.0 release also published a `latest` tag, which this project does not
  want: `docker/metadata-action`'s default flavor adds one on any semver push.
  Suppressed with `flavor: latest=false` from the next release on. The existing
  `latest` will stay pinned to v0.1.0 and go stale; deleting it needs a token
  with `write:packages`.
- Search finds recordings but every snippet is blank: the FTS5 table is
  contentless (`content=''`), so `snippet()` has no text to build from and
  returns NULL. The fix is an external-content table
  (`content='recordings', content_rowid='id'`).
