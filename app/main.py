from flask import Flask

from jobs import AppState, start_worker
from routes import register_routes

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # helps while iterating UI

state = AppState()
register_routes(app, state)
start_worker(state)

if __name__ == "__main__":
    # threaded=True so SSE + worker don't block each other
    app.run(host="0.0.0.0", port=904, debug=False, threaded=True)
