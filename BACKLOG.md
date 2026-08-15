# BACKLOG.md

Outstanding work observed while surveying the repository. The codebase contains
no `TODO` or `FIXME` markers, so every item below was derived from reading the
code, the docs, and CI -- each one cites what it is based on.

Current state: 572 Python tests, all of which pass on CI with none skipped; a
Windows checkout runs 563 of them, since nine need symlinks or media tools. 22
frontend tests, 50 browser tests covering every page and maintenance tab with an
axe pass on each. `ruff check` is clean (including a C901 complexity ceiling and
an enforced 120-column limit), `ruff format --check` is clean, coverage is 83.09%
against an 80% floor, and CI is green on `main`.

There are no open questions. Everything below is mine to do.

## Lower priority

### 1. Several modules have outgrown one file

`app/video_preview_maintenance.py` (4,183 lines), `app/maintenance.py` (4,016),
`app/poster_maintenance.py` (2,092), `app/routes.py` (1,724), and
`app/subtitle_maintenance.py` (1,604) each hold scanning, planning, applying,
persistence, and payload shaping together. Nothing is broken, but the seams are
already visible in the module names (`duplicate_slots.py` and
`duplicate_review_store.py` were split out of `maintenance.py`).

Split opportunistically when touching one of these for another reason, not as a
standalone refactor.

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
