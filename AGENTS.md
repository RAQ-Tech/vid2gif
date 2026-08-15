# vid2gif engineering instructions

## What this is

vid2gif is a dockerized web UI for generating GIF previews from large video libraries and
for maintaining those libraries — posters, subtitles, video previews, actor images, and
duplicate cleanup. It runs on a trusted LAN, operated by one person, against mounted media
directories.

The product's visual and interaction language is defined in [`DESIGN.md`](DESIGN.md), and
the data-exposure model in [`SECURITY.md`](SECURITY.md). Both are authoritative. Read the
relevant one before changing UI or anything that touches what the app can reach.

## The one rule that matters most: this deletes people's media

This app moves, deletes, and renames files in a library the operator cannot regenerate. A
mistake here is not a bad render — it is lost footage.

- **Scan, review, apply are three separate steps.** Every maintenance domain follows the
  same shape: a `build_*_plan` function produces a plan, the operator reviews it, and a
  separate `apply_*_plan` executes it against a stored `plan_id`. Never collapse those into
  one action, and never let a scan mutate anything.
- **Show exact counts before mutating.** The operator sees how many files and groups an
  apply will touch, before confirming. A plan that hides its blast radius is a defect.
- **Never bypass `app/file_safety.py`.** It re-verifies a file's identity — real path, size,
  mtime, inode, device — immediately before acting, so a file that changed under the app
  since the scan is not clobbered. It also refuses symlinked path components and provides
  the atomic install / quarantine / no-overwrite-move primitives. Use those rather than
  `shutil` directly.
- **Everything stays under the library root.** `path_is_under` guards this in ~80 places.
  Any new path that reaches the filesystem needs the same check; never widen what a scan can
  reach without the operator explicitly configuring it.

If a change touches destructive behavior, state in the PR description exactly which files it
can affect and what stops it reaching anything else.

## Trusted-LAN only — by design

There is no authentication, no CSRF protection, and no rate limiting. That is a deliberate
scope decision, not an oversight, and it shapes everything:

- **Never add a feature that assumes an authenticated user exists.** There are no users, only
  whoever can reach the port.
- **Assume every endpoint is public to the LAN.** `POST /system/backup` streams the whole
  `/state` directory to any caller, which is why the Emby API key is blanked out of the
  archive and listed under `redacted` in the manifest. Any new stored credential must be
  blanked the same way, or it leaves the machine in a backup.
- Anything that would change this posture — auth, internet exposure, stricter file serving —
  is a product decision, not an implementation detail. Raise it rather than building it.

## Architecture and code organization

Two mounted volumes define everything: `LIB_ROOT` (`/library`) is the operator's media, read
mostly and written only through deliberate reviewed operations; `STATE_ROOT` (`/state`) is
all app state — settings, logs, job queue, scan caches, audit logs, metrics. Nothing
persists outside those two.

**Entry points.** `app/routes.py` defines the Flask app and every route but starts nothing.
`app/wsgi.py` (production) and `app/main.py` (dev) import it and then call `start_worker()`,
`start_test_lab_worker()`, and `start_landscape_poster_worker()`. Importing `app.routes`
alone gives a working test client with no workers — which is exactly what the tests do.

**Pages.** Five templates in `app/templates/` (dashboard, gifs, maintenance, settings,
system) over 108 JSON endpoints, 124 routes in all. The Test Lab lives inside the GIFs page;
Maintenance is a seven-tab workbench, rendered on Bootstrap 5.3.

**Per-workflow modules.** Each maintenance workstream is one module owning its own
scan/plan/apply state and its own `/state` subdirectory. Keep domains separate; several of
these files are already very large (`app/static/maintenance.js` is over 6,000 lines,
`maintenance.py` over 4,000), so prefer a new module over growing them further.

