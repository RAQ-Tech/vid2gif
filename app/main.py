from routes import app
from jobs import start_worker


if __name__ == "__main__":
    start_worker()
    # threaded=True so SSE + worker don’t block each other
    app.run(host="0.0.0.0", port=904, debug=False, threaded=True)
