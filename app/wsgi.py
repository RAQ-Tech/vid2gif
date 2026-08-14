from .jobs import start_worker
from .test_lab import start_test_lab_worker
from .poster_maintenance import start_landscape_poster_worker
from .routes import app


# `app` looks unused here, but it is the WSGI callable gunicorn loads:
# the Dockerfile runs `app.wsgi:app`. Re-exporting it explicitly keeps a
# linter or an over-eager cleanup from deleting the production entry point.
__all__ = ["app"]


start_worker()
start_test_lab_worker()
start_landscape_poster_worker()