| Module | Workstream |
| --- | --- |
| `jobs.py` | GIF job queue and worker |
| `test_lab.py` | Side-by-side GIF variant comparison |
| `maintenance.py` + `duplicate_slots.py` + `duplicate_review_store.py` | Duplicate cleanup |
| `poster_maintenance.py` | Landscape poster replacement |
| `video_preview_maintenance.py` | BIF preview scan, quarantine, generation |
| `subtitle_maintenance.py` + `subtitle_quality.py` | Subtitle coverage and language |
| `actor_image_maintenance.py` | Actor image import |
| `emby_*.py` | Emby catalog, client, sync, playback, notifications — isolated so the app still works without Emby configured |
| `dashboard.py` + `impact_metrics.py` | Dashboard aggregation and lifetime metrics |

**Shared infrastructure — use these rather than reinventing them:**

- `config.py` — every environment variable, read once at import.
- `app_settings.py` — user settings persisted to `/state/app_settings.json`, with a
  `SCHEMA_VERSION` and migrations. Bump it when the shape changes.
- `file_safety.py` — identity capture (size, mtime, inode, device), symlink rejection,
  atomic install, same-filesystem link-and-unlink moves.
- `operation_gate.py` / `conversion_gate.py` — FIFO coordination so scans, maintenance
  writes, BIF generation, and GIF conversion never compete for the same library disks.
- `process_runner.py` — streaming subprocess execution with cancellation, stall detection,
  bounded output.
- `progress.py` / `task_progress.py` — progress payload and duration formatting.
- `media_scope.py` — decides whether a file is a main video or a trailer/extra/sample, so
  extras aren't treated as the feature. All maintenance scans should respect it.
- `utils.py` — `path_is_under()` is the containment check; use it before touching any
  user-supplied path.
- `ffmpeg_utils.py`, `gif_optimizer.py` — media processing.

**Concurrency.** All live state is module-level dicts guarded by `threading.Lock`, and
workers are daemon threads. The container therefore runs `--workers 1 --threads 8`. **Do not
add a second gunicorn worker without moving state out of process memory first** — a second
worker would silently get its own copy of every scan, plan, and job queue.

## Verification and delivery

```bash
pip install -r requirements-dev.txt
npm ci --ignore-scripts
python -m pytest                              # 563 tests, ~35s; safe to run bare
python -m ruff check .                        # what CI lints with
python -m ruff format .                       # run before committing; CI checks it
npm run test:frontend                         # 19 Node tests, needs no node_modules
npm run build:frontend
python -m app.main                            # dev server on port 904, workers started
```

CI runs pytest under coverage with a floor: `python -m pytest --cov=app
--cov-report=term-missing --cov-fail-under=80`. Coverage sits around 83%. Raise the floor as
coverage improves — never lower it to accommodate a regression.

**`STATE_ROOT` is why `tests/conftest.py` exists.** `app/config.py` creates its directories
at import time, so any `import app.*` has a filesystem side effect before a single test runs
— a stray folder at the drive root on Windows, an outright failure on Linux. `conftest.py`
points `STATE_ROOT` at a temp directory before the first `app` import, so a bare
`python -m pytest` is now safe. An explicitly exported `STATE_ROOT` still wins.

**Do not set `LIB_ROOT` when running the suite.** `conftest.py` deliberately leaves it alone:
nothing creates it at import time, and several tests assert against the `/library` container
path directly, so an override fails them. Set it only when actually serving a library.

`npm run test:browser` runs Playwright + axe accessibility checks against a real Flask server
on port 19040; it additionally needs `npx playwright install --with-deps chromium` and a
`.venv`. Fifty tests cover every page and every maintenance tab, each with an axe pass on
populated data — empty states hide the contrast and labelling defects that matter.

`npm run build:frontend` first vendors third-party assets (`npm run vendor:assets`), then
bundles `frontend/test-lab/` and `frontend/tables/` into `app/static/`. **Rebuild and commit
everything under `app/static/`** whenever you change anything under `frontend/` or the pinned
frontend packages — CI runs `git diff --exit-code -- app/static`, so the checked-in bundles
and `app/static/vendor/` must match a fresh build. Node is only needed for development, never
at runtime in the deployed container.

CI additionally runs `python -m pip_audit` over both requirements files and
`npm audit --audit-level=low`; a new dependency carrying an advisory will fail the build. The
image publishes to GHCR only if every one of those steps passed.

