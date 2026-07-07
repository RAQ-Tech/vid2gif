from .jobs import start_worker
from .test_lab import start_test_lab_worker
from .routes import app


start_worker()
start_test_lab_worker()
