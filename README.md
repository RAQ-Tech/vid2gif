# vid2gif

Dockerized Web UI for generating GIF previews from large video libraries.

## Security Notice

vid2gif is intended for trusted private networks only. Do not expose it directly
to the public internet.

The app can browse mounted library directories, shows video paths and file names
to users of the Web UI, can write `poster.gif` next to selected videos, can
replace matching Emby poster images, and can move, delete, or rename confirmed
duplicate-cleanup files during library maintenance. Run it behind a firewall or
private reverse proxy, and only mount media directories that the container
should be allowed to inspect and write to.

If you need internet-facing access, add authentication, CSRF protection, rate
limiting, stricter file-serving rules, and reverse-proxy hardening before
deployment.

The System page can download the whole `/state` directory as a zip. That
endpoint is unauthenticated like the rest of the app, so stored credentials
(currently the Emby API key) are blanked out of the archive before it is sent
and are listed under `redacted` in the backup manifest. Restoring a backup
therefore requires re-entering the Emby API key on the Settings page. That
redaction covers the archive only -- the key itself is stored in plain text in
`/state/app_settings.json`, so treat the `/state` volume as sensitive. See
[`SECURITY.md`](SECURITY.md) for the full data-exposure model.

## Testing

Install development dependencies, audit runtime dependencies, build the checked-in
Test Lab bundle, and run tests:

```bash
pip install -r requirements-dev.txt
npm ci --ignore-scripts
python -m pip_audit -r requirements.txt
python -m pip_audit -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
npm audit --audit-level=low
npm run test:frontend
npm run build:frontend
python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=80
```

Node.js is only required for frontend development. Docker and deployed instances
serve the generated `app/static/test-lab.bundle.js` file directly.

`tests/conftest.py` points `STATE_ROOT` at a temporary directory before any
`app` module is imported, so the suite never writes to the real `/state`. Export
`STATE_ROOT` yourself to override that.

Browser tests start a Flask server from a virtualenv at `.venv`. If your
interpreter lives somewhere else, point `VID2GIF_TEST_PYTHON` at it:

```bash
VID2GIF_TEST_PYTHON=/path/to/python npm run test:browser
```

## Installation

### Local Python

1. Clone the repository and install dependencies:

   ```bash
   git clone https://github.com/RAQ-Tech/vid2gif.git
   cd vid2gif
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   On Windows, activate the virtualenv with `.venv\Scripts\Activate.ps1`
   (PowerShell) or `.venv\Scripts\activate.bat` (Command Prompt) instead.

   ffmpeg, ffprobe, and gifsicle are not Python packages and must be installed
   separately and available on `PATH`. Without them the app still starts and
   every page renders, but `/healthz` reports unhealthy and GIF generation
   fails. The Docker image installs all three.

2. Launch the application:

   ```bash
   python -m app.main
   ```

### Docker

1. Build the container:

   ```bash
   docker build -t vid2gif .
   ```

2. Run the service, binding your video library and state directories:

   ```bash
   docker run \
     -p 904:904 \
     -e PUID=99 \
     -e PGID=100 \
     -e UMASK=002 \
     -v /path/to/videos:/library \
     -v /path/to/state:/state \
     vid2gif
   ```

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUID` | `99` | User ID the app runs as in Docker |
| `PGID` | `100` | Group ID the app runs as in Docker |
| `UMASK` | `002` | Create group-writable files and directories for shared media-library access |
| `LIB_ROOT` | `/library` | Location of the video library |
| `STATE_ROOT` | `/state` | Base directory for logs and temporary output |
| `LOG_DIR` | `/state/logs` | Job log directory |
| `TMP_ROOT` | `/state/tmp` | General temporary directory |
| `PROCESS_TMP_ROOT` | `/state/processing/tmp` | Per-job processing directory |
| `LANDSCAPE_POSTER_ROOT` | `/state/landscape-posters` | State directory for landscape poster automation |
| `CHOWN_LIBRARY` | `0` | Set to `1` only if the container should recursively take ownership of `/library` at startup |
| `GIF_OPTIMIZE` | `1` | Run lossless Gifsicle optimization before moving the final `poster.gif` into place |
| `GIF_OPTIMIZE_LEVEL` | `2` | Gifsicle optimization level, clamped to `1`, `2`, or `3` |
| `GIFSICLE_BIN` | `gifsicle` | Gifsicle executable path or command name |
| `GIF_OPTIMIZE_TIMEOUT` | `600` | Maximum seconds allowed for one GIF optimization step |
| `GIF_GENERATION_STALL_TIMEOUT` | `180` | Stop a GIF/Test Lab FFmpeg process that produces no progress for this many seconds |
| `LANDSCAPE_POSTER_AUTO` | `0` | Enable automatic landscape poster maintenance at startup |
| `LANDSCAPE_POSTER_INTERVAL_SECONDS` | `900` | Incremental landscape poster scan interval when automation is enabled |
| `LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS` | `86400` | Maximum interval between full landscape poster reconciliation scans |
| `LANDSCAPE_POSTER_AUTO_APPLY` | `0` | Apply eligible landscape poster updates automatically after a manual Scan on the Library Maintenance page. Off by default; can also be toggled and persists from the UI |
| `VIDEO_PREVIEW_GENERATION_STALL_TIMEOUT` | `120` | Stop and skip one BIF extraction when FFmpeg writes no new frame for this many seconds |
| `EMBY_REFRESH_ENABLED` | `0` | Request an Emby library refresh after landscape poster changes |
| `EMBY_URL` | empty | Emby server base URL, for example `http://emby:8096` |
| `EMBY_API_KEY` | empty | Emby API key used for optional library refresh |

