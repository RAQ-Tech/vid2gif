# vid2gif

Dockerized Web UI for generating GIF previews from large video libraries.

## Security Notice

vid2gif is intended for trusted private networks only. Do not expose it directly
to the public internet.

The app can browse mounted library directories, shows video paths and file names
to users of the Web UI, and can write `poster.gif` next to selected videos. Run
it behind a firewall or private reverse proxy, and only mount media directories
that the container should be allowed to inspect and write to.

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
| `SOCKETIO_CORS_ALLOWED_ORIGINS` | same-origin only | Optional comma-separated Socket.IO CORS allowlist; use `*` only on trusted networks |

These can be overridden when invoking `python -m app.main` or the Docker
container, for example `docker run -e LIB_ROOT=/media/videos ...`.

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
