"""Individual logins, no shared accounts (SPEC.md B1)."""

import sqlite3

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
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

auth_bp = Blueprint("auth", __name__)


class User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.display_name = row["display_name"]
        self.email = row["email"]
        self.is_victoria = bool(row["is_victoria"])
        self.is_admin = bool(row["is_admin"])


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = get_connection(current_app.config["SAVVY_SCOUT_DB_PATH"])
    return g.db


@login_manager.user_loader
def load_user(user_id: str):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(row) if row else None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        # The four original accounts (mark/kanvesh/hammad/victoria) log in by
        # username, same as before; accounts added later via the admin
        # screen log in by email (2026-08-08) -- one input matches either
        # column so both keep working without forcing a migration on the
        # original accounts.
        identifier = request.form.get("username", "").strip()
        row = get_db().execute(
            "SELECT * FROM users WHERE username = ? OR email = ?", (identifier, identifier)
        ).fetchone()
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            login_user(User(row))
            return redirect(url_for("welcome.index"))
        error = "Invalid email/username or password"

    return render_template("login.html", error=error)


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