These can be overridden when invoking `python -m app.main` or the Docker
container, for example `docker run -e LIB_ROOT=/media/videos ...`.

Media outputs are staged outside the library, copied to a hidden temporary
file beside the destination, flushed, and installed atomically. Jobs capture
the source video and existing destination identities when queued and refuse to
install if either changes. Quarantine actions use a same-filesystem,
no-overwrite link-and-unlink operation; they refuse cross-filesystem fallback
instead of risking a partial copy followed by deletion. Permanent-delete
maintenance actions remain explicit and irreversible, so quarantine is the
recommended operation for live libraries.

The standard GIF queue is persisted under `/state/gif-jobs`. Queued work is
restored after a container restart; work that was actively rendering is marked
interrupted and its staging directory is removed without installing partial
output. FFmpeg and Gifsicle output is drained continuously with bounded logs,
and active GIF/Test Lab work can be cancelled from either its page or the global
activity strip. Disk-heavy scans, maintenance writes, BIF generation, and GIF
conversion share a FIFO coordinator so they do not compete for the same library
disks. Waiting work remains visible and cancellable where the workflow supports
cancellation.

The Docker entrypoint always ensures `/state` is writable for logs and
temporary files. It does not scan and chown `/library` by default, which avoids
slow startup on large mounted media libraries.

GIF optimization is lossless and keeps the original ffmpeg output if Gifsicle is
missing, fails, times out, or produces a larger file.

Landscape poster automation is disabled by default. The Library Maintenance
page provides a review-first manual workflow with a shared scan-source picker,
cancelable progress, category/search filters, scan-wide selection, plan preview,
and explicit apply. It uses FFprobe to require a valid landscape
`*-background.*` image and skips posters that are already landscape. An eligible
portrait poster is renamed without overwrite to `*-poster-backup.*`, verified,
and then replaced atomically. If an existing backup differs from the current
portrait, the item is marked unsafe and left unchanged. Ambiguous, unreadable,
or mismatched artwork is also left unchanged. Optional automatic runs store
state under `/state` and do not use `.posters_done` marker files.

