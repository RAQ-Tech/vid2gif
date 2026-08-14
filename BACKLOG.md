# BACKLOG.md

Outstanding work observed while surveying the repository. The codebase contains
no `TODO` or `FIXME` markers, so every item below was derived from reading the
code, the docs, and CI -- each one cites what it is based on.

Current state: 538 Python tests, all of which pass on CI with none skipped; a
Windows checkout runs 529 of them, since nine need symlinks or media tools. 19
frontend tests, 31 browser tests. `ruff check` is clean, coverage is 83.15%
against an 80% floor, and CI is green on `main`.

## Test coverage

### 1. Cover the error paths in file_safety.py

file_safety.py is the module that decides whether it is safe to touch a user's
file -- symlink rejection, identity capture, atomic install, same-filesystem
moves -- and the coverage run puts it at 75%, among the lowest in the codebase.
`subtitle_quality.py` (73%), `ffmpeg_utils.py` (75%), and `test_lab.py` (77%)
are the next lowest. `routes.py` sits at 71%, but that is mostly thin endpoint
wrappers whose logic is tested through the modules underneath.

Uncovered lines in `file_safety.py` are the error branches: unreadable files,
cross-device moves, races where the destination appears mid-operation. Those are
exactly the paths that matter when something goes wrong with someone's library.
Worth writing tests for before chasing the overall percentage.

### 2. Browser tests cover three of the seven maintenance tabs

`frontend/browser/` drives `/maintenance#duplicates` (three specs),
`/maintenance#video-previews`, `/maintenance#posters`, and the `/gifs` page.
That leaves the **subtitles**, **actor images**, **Emby operations**, and
**overview** tabs with no browser or accessibility coverage, along with the
**Dashboard**, **Settings**, **System**, and **Test Lab** pages -- even though
`DESIGN.md`'s implementation checklist expects populated real-world data on
every surface, and these are where the axe contrast and focus-order checks pay
off.

Prioritize subtitles and actor images: both perform quarantine and delete
operations through the UI.

## Repository housekeeping

### 3. Draft PR #43 now conflicts with everything merged today

[PR #43](https://github.com/RAQ-Tech/vid2gif/pull/43), "Add AGENTS.md, and give
the project a complexity linter", was opened on 2026-08-13 before this session's
work and is still a draft. It edits `CLAUDE.md`, `ruff.toml`, and
`requirements-dev.txt`, all three of which changed on `main` since -- so it will
not merge cleanly, and it proposes `AGENTS.md` as the single source of truth
while `CLAUDE.md` has been serving that role.

Decide which file is authoritative before rebasing it, and fold its complexity
linter into the existing `ruff.toml` rather than alongside it.

## Lower priority

### 4. No formatter, and 1,267 lines exceed 88 columns

`ruff check` now runs in CI, but `E501` (line too long) is switched off in
`ruff.toml`. 1,267 lines exceed ruff's default 88 columns and 133 exceed 120,
concentrated in `maintenance.py`, `video_preview_maintenance.py`, and
`poster_maintenance.py`. Rewrapping them by hand would be a large diff through
the code that moves users' files, for no behavioural gain.

Adopting `ruff format` would do it mechanically and consistently, but it would
touch nearly every Python file in one commit. Worth doing deliberately, on a
quiet branch, not folded into other work.

### 5. Several modules have outgrown one file

`app/video_preview_maintenance.py` (4,211 lines), `app/maintenance.py` (4,127),
`app/poster_maintenance.py` (1,950), `app/routes.py` (1,776), and
`app/subtitle_maintenance.py` (1,623) each hold scanning, planning, applying,
persistence, and payload shaping together. Nothing is broken, but the seams are
already visible in the module names (`duplicate_slots.py` and
`duplicate_review_store.py` were split out of `maintenance.py`).

Split opportunistically when touching one of these for another reason, not as a
standalone refactor.

### 6. Dashboard impact metrics cannot be backfilled

`README.md` states the dashboard tracks impact only from first launch after the
feature was installed and does not backfill. Existing installations therefore
show a lifetime total that understates real work. The bounded audit logs under
`/state/maintenance-logs/` hold some of the missing history.

A one-time backfill would make the lifetime number trustworthy for existing
users. Worth doing only if that number is meant to be authoritative.

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

## Open questions

These need a decision from Chris; they are not engineering calls.

### Delete the stray `C:\state` folder?

Test runs on this machine before 2026-08-13 wrote 2.1 MB of app state to
`C:\state` (created 2026-08-07, last written 2026-08-11) because `STATE_ROOT`
was unset.

Every path recorded in it points at `pytest` temp directories that no longer
exist, so the 60 duplicate-cleanup audit logs it contains cannot restore
anything; the stored settings hold no credentials, and the job queue is empty.
The dashboard file claims 815 duplicates found and 681 resolved, but that is
the test suite exercising the app, not real work -- which is the main argument
for removing it, since it reads like genuine history.

Deleting it also arms the guard added in `app/config.py`. That guard refuses to
create the `/state` default when it does not already exist -- but this folder
does exist, so on this machine the default still looks intentional and is still
written to. Removing it is what makes the fix take effect here.

It is outside the repository and deleting it is not reversible from git, so it
is left in place. Say the word and it goes.
