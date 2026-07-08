# vid2gif

Dockerized Web UI for generating GIF previews from large video libraries.

## Security Notice

vid2gif is intended for trusted private networks only. Do not expose it directly
to the public internet.

The app can browse mounted library directories, shows video paths and file names
to users of the Web UI, can write `poster.gif` next to selected videos, and can
replace matching Emby poster images during library maintenance. Run it behind a
firewall or private reverse proxy, and only mount media directories that the
container should be allowed to inspect and write to.

If you need internet-facing access, add authentication, CSRF protection, rate
limiting, stricter file-serving rules, and reverse-proxy hardening before
deployment.

## Testing

Install development dependencies, audit runtime dependencies, and run tests:

```bash
pip install -r requirements-dev.txt
python -m pip_audit -r requirements.txt
python -m pytest
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
     -v /path/to/videos:/library \
     -v /path/to/state:/state \
     vid2gif
   ```

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUID` | `99` | User ID the app runs as in Docker |
| `PGID` | `100` | Group ID the app runs as in Docker |
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
| `LANDSCAPE_POSTER_AUTO` | `0` | Enable automatic landscape poster maintenance at startup |
| `LANDSCAPE_POSTER_INTERVAL_SECONDS` | `900` | Incremental landscape poster scan interval when automation is enabled |
| `LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS` | `86400` | Maximum interval between full landscape poster reconciliation scans |
| `EMBY_REFRESH_ENABLED` | `0` | Request an Emby library refresh after landscape poster changes |
| `EMBY_URL` | empty | Emby server base URL, for example `http://emby:8096` |
| `EMBY_API_KEY` | empty | Emby API key used for optional library refresh |

These can be overridden when invoking `python -m app.main` or the Docker
container, for example `docker run -e LIB_ROOT=/media/videos ...`.

The Docker entrypoint always ensures `/state` is writable for logs and
temporary files. It does not scan and chown `/library` by default, which avoids
slow startup on large mounted media libraries.

GIF optimization is lossless and keeps the original ffmpeg output if Gifsicle is
missing, fails, times out, or produces a larger file.

Landscape poster automation is disabled by default. When enabled from the
Library Maintenance page or environment variables, it copies existing
`*-background.*` images over matching existing `*-poster.*` files and preserves
the original poster once as `*-poster-backup.*`. It stores run state under
`/state` and does not create `.posters_done` marker files.

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

- Follow [PEP 8](https://peps.python.org/pep-0008/) style guidelines.
- Add tests under [`tests/`](tests/) and ensure they pass with `python -m pytest`.
- Keep runtime dependencies in `requirements.txt` and development-only tools in `requirements-dev.txt`.