Manual poster Scans reuse each folder's previous verdict when its artwork is
unchanged since the last analysis, so a repeated scan of a large library only
re-checks new or modified artwork. Use Full Rescan to ignore the cache and
re-check every folder. When at least one update is ready and auto-apply is
off, an Emby administrator notification is sent (subject to the notification
policy on the Settings page) so the review can happen without leaving a scan
running in a browser tab. The "Apply ready updates automatically after Scan"
switch applies only the strictly-safe matches immediately after a manual scan
finishes; anything ambiguous, unsafe, or unreadable still waits on the results
table for manual review. It is off by default and its state persists in
`/state/landscape-posters/settings.json` across container restarts.

Duplicate cleanup keeps the best copy of every file, not just the best video.
Each sidecar role and suffix forms a slot -- `.eng.srt`, `-poster.jpg`, `.bif` --
and the strongest candidate wins that slot even when it sits beside a copy being
removed, in which case it is renamed onto the surviving filename. Subtitles and
BIF previews are judged against the keeper's runtime, so a file from a longer cut
ranks below one that matches and is flagged rather than adopted silently.
Borrowing requires a real measurement: files that cannot be read or measured
leave the keeper's own copy in place and raise the slot for review. Expanding a
group shows one row per slot with the winner, the copy it came from, and the
measurement that decided it; the full per-file action list stays available behind
a disclosure.

Three settings control when cleanup stops deciding and asks instead: how close
two subtitles' coverage must be to count as tied, how close two images must be in
pixel count, and how far a runtime may differ before a subtitle or preview from a
different-length copy is flagged rather than used. Smaller values mean fewer
questions and more trust in the ranking; larger values send more folders back for
review.

Cleanup runs can be undone a file at a time. Each log lists every file it moved
with its current state, and restoring some files leaves the rest available to
restore later. Records describing moved files are exempt from the log size cap so
a large cleanup stays reversible.

Duplicate cleanup settings live on the Settings page. Duplicate move
destinations default to `/library/.vid2gif-duplicates`, can be changed to another
folder under the mounted library root, and every applied cleanup writes a bounded
JSONL audit log under `/state/maintenance-logs/duplicates`. Review selections
persist across result pages. Quarantined cleanup runs can be previewed and
restored from their audit logs; restore name conflicts are adjusted without
overwriting existing files.

BIF generation escalates rather than giving up on the first error. A video is
first extracted normally, then retried with a decoder that tolerates damaged
packets, then retried again at reduced width -- a partial preview is better than
none. Escalation is driven by the result, not only by errors: a video whose
presentation timestamps are unusable extracts exactly one frame while ffmpeg
exits cleanly, so a short result counts as a failure and moves on to the tactic
that rebuilds timestamps. Only a video that fails all three is recorded as a
failure, and the record says which tactics were tried and how many frames each
produced. Failures caused by the machine rather than the
file (a stall, a busy disk, a video that changed mid-run) are cleared at the
start of the next scan and tried again automatically; the rest stay listed with
a "Try again" control so they are never a dead end.

Every quarantine destination is configurable from the Settings page: duplicate
cleanup, damaged videos, quarantined BIF previews and quarantined subtitles.
Scans exclude those destinations by path rather than by folder name, so a
renamed or relocated quarantine folder is still never walked back into the scan
that emptied it.

A video too damaged to preview can be quarantined from the missing-BIF list.
It moves with its sidecars to the damaged destination, which defaults to a
subfolder of `/library/.vid2gif-quarantine` and is kept separate from duplicate
cleanup so the two piles stay distinct. Setting a library path prefix (what
`/library` looks like from your own computer) adds a "Copy folder path" control
for inspecting a file before deciding. Browsers refuse to open a folder directly
from a web page regardless of HTTP or HTTPS, so copying the path is the closest
available behaviour.

Video preview maintenance separates cleanup from generation. Bad and warning
BIFs can be quarantined or deleted first; a fresh scan then provides the missing
videos eligible for direct BIF generation. Width and interval settings persist,
and the page compares them with the newest valid externally observed BIF before
generation. Frames and the BIF archive are built under `/state`, validated, and
atomically installed only while the video still has no matching BIF. Generation
status is persisted under `/state`, including the current video and completed
per-file results. Decoder errors and stalled extraction are bounded to one video
so a malformed source cannot silently block the rest of a batch.

