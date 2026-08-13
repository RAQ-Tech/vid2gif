# BACKLOG.md

Outstanding work observed while surveying the repository. The codebase contains
no `TODO` or `FIXME` markers, so every item below was derived from reading the
code, the docs, and CI — each one cites what it is based on.

Nothing here is a known-broken feature. The test suite passes in full
(520 Python tests, 19 frontend tests) and the checked-in bundles reproduce
exactly from source.

## Correctness

### 1. Actor name matching drops accents instead of folding them

`normalize_actor_name` (`app/actor_image_maintenance.py:90-94`) strips anything
outside `[a-z0-9 ]`, so `Amélie` normalizes to `amlie` while `Amelie`
normalizes to `amelie` -- they do not match. Verified by running the function.
For a library with international names this silently produces "no candidate" for
actors whose image is sitting right there under a differently-spelled filename.

Folding with `unicodedata.normalize("NFKD", ...)` before stripping would match
both spellings. Cheap to do; it changes matching behaviour, so it wants a test
per direction.

## Test coverage

### 2. No coverage measurement

The suite is large and well-structured, but nothing reports which branches of
the safety-critical modules (`file_safety.py`, `maintenance.py`,
`video_preview_maintenance.py`) are actually exercised.

Add `pytest-cov` and report the number in CI, without gating on it initially.

### 3. Browser tests cover four of the seven maintenance tabs

`frontend/browser/` has specs for posters, duplicates, duplicate slots, restore,
BIF, the activity strip, and GIF job creation. There is no browser or
accessibility coverage for the **subtitles**, **actor images**,
**Emby operations**, or **overview** tabs, nor for the **Dashboard**,
**Settings**, **System**, or **Test Lab** pages — even though `DESIGN.md`'s
implementation checklist expects populated real-world data on every surface, and
these are where the axe contrast and focus-order checks pay off.

Prioritize subtitles and actor images: both perform quarantine and delete
operations through the UI.

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
