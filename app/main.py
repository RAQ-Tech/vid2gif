from .routes import app
from .jobs import start_worker
from .test_lab import start_test_lab_worker


if __name__ == "__main__":
    start_worker()
    start_test_lab_worker()
    app.run(host="0.0.0.0", port=904, debug=False, threaded=True)
