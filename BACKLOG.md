# BACKLOG.md

Outstanding work observed while surveying the repository. The codebase contains
no `TODO` or `FIXME` markers, so every item below was derived from reading the
code, the docs, and CI -- each one cites what it is based on.

Current state: 563 Python tests, all of which pass on CI with none skipped; a
Windows checkout runs 554 of them, since nine need symlinks or media tools. 19
frontend tests, 50 browser tests covering every page and maintenance tab with an
axe pass on each. `ruff check` is clean (including a C901 complexity ceiling),
coverage is 83.04% against an 80% floor, and CI is green on `main`.

There are no open questions. Everything below is mine to do.

## Lower priority

### 1. No formatter, and 1,267 lines exceed 88 columns

`ruff check` now runs in CI with pycodestyle, pyflakes, bugbear and a C901
complexity ceiling, but `E501` (line too long) is switched off in `ruff.toml`.
1,267 lines exceed ruff's default 88 columns and 133 exceed 120, concentrated in
`maintenance.py`, `video_preview_maintenance.py`, and `poster_maintenance.py`.
Rewrapping them by hand would be a large diff through the code that moves users'
files, for no behavioural gain.

Adopting `ruff format` would do it mechanically and consistently, but it would
touch nearly every Python file in one commit. Worth doing deliberately, on a
quiet branch, not folded into other work.

### 2. Several modules have outgrown one file

`app/video_preview_maintenance.py` (4,211 lines), `app/maintenance.py` (4,127),
`app/poster_maintenance.py` (1,950), `app/routes.py` (1,776), and
`app/subtitle_maintenance.py` (1,623) each hold scanning, planning, applying,
persistence, and payload shaping together. Nothing is broken, but the seams are
already visible in the module names (`duplicate_slots.py` and
`duplicate_review_store.py` were split out of `maintenance.py`).

Split opportunistically when touching one of these for another reason, not as a
standalone refactor.

### 3. Dashboard impact metrics cannot be backfilled

`README.md` states the dashboard tracks impact only from first launch after the
feature was installed and does not backfill. Existing installations therefore
show a lifetime total that understates real work. The bounded audit logs under
`/state/maintenance-logs/` hold some of the missing history.

Decided 2026-08-14: the figure is meant to be a true lifetime record, so
backfill what the logs can support. Anything that was never tracked is simply
gone -- do not invent it, and do not hold up the rest of the work over it.
Whatever cannot be recovered should be stated on the dashboard rather than
quietly folded into the total.

## Deliberately not on this list

- **Authentication, CSRF, and rate limiting.** Absent by design, not by
  oversight: `SECURITY.md` and `README.md` both state vid2gif is for a trusted
  private LAN and list what would need adding before any internet-facing use.
  That is a product decision, not outstanding work.
- **`/healthz` returning 503 outside Docker.** Correct behaviour -- it fails
  when ffmpeg, ffprobe, or the worker threads are absent, which is the normal
  state of a dev machine.
- **0% coverage on `app/main.py` and `app/wsgi.py`.** They are process entry
  points that start daemon threads; importing them under test would start
  workers.
