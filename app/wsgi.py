from .jobs import start_worker
from .test_lab import start_test_lab_worker
from .poster_maintenance import start_landscape_poster_worker
from .routes import app


start_worker()
start_test_lab_worker()
start_landscape_poster_worker()
