"""Welcome/landing page (2026-09-06), first stop after login. Deliberately
separate from home.index's Overview: this is a router ("where would you
like to go?"), not a stats page -- modeled after Contracts Advance's own
Welcome screen. Built with multi-client (see triage/client_filter.py) in
mind: the Clients section below lets an admin jump into Trifork's full
workspace or a newer client's read-only matches view today, and needs no
changes when a future client earns its own full workspace -- it would just
become another entry in the same list."""

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.routes.admin import _is_super_admin

welcome_bp = Blueprint("welcome", __name__)


@welcome_bp.route("/welcome")
@login_required
def index():
    conn = get_db()
    clients = []
    if _is_super_admin():
        clients = conn.execute(
            "SELECT * FROM clients WHERE is_active = 1 ORDER BY (name != 'Trifork'), name"
        ).fetchall()
    return render_template(
        "welcome.html",
        clients=clients,
        is_super_admin=_is_super_admin(),
    )