Subtitle maintenance provides independent missing/language and timestamp-coverage
scans. It scans main videos only, excluding conventional trailer, extra,
featurette, scene, short, interview, and sample folders or filenames. Likely
incomplete SRT files can be quarantined or permanently deleted; uncertain
coverage stays review-only. Video files and missing-subtitle findings are never
cleanup targets, and review selections persist across result pages.

Timestamp-coverage scans need each video's duration; Emby is checked first, and
when Emby doesn't have it, a repeated Scan Coverage reuses the duration probed
by the previous scan of that folder as long as the video file itself hasn't
changed, instead of re-probing it with FFprobe every time. Use Full Coverage
Rescan to ignore that cache and re-probe every video.

Actor image maintenance fills in Emby people who have no picture, using images
that are already sitting in the library. It is the one workstream that never
writes to `/library`: it reads, and everything it changes it changes in Emby.
It therefore needs an Emby URL and API key on the Settings page, and does
nothing without them.

A scan asks Emby which people appear in the scanned folders and which of them
have no primary image, then looks through those folders for an image file whose
name matches the actor's. Matching is on a normalized name, so case, spacing,
separators such as `_ - .`, punctuation, accents, and a trailing video stem do
not defeat it -- `Amelie` and `Amélie` match each other, so the file does not
have to spell the actor the way Emby does. Every candidate must be a real file
under the
library root -- symlinks and paths outside it are skipped. An actor with exactly
one match is listed as ready; several plausible matches are listed as ambiguous
and left for a person to settle rather than guessed at. Actors with no match are
listed too, so the gap is visible.

Review then applies: selected images are uploaded to Emby as that person's
primary image, one at a time, with per-file progress and the same cancellation
and library-access coordination as every other scan. An actor who already has an
image in Emby is refused rather than overwritten. Nothing is renamed, moved, or
deleted in the library, and the source images stay exactly where they were.

Individual actors can be marked ignored, handled manually, or blocked. Those
decisions persist in `/state/actor-images/exceptions.json` and survive rescans,
so a name that will never match automatically stops asking. Each applied run
writes a bounded JSONL log under `/state/maintenance-logs/actor-images`.

The dashboard tracks maintenance impact from the first launch after this
feature is installed, and on that first launch it replays the maintenance audit
logs to recover the file operations they record, so the lifetime total is not
limited to work done since. The logs do not hold everything: the per-issue
history, subtitle byte totals, actor image imports (which write to Emby rather
than the library) and GIF creation predate any audit record, and runs older than
the log retention limits are gone. The dashboard lists exactly what it could not
recover rather than folding an estimate into the total. Distinct
actionable issues, completed fixes, quarantine/delete totals, milestones, daily
activity, and newly created GIF output persist in
`/state/dashboard/impact-metrics.json`; retaining the `/state` volume retains
the lifetime record across container updates.

## Example Workflow

1. Start the server locally or via Docker.
2. Visit [http://localhost:904](http://localhost:904) and select a video or folder under the mounted library.
3. Submit the job and monitor progress on the **Live Logs** page.
4. Completed jobs are listed on the **Completed** tab.

## Smooth Motion

Enable the **Smooth motion** option in the New Job form to generate intermediate
frames with ffmpeg's `minterpolate` filter when the requested GIF FPS differs
from the source video. This makes motion look fluid but can significantly
increase processing time.

## Contributing

- Follow [`DESIGN.md`](DESIGN.md) for user-facing interface and interaction work.
- Follow [PEP 8](https://peps.python.org/pep-0008/) style guidelines; `python -m ruff check .`
  enforces them and runs in CI. Run `python -m ruff format .` before committing —
  CI fails on unformatted code.
- Add tests under [`tests/`](tests/) and ensure they pass with `python -m pytest`.
- Run `npm run test:frontend` and rebuild the checked-in frontend bundle after changing Test Lab source files.
- Run `npm run test:browser` for Chromium interaction, responsive-layout, and accessibility checks.
- Keep runtime dependencies in `requirements.txt` and development-only tools in `requirements-dev.txt`.