`ruff format` owns layout. CI runs `ruff format --check .`, so hand-aligned code will fail
the build -- run the formatter rather than arguing with it. Adopting it was a 5,000-line
mechanical diff across 59 files, verified by comparing every file's parsed syntax tree
before and after: all 83 came back identical, so no behaviour changed. Lines are capped at
120 columns and that cap is now enforced (`E501`); the formatter holds it, and the only
three lines it could not wrap were long f-strings, split by hand.

Functions are capped at complexity 15 (`ruff` rule `C901`, in `ruff.toml` alongside the
pycodestyle/pyflakes/bugbear selection). Existing offenders carry an explicit `# noqa: C901`
and are accepted debt — new code should not add more. The worst are
`build_duplicate_cleanup_plan` (54) and `apply_duplicate_cleanup_plan` (43), both in
`app/maintenance.py`, and both on the path that deletes files — worth untangling before they
hide a bug.

**Tests live in `tests/`, one file per module**, as plain pytest functions using
`monkeypatch` and `tmp_path`. `tests/conftest.py` exists only to default `STATE_ROOT` — it
defines no fixtures, and each test file builds and resets its own state. Follow the existing
patterns rather than introducing shared fixtures.

**Runtime dependencies go in `requirements.txt`, dev-only tools in `requirements-dev.txt`.**
A test asserts the split, so putting a dev tool in the runtime file fails the suite. Python
only: Node is a frontend build tool, never a runtime dependency of the deployed container.

For an approved change: finish all in-scope work, update the README, `DESIGN.md`,
`SECURITY.md`, or this file in the same change as the behavior they describe, commit with a
focused message, and push. Commit messages are a short imperative sentence describing the
user-visible outcome ("Keep partial previews recovered from damaged videos"), not the
mechanism. Never discard unrelated uncommitted work to make a commit clean — stash it and ask.

## Traps that will waste your time

These are environment quirks, not bugs to fix. Each has cost someone an afternoon.

- **`/healthz` returns 503 on a normal dev machine.** It fails when ffmpeg, ffprobe, or the
  three worker threads are absent, which is the usual state outside Docker. Pages still
  render — this is not a broken checkout.
- **ffmpeg, ffprobe, and gifsicle are not Python packages.** They must be on `PATH`. The
  Docker image installs them; a local checkout may not have them, and a handful of tests skip
  themselves when they are missing.
- **`playwright.config.js` hard-codes `.venv/Scripts/python.exe` on Windows.** Browser tests
  need a virtualenv at `.venv`, or `VID2GIF_TEST_PYTHON` pointing at a Python that has Flask
  installed.
- **`app/wsgi.py` re-exports `app` via `__all__`.** It reads as an unused import, but it is
  the WSGI callable gunicorn loads — the Dockerfile runs `app.wsgi:app`. Do not let a linter
  or a tidy-up delete the production entry point.
- **Third-party assets are vendored, not fetched.** Bootstrap, Bootstrap Icons, and Inter
  live under `app/static/vendor/`, copied by `scripts/vendor-assets.mjs` from the packages
  pinned in `package-lock.json`. Add new assets the same way — a test fails the build if a CDN
  host reappears in a template, and `app/static/vendor/**` is marked `-text` in
  `.gitattributes` so the bytes stay identical to the upstream package.

## Repository privacy — never commit personal data

- Real library paths, mount points, hostnames, or the operator's folder structure. Use
  placeholders in docs and config examples.
- Any media file, poster, GIF, or screenshot of a real library. Use generated test fixtures.
- Emby API keys or any other credential. These belong in runtime settings under `/state`,
  never in git.

## Model/agent working notes

This project is expected to involve more than one AI coding tool over time. `AGENTS.md` is
the single source of truth read by all of them; `CLAUDE.md` exists only as a pointer to this
file — do not duplicate instructions into it.

Cheap/fast models are appropriate for mechanical work: templates, table rendering, settings
plumbing, tests for well-specified behavior. Escalate to a more capable model for anything
touching duplicate cleanup, `file_safety.py`, path scoping, or the apply half of any
maintenance plan — those are the paths that can destroy data.
