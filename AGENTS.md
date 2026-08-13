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

- `app/routes.py` — HTTP surface.
- One maintenance module per domain: `poster_maintenance.py`, `subtitle_maintenance.py`,
  `video_preview_maintenance.py`, `actor_image_maintenance.py`, and `maintenance.py` for
  duplicate cleanup. Keep domains separate; these files are already large.
- `app/jobs.py` with `operation_gate.py` and `conversion_gate.py` — background work and the
  locks that keep concurrent operations from fighting over the same files or the GPU/CPU.
- `app/emby_*.py` — Emby integration (catalog, client, sync, playback, notifications,
  operations), isolated so the app still works without Emby configured.
- `app/ffmpeg_utils.py`, `gif_optimizer.py`, `process_runner.py` — media processing and
  subprocess handling.
- `app/media_scope.py` — decides which video in a folder is the main playable item, so
  trailers, extras, and featurettes aren't treated as the feature.
- `app/templates/` + `app/static/` — server-rendered UI on Bootstrap 5.3.

Several of these files are very large — `app/static/maintenance.js` is over 6,000 lines and
`maintenance.py` over 4,000. Prefer adding a new module over growing them further.

## Verification and delivery

```bash
pip install -r requirements-dev.txt
npm ci --ignore-scripts
python -m pytest
npm run test:frontend
npm run build:frontend
```

`npm run build:frontend` produces two bundles — `app/static/test-lab.bundle.js` and
`app/static/workspace-tables.bundle.js` — and both are checked in and served directly by
Docker and deployed instances. **Rebuild and commit them whenever anything under
`frontend/` changes**, or deployments silently run the old code. Node is only needed for
development.

CI additionally runs `python -m pip_audit -r requirements.txt` and
`npm audit --audit-level=low`; a new dependency carrying an advisory will fail the build.

Functions are capped at complexity 15 (`ruff` rule `C901`, configured in `ruff.toml`).
Existing offenders carry an explicit `# noqa: C901` and are accepted debt — new code should
not add more. The worst are `build_duplicate_cleanup_plan` (54) and
`apply_duplicate_cleanup_plan` (43), both in `app/maintenance.py`, and both on the path that
deletes files — worth untangling before they hide a bug.

For an approved change: finish all in-scope work, update the README, `DESIGN.md`,
`SECURITY.md`, or this file in the same change as the behavior they describe, commit with a
focused message, and push. Never discard unrelated uncommitted work to make a commit clean —
stash it and ask.

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
