import eventlet
# 必须在绝对的第一行执行 Patch
eventlet.monkey_patch()

from app import app

if __name__ == "__main__":
    from app import socketio
    socketio.run(app)