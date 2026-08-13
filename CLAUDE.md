# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

vid2gif is a Dockerized Flask web app for a trusted private LAN. It generates
animated `poster.gif` previews from a mounted video library and runs
review-first maintenance workflows over that library (duplicate cleanup,
landscape posters, BIF video previews, subtitles, actor images), with optional
Emby integration.

Two mounted volumes define everything:

- `LIB_ROOT` (`/library`) — the user's media. Read mostly; written only through
  deliberate, reviewed operations.
- `STATE_ROOT` (`/state`) — all app state: settings, logs, job queue, scan
  caches, audit logs, dashboard metrics. Nothing persists outside it.

## Commands

Run these from the repository root.

```bash
python -m pytest
```

520 Python tests, ~35s. **Set `STATE_ROOT` first** (see Gotchas) or the import of
`app/config.py` will try to create `/state` on the real filesystem.

```bash
npm run test:frontend
```

19 Node unit tests for `frontend/`. Needs no `node_modules` — it only imports
local modules and Node built-ins.

```bash
npm run build:frontend
```

Bundles `frontend/test-lab/` and `frontend/tables/` into `app/static/*.bundle.js`
with esbuild. Requires `npm ci --ignore-scripts`.

```bash
npm run test:browser
```

Playwright + axe accessibility checks against a real Flask server on port 19040.
Requires `npm ci --ignore-scripts`, `npx playwright install --with-deps chromium`,
and a `.venv` (see Gotchas).

```bash
python -m app.main
```

Runs the dev server on port 904 with all three background workers started.

```bash
docker build -t vid2gif . && docker run -p 904:904 -v /path/to/videos:/library -v /path/to/state:/state vid2gif
```

Production shape: gunicorn serving `app.wsgi:app`, with ffmpeg, ffprobe, and
gifsicle installed in the image.

CI (`.github/workflows/ci.yml`) runs pip-audit, npm audit, the frontend tests,
the frontend build, `git diff --exit-code` on the built bundles, pytest, and the
browser tests — then publishes the image to GHCR only if all of that passed.

## Architecture

**Entry points.** `app/routes.py` defines the Flask app and every route but
starts nothing. `app/wsgi.py` (production) and `app/main.py` (dev) import it and
then call `start_worker()`, `start_test_lab_worker()`, and
`start_landscape_poster_worker()`. Importing `app.routes` alone gives you a
working test client with no workers — which is exactly what the tests do.

**Pages.** Five templates in `app/templates/` (dashboard, gifs, maintenance,
settings, system) over 108 JSON endpoints (124 routes in all). The Test Lab lives inside the GIFs
page; Maintenance is a seven-tab workbench.

**Per-workflow modules.** Each maintenance workstream is one module owning its
own scan/plan/apply state and its own `/state` subdirectory:

| Module | Workstream |
| --- | --- |
| `jobs.py` | GIF job queue and worker |
| `test_lab.py` | Side-by-side GIF variant comparison |
| `maintenance.py` + `duplicate_slots.py` + `duplicate_review_store.py` | Duplicate cleanup |
| `poster_maintenance.py` | Landscape poster replacement |
| `video_preview_maintenance.py` | BIF preview scan, quarantine, generation |
| `subtitle_maintenance.py` + `subtitle_quality.py` | Subtitle coverage and language |
| `actor_image_maintenance.py` | Actor image import |
| `emby_*.py` | Emby catalog, client, sync, playback, notifications |
| `dashboard.py` + `impact_metrics.py` | Dashboard aggregation and lifetime metrics |

**Shared infrastructure.** Use these rather than reinventing them:

- `config.py` — every environment variable, read once at import.
- `app_settings.py` — user settings persisted to `/state/app_settings.json`,
  with a `SCHEMA_VERSION` and migrations. Bump it when the shape changes.
- `file_safety.py` — identity capture (size, mtime, inode, device), symlink
  rejection, atomic install, same-filesystem link-and-unlink moves.
- `operation_gate.py` / `conversion_gate.py` — FIFO coordination so scans,
  maintenance writes, BIF generation, and GIF conversion never compete for the
  same library disks.
- `process_runner.py` — streaming subprocess execution with cancellation, stall
  detection, bounded output.
- `progress.py` / `task_progress.py` — progress payload and duration formatting.
- `media_scope.py` — decides whether a file is a main video or a trailer/extra/
  sample. All maintenance scans should respect it.
- `utils.py` — `path_is_under()` is the containment check; use it before
  touching any user-supplied path.

**Concurrency.** All live state is module-level dicts guarded by
`threading.Lock`, and workers are daemon threads. The container therefore runs
`--workers 1 --threads 8`. Do not add a second gunicorn worker without moving
state out of process memory first.

## Conventions

- **Safety before convenience.** Detection, review, and mutation are separate
  steps. Capture file identity when a plan is built and refuse to apply if it
  changed. Quarantine (a reversible move) is the default; permanent deletion is
  always an explicit, separately-styled choice.
- **Read `DESIGN.md` before any user-facing UI work.** It is a real design
  system with an enforced implementation checklist, not decoration.
- **Tests live in `tests/`, one file per module**, using plain pytest functions
  with `monkeypatch` and `tmp_path`. There is no `conftest.py`; each test file
  builds and resets its own state. Follow the existing patterns.
- **Rebuild and commit `app/static/*.bundle.js`** whenever you change anything
  under `frontend/test-lab/` or `frontend/tables/`. CI fails if the checked-in
  bundle differs from a fresh build.
- **Runtime dependencies go in `requirements.txt`; dev-only tools in
  `requirements-dev.txt`.** A test asserts the split.
- PEP 8, and Python only — Node is a frontend build tool, never a runtime
  dependency of the deployed container.
- Commit messages are a short imperative sentence describing the user-visible
  outcome ("Keep partial previews recovered from damaged videos"), not the
  mechanism.

## Gotchas

- **`app/config.py:41` creates directories at import time.** Any `import app.*`
  will `mkdir` under `STATE_ROOT`, defaulting to `/state` — meaning `C:\state`
  on Windows or a permission error on Linux. Always export `STATE_ROOT` (and
  usually `LIB_ROOT`) to a scratch directory before running Python locally. CI
  sets `STATE_ROOT=/tmp/vid2gif-state`.
- **`/healthz` returns 503 on a normal dev machine.** It fails when ffmpeg,
  ffprobe, or the three worker threads are absent, which is the usual state
  outside Docker. Pages still render.
- **`playwright.config.js:14` hard-codes `.venv/Scripts/python.exe` on Windows.**
  Browser tests need a virtualenv at `.venv`, or `VID2GIF_TEST_PYTHON` pointing
  at a Python that has Flask installed.
- **Line endings.** `.gitattributes` forces LF for source files, but this
  Windows checkout has `core.autocrlf=true` and `.gitattributes` does not cover
  `*.txt` or `*.css`. Rebuilding the frontend makes
  `app/static/test-lab.bundle.js.LEGAL.txt` show as modified in `git status`
  even when its content is identical; `git checkout --` on it is safe.
- **The UI loads Bootstrap, Bootstrap Icons, and Inter from public CDNs**
  (`app/templates/base.html:8-9,63`). The app will look broken on a genuinely
  offline network.
- **ffmpeg, ffprobe, and gifsicle are not Python packages.** They must be on
  `PATH`. The Docker image installs them; a local checkout may not have them,
  and a handful of tests skip themselves when they are missing.
