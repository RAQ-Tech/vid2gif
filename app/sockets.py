from flask_socketio import SocketIO

# Shared SocketIO instance for the app
# Allow CORS from any origin for simplicity in this container setup
socketio = SocketIO(cors_allowed_origins="*")
