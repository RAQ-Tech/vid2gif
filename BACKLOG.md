# BACKLOG.md

Outstanding work observed while surveying the repository. The codebase contains
no `TODO` or `FIXME` markers, so every item below was derived from reading the
code, the docs, and CI — each one cites what it is based on.

Nothing here is a known-broken feature. The test suite passes in full
(520 Python tests, 19 frontend tests) and the checked-in bundles reproduce
exactly from source.

## Correctness and deployment risk

### 1. Nothing enforces the single-gunicorn-worker constraint

All job, scan, and plan state lives in module-level dictionaries guarded by
`threading.Lock` (`app/jobs.py`, and every `*_maintenance.py`). `Dockerfile:32`
consequently pins `--workers 1 --threads 8`. That coupling is invisible: raising
the worker count to use more CPU would split the queue across processes and
silently break progress reporting, cancellation, and duplicate-apply safety.

Document the constraint next to the `CMD`, and consider a startup check that
refuses to run with more than one worker.

### 2. The Emby API key is stored in plaintext at rest

`app/app_settings.py:131` persists `emby_api_key` into
`/state/app_settings.json`. The `/system/backup` endpoint already redacts it
from downloaded archives (`SECURITY.md`), but anyone with read access to the
`/state` volume still sees the raw key.

At minimum note this in `SECURITY.md` alongside the backup redaction, so the
protection is not mistaken for encryption at rest.

## Developer experience

### 3. No linter or formatter anywhere

`README.md` asks contributors to follow PEP 8, but nothing checks it: there is
no ruff, flake8, or black in `requirements-dev.txt`, and no lint step in
`.github/workflows/ci.yml`. Style drift across 45,000 lines is left to review.

Add ruff to `requirements-dev.txt` and a CI step. Expect a first pass of
mechanical fixes.

### 4. No coverage measurement

The suite is large and well-structured, but nothing reports which branches of
the safety-critical modules (`file_safety.py`, `maintenance.py`,
`video_preview_maintenance.py`) are actually exercised.

Add `pytest-cov` and report the number in CI, without gating on it initially.

## Test coverage gaps

### 5. Browser tests cover four of the seven maintenance tabs

`frontend/browser/` has specs for posters, duplicates, duplicate slots, restore,
BIF, the activity strip, and GIF job creation. There is no browser or
accessibility coverage for the **subtitles**, **actor images**,
**Emby operations**, or **overview** tabs, nor for the **Dashboard**,
**Settings**, **System**, or **Test Lab** pages — even though `DESIGN.md`'s
implementation checklist expects populated real-world data on every surface, and
these are where the axe contrast and focus-order checks pay off.

Prioritize subtitles and actor images: both perform quarantine and delete
operations through the UI.

## Documentation

### 6. Actor image maintenance is entirely undocumented

`app/actor_image_maintenance.py` is 1,537 lines with a full Maintenance tab, a
dashboard workstream, eleven API endpoints, and 338 lines of tests. `README.md`
mentions it zero times, while every other workstream gets several paragraphs. A
user cannot discover what the feature does, what it writes, or whether it is
safe.

Add a README section matching the depth of the duplicate and subtitle sections,
including what it writes to the library.

## Lower priority

### 7. `TEMPLATES_AUTO_RELOAD` is on in production

`app/routes.py:59` sets `app.config["TEMPLATES_AUTO_RELOAD"] = True`
unconditionally, so the container stats every template on every request. Harmless
at LAN scale, but it is dev configuration shipped to production. Gate it on a
debug flag.

### 8. Several modules have outgrown one file

`app/video_preview_maintenance.py` (4,211 lines), `app/maintenance.py` (4,127),
`app/poster_maintenance.py` (1,950), `app/routes.py` (1,776), and
`app/subtitle_maintenance.py` (1,623) each hold scanning, planning, applying,
persistence, and payload shaping together. Nothing is broken, but the seams are
already visible in the module names (`duplicate_slots.py` and
`duplicate_review_store.py` were split out of `maintenance.py`).

Split opportunistically when touching one of these for another reason, not as a
standalone refactor.

### 9. Dashboard impact metrics cannot be backfilled

`README.md` states the dashboard tracks impact only from first launch after the
feature was installed and does not backfill. Existing installations therefore
show a lifetime total that understates real work. The bounded audit logs under
`/state/maintenance-logs/` hold some of the missing history.

A one-time backfill would make the lifetime number trustworthy for existing
users. Worth doing only if that number is meant to be authoritative.

### 10. The maintenance page polls an endpoint that 404s when idle

`/api/maintenance/duplicates/refresh/status` returns 404 whenever no duplicate
refresh run exists (`app/routes.py:953-958`), but `maintenance.js` polls it on
every page load. Opening Library Maintenance therefore logs two console errors
before the user does anything. Observed live in the browser, not inferred.

Return an idle payload instead, the way the sibling status endpoints do, and
reserve 404 for a `refresh_id` that was genuinely never issued.

## Open questions

These need a decision from Chris; they are not engineering calls.

### Delete the stray `C:\state` folder?

Test runs on this machine before 2026-08-13 wrote 2.1 MB of app state to
`C:\state` (created 2026-08-07, last written 2026-08-11) because `STATE_ROOT`
was unset. `tests/conftest.py` now prevents new ones. The folder holds only test
leftovers as far as I can tell — settings, logs, an empty job queue — but it is
outside the repository and deleting it is not reversible from git, so it is left
in place. Say the word and it goes.
