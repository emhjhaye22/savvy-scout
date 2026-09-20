"""Production entry point. Run with `python wsgi.py` (see render.yaml)."""

import os

from waitress import serve

from savvy_scout.config import load_settings
from savvy_scout.dashboard import create_app
from savvy_scout.scheduler import start_scheduler

if __name__ == "__main__":
    start_scheduler()
    settings = load_settings()
    app = create_app(settings)
    serve(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        threads=int(os.environ.get("WAITRESS_THREADS", "16")),
        channel_timeout=int(os.environ.get("WAITRESS_CHANNEL_TIMEOUT", "60")),
    )
