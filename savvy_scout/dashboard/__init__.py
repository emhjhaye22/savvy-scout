"""Flask approval dashboard (SPEC.md B1). Local-only tool for four named
accounts (Mark, Kanvesh, Hammad, Victoria), individual logins, no shared
accounts. Not hardened for internet exposure -- run it on localhost or an
internal network only."""

import json

from flask import Flask, g
from flask_login import current_user

from savvy_scout.config import Settings
from savvy_scout.dashboard.auth import auth_bp, get_db, login_manager
from savvy_scout.dashboard.notifications import get_notification_context, get_sidebar_stage_counts
from savvy_scout.dashboard.routes.admin import admin_bp
from savvy_scout.dashboard.routes.competitor_intel import competitor_intel_bp
from savvy_scout.dashboard.routes.draft_assist import draft_assist_bp
from savvy_scout.dashboard.routes.home import home_bp
from savvy_scout.dashboard.routes.queues import queues_bp
from savvy_scout.dashboard.routes.settings import settings_bp
from savvy_scout.dashboard.routes.shortlists import shortlists_bp
from savvy_scout.dashboard.routes.signals import signals_bp
from savvy_scout.dashboard.routes.welcome import welcome_bp
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def create_app(settings: Settings) -> Flask:
    import os
    # Explicitly set template_folder to ensure Flask finds our templates
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    app = Flask(__name__, template_folder=template_dir)
    app.config["SAVVY_SCOUT_DB_PATH"] = settings.db_path
    app.config["SAVVY_SCOUT_SETTINGS"] = settings
    app.config["SAVVY_SCOUT_APP_BASE_URL"] = os.environ.get("SAVVY_SCOUT_APP_BASE_URL") or None
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.secret_key = settings.flask_secret_key or "dev-only-insecure-key-set-FLASK_SECRET_KEY-in-.env"
    # Render sets RENDER=true in every runtime environment there -- only
    # force Secure cookies there, never on plain-http localhost (2026-08-08,
    # prepping for a public deploy), or a local dev login would silently
    # never set its session cookie at all.
    if os.environ.get("RENDER"):
        app.config["SESSION_COOKIE_SECURE"] = True

    @app.template_filter("from_json")
    def from_json_filter(value):
        """Parses a JSON text column (e.g. phase2_assessments.open_questions)
        for display in a template. Missing entirely before 2026-07-30, which
        crashed notice_detail.html with a 500 for any notice whose Phase 2 AI
        read included open questions."""
        if not value:
            return []
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(welcome_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(queues_bp)
    app.register_blueprint(signals_bp)
    app.register_blueprint(competitor_intel_bp)
    app.register_blueprint(shortlists_bp)
    app.register_blueprint(draft_assist_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # Ensure schema and lightweight migrations are applied on dashboard boot.
    # seed_all is called here too (2026-08-09) -- previously only the CLI's
    # init-db/sweep commands called it, so a database that only ever booted
    # through the web dashboard (as production's did) had every config_*
    # table -- crucially config_sources -- sitting empty forever. That's why
    # "Sweep now" pulled 0 notices: run_sweep reads enabled config_sources
    # rows to know what to hit, and found none. seed_all only inserts into a
    # table that's still empty, so this is a no-op everywhere it's already
    # been seeded via the CLI.
    conn = get_connection(settings.db_path)
    init_db(conn)
    seed_all(conn)
    conn.close()

    # Create test users if they don't exist (development only)
    def create_test_users():
        import sqlite3
        from werkzeug.security import generate_password_hash
        from datetime import datetime, timezone
        
        conn = get_connection(settings.db_path)
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        
        if count == 0:
            # Create test users. Mark is the account-management admin
            # (is_admin), separate from Victoria's rule-correction authority
            # (is_victoria) -- see dashboard/routes/admin.py. Kanvesh and
            # Hammad are no longer seeded here: scouting consolidated to
            # Mark alone on 2026-09-01.
            password_hash = generate_password_hash('12345')
            users = [
                ('mark', 'Mark', False, True),
                ('victoria', 'Victoria', True, False),
            ]

            for username, display_name, is_victoria, is_admin in users:
                conn.execute(
                    "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (username, password_hash, display_name, int(is_victoria), int(is_admin), datetime.now(timezone.utc).isoformat())
                )

            conn.commit()
            print("✓ Created test users: mark, victoria (password: '12345')")
    
    try:
        with app.app_context():
            create_test_users()
    except Exception as e:
        print(f"Warning: Could not create test users: {e}")

    @app.teardown_appcontext
    def close_db(_exception=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_notifications():
        if not current_user.is_authenticated:
            return {}
        conn = get_db()
        notif = get_notification_context(conn, current_user.display_name, int(current_user.is_victoria))
        sidebar_stage_counts = get_sidebar_stage_counts(
            conn, current_user.display_name, int(current_user.is_victoria)
        )
        return {"notif": notif, "sidebar_stage_counts": sidebar_stage_counts}

    return app
