from .jobs import start_worker
from .test_lab import start_test_lab_worker
from .poster_maintenance import start_landscape_poster_worker
from .routes import app
from .impact_backfill import ensure_backfilled


# `app` looks unused here, but it is the WSGI callable gunicorn loads:
# the Dockerfile runs `app.wsgi:app`. Re-exporting it explicitly keeps a
# linter or an over-eager cleanup from deleting the production entry point.
__all__ = ["app"]


# Recover lifetime totals from the audit logs before the first request,
# so the dashboard never shows an understated figure. No-op after the
# first run on this install.
ensure_backfilled()

start_worker()
start_test_lab_worker()
start_landscape_poster_worker()
