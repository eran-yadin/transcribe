# Changelog

Staying on `0.x` until the project's sprints are done.

## v0.3.0 — 2026-10-25

New interface, approved from a preview before any template was touched. Coffee
palette in both light and dark, hairline rows instead of cards, search and
filters on top, settings pinned at the bottom.

### Added
- A shared `_layout.html`, so the chrome lives in one place instead of being
  pasted into three pages.
- Theme toggle, remembered per browser. The palette is defined as tokens on
  bare `:root`, redefined under `prefers-color-scheme: dark` guarded against an
  explicit light choice, and again under `[data-theme="dark"]`, so an explicit
  choice wins over the OS in both directions.
- Hebrew is handled rather than hoped for: every filename, segment and excerpt
  carries `dir="auto"`, so Hebrew lays out RTL while an English filename in the
  same list stays LTR.
- Filters (status, language, sort) run in the page. It already holds every row,
  so a round-trip to re-render for a dropdown would be the slower option.
- Segment highlighting follows the audio, and clicking a timestamp seeks.
  `timeupdate` fires several times a second, so it walks from the last known
  index instead of rescanning every segment.
- Upload posts straight from the file input, so adding a recording on a phone
  is one gesture rather than pick-then-press. Drag and drop still works.
- Search results show an excerpt, and the no-match page explains the stem rule
  (`פגיש` finds `פגישה` and `פגישת`; `פגישה` finds neither).

### Changed
- Pages poll `/api/status` **only while something is pending or transcribing**,
  and stop as soon as it is not. An always-on timer is idle cost a 2-core box
  should not pay.
- The gradient is painted on one fixed, composited layer rather than on `body`
  with `background-attachment: fixed`, which costs a repaint per scroll frame on
  several mobile browsers.

### Known issues
- Search snippets are still not highlighted. `snippet()` returns NULL because
  `search` is a contentless fts5 table with no text to build from; the result
  list falls back to a leading excerpt. The real fix is an external-content
  table, which would also make recordings deletable — see below.
- **A recording cannot be deleted through the app**: no route, no button. And
  the contentless fts5 table refuses `DELETE` outright
  (`cannot DELETE from contentless fts5 table`), so removing one by hand needs
  the fts5 `'delete'` command with the original column values handed back. The
  same `content=''` decision causes both this and the blank snippets.

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
