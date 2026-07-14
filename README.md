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

## Testing

Install development dependencies, audit runtime dependencies, build the checked-in
Test Lab bundle, and run tests:

```bash
pip install -r requirements-dev.txt
npm ci --ignore-scripts
python -m pip_audit -r requirements.txt
npm audit --audit-level=low
npm run test:frontend
npm run build:frontend
python -m pytest
```

Node.js is only required for frontend development. Docker and deployed instances
serve the generated `app/static/test-lab.bundle.js` file directly.

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

Landscape poster automation is disabled by default. When enabled from the
Library Maintenance page or environment variables, it uses FFprobe to require a
valid landscape `*-background.*` image and skips posters that are already
landscape. Eligible portrait posters are backed up once as
`*-poster-backup.*`, verified, and replaced atomically. Ambiguous, unreadable,
or mismatched artwork is left unchanged. It stores run state under `/state` and
does not use `.posters_done` marker files. Optional Emby refresh settings can be
tested from the same page before automatic refresh is enabled.

Duplicate cleanup settings live on the Settings page. Duplicate move
destinations default to `/library/.vid2gif-duplicates`, can be changed to another
folder under the mounted library root, and every applied cleanup writes a bounded
JSONL audit log under `/state/maintenance-logs/duplicates`. Cleanup plans are
limited to the duplicate groups visible on the current results page.

Video preview maintenance separates cleanup from generation. Bad and warning
BIFs can be quarantined or deleted first; a fresh scan then provides the missing
videos eligible for direct BIF generation. Width and interval settings persist,
and the page compares them with the newest valid externally observed BIF before
generation. Frames and the BIF archive are built under `/state`, validated, and
atomically installed only while the video still has no matching BIF. Generation
status is persisted under `/state`, including the current video and completed
per-file results. Decoder errors and stalled extraction are bounded to one video
so a malformed source cannot silently block the rest of a batch.

Subtitle health results can quarantine or permanently delete flagged unexpected
or unknown-language SRT sidecars. Video files and missing-subtitle findings are
never cleanup targets, and plans are restricted to explicitly selected subtitle
files on the visible page.

The dashboard tracks maintenance impact from the first launch after this
feature is installed. It does not backfill bounded historical logs. Distinct
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
- Follow [PEP 8](https://peps.python.org/pep-0008/) style guidelines.
- Add tests under [`tests/`](tests/) and ensure they pass with `python -m pytest`.
- Run `npm run test:frontend` and rebuild the checked-in frontend bundle after changing Test Lab source files.
- Run `npm run test:browser` for Chromium interaction, responsive-layout, and accessibility checks.
- Keep runtime dependencies in `requirements.txt` and development-only tools in `requirements-dev.txt`.
