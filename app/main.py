from routes import app
from jobs import start_worker
from sockets import socketio


if __name__ == "__main__":
    start_worker()
    socketio.run(app, host="0.0.0.0", port=904, debug=False)