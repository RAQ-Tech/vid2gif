from .routes import app
from .jobs import start_worker
from .test_lab import start_test_lab_worker
from .poster_maintenance import start_landscape_poster_worker
from .impact_backfill import ensure_backfilled


if __name__ == "__main__":
    ensure_backfilled()
    start_worker()
    start_test_lab_worker()
    start_landscape_poster_worker()
    app.run(host="0.0.0.0", port=904, debug=False, threaded=True)
