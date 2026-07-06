import os

from flask_socketio import SocketIO


def _cors_allowed_origins():
    raw = os.getenv("SOCKETIO_CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return None
    if raw == "*":
        return "*"
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# Shared SocketIO instance for the app
socketio = SocketIO(cors_allowed_origins=_cors_allowed_origins())
