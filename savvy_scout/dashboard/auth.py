"""Individual logins, no shared accounts (SPEC.md B1)."""

import sqlite3

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from savvy_scout.db.connection import get_connection

login_manager = LoginManager()
login_manager.login_view = "auth.login"
# Suppress Flask-Login's default "Please log in to access this page" flash.
# login.html doesn't render flashed messages (it has its own `error` slot for
# failed submissions), so that flash was going unread until the next
# authenticated page render, leaking a stale message onto the queues view
# right after a successful login.
login_manager.login_message = None

# Per-IP login throttling (2026-09-17, audit finding): unlimited password
# guesses were allowed against any username. In-memory storage is fine here
# -- this is a single Render instance, no shared cache between workers to
# worry about. init_app(app) is called from dashboard/__init__.py.
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")

auth_bp = Blueprint("auth", __name__)


class User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.display_name = row["display_name"]
        self.email = row["email"]
        self.role = row["role"]
        # Which client this user is acting on behalf of (2026-09-18 tenancy
        # fix) -- scopes shortlists/watchlists so they aren't one global
        # list shared by everyone. row["client_id"] may be None only for a
        # row read before the migration backfill has run.
        self.client_id = row["client_id"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_account_approver(self) -> bool:
        return self.role == "account_approver"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = get_connection(current_app.config["SAVVY_SCOUT_DB_PATH"])
    return g.db


@login_manager.user_loader
def load_user(user_id: str):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(row) if row else None


def _authenticate(identifier: str, password: str):
    # The four original accounts (mark/kanvesh/hammad/victoria) log in by
    # username, same as before; accounts added later via the admin
    # screen log in by email (2026-08-08) -- one input matches either
    # column so both keep working without forcing a migration on the
    # original accounts.
    row = get_db().execute(
        "SELECT * FROM users WHERE username = ? OR email = ?", (identifier, identifier)
    ).fetchone()
    if row and check_password_hash(row["password_hash"], password):
        return User(row)
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per 5 minutes", methods=["POST"])
def login():
    error = None
    if request.method == "POST":
        user = _authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
        if user:
            login_user(user)
            return redirect(url_for("welcome.index"))
        error = "Invalid email/username or password"

    return render_template("login.html", error=error, client=None)


@auth_bp.route("/login/<slug>", methods=["GET", "POST"])
@limiter.limit("10 per 5 minutes", methods=["POST"])
def client_login(slug):
    """A client's own distinct, bookmarkable login link (2026-09-20,
    explicit request -- "Trifork is a distinct client so they should have
    a different link"): same branding as the generic /login, just with
    that client's name shown instead of the generic copy, and without the
    Trifork-internal Mark/Victoria quick-access shortcuts.

    This is a branding/convenience layer, not a security boundary: signing
    in here runs the exact same _authenticate() check as /login, and a
    successful login redirects the exact same way regardless of which
    client's URL it happened on. Actual access is still enforced entirely
    by the account's own role/client_id via the tenant-isolation gate
    (dashboard/__init__.py) -- logging in with the wrong client's
    credentials on the right client's link (or vice versa) still lands
    you wherever your own account actually belongs, same as today."""
    client = get_db().execute("SELECT * FROM clients WHERE slug = ?", (slug,)).fetchone()
    if client is None:
        return render_template("login.html", error="That portal link isn't recognised.", client=None)

    error = None
    if request.method == "POST":
        user = _authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
        if user:
            login_user(user)
            return redirect(url_for("welcome.index"))
        error = "Invalid email/username or password"

    return render_template("login.html", error=error, client=client)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Self-service password change (2026-08-09) -- previously the only way
    to get a new password was an admin-triggered reset (random temp
    password, re-sent by email), with no way for someone to just set their
    own once logged in."""
    error = None
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        row = get_db().execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
        if not check_password_hash(row["password_hash"], current_password):
            error = "Current password is incorrect."
        elif len(new_password) < 8:
            error = "New password must be at least 8 characters."
        elif new_password != confirm_password:
            error = "New password and confirmation don't match."
        else:
            get_db().execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), current_user.id),
            )
            get_db().commit()
            flash("Password updated.")
            return redirect(url_for("queues.index"))

    return render_template("change_password.html", error=error)
