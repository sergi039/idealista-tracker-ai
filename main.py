import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or "5050")
    except Exception:
        port = 5050
    app.run(host="0.0.0.0", port=port, debug=True)
