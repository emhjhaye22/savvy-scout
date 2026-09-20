"""Tenant-facing self-service Matches view (2026-09-19) -- the first real
onboarding path for a non-Trifork client with its own login(s). Mirrors
admin.py's client_matches()/set_client_notice_status() exactly (same
shared helpers, savvy_scout.triage.client_filter), but scoped implicitly
by current_user.client_id rather than a client_id path parameter: a
tenant's own URL must never be able to name another client's id -- see
dashboard/__init__.py's tenant-isolation gate, which routes any
non-Trifork login here (and nowhere else in the app)."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.triage.client_filter import (
    get_client_match_status_counts,
    get_client_matches,
    record_client_notice_status,
)

client_portal_bp = Blueprint("client_portal", __name__)


def _require_tenant_client(conn):
    """The caller's own client row, or None if they don't belong to a
    real, active, non-Trifork client -- e.g. Trifork staff reaching this
    route directly, or a client paused since they last logged in."""
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (current_user.client_id,)).fetchone()
    if client is None or client["name"] == "Trifork" or not client["is_active"]:
        return None
    return client


@client_portal_bp.route("/my-matches")
@login_required
def matches():
    conn = get_db()
    client = _require_tenant_client(conn)
    if client is None:
        flash("Your account isn't set up with an active client filter yet.", "error")
        return redirect(url_for("welcome.index"))

    status_filter = request.args.get("status", "")
    client_matches = get_client_matches(conn, client["id"], status_filter or None)
    status_counts = get_client_match_status_counts(conn, client["id"])

    return render_template(
        "client_portal_matches.html", client=client, matches=client_matches,
        status_filter=status_filter, status_counts=status_counts,
    )


@client_portal_bp.route("/my-matches/<int:notice_id>/set-status", methods=["POST"])
@login_required
def set_status(notice_id):
    conn = get_db()
    client = _require_tenant_client(conn)
    if client is None:
        flash("Your account isn't set up with an active client filter yet.", "error")
        return redirect(url_for("welcome.index"))

    # Real permission split (2026-09-20): previously any logged-in client
    # user could record a decision -- now only an Account Approver (or the
    # platform Admin) can, an Account User can view but not decide.
    if not (current_user.is_admin or current_user.is_account_approver):
        flash("Only an Account Approver can record a decision on a match.", "error")
        return redirect(url_for("client_portal.matches"))

    status = request.form.get("status", "")
    if status not in ("NEW", "SHORTLISTED", "REJECTED"):
        flash("Invalid status.", "error")
        return redirect(url_for("client_portal.matches"))

    note = request.form.get("note", "").strip() or None
    record_client_notice_status(conn, client["id"], notice_id, status, note, current_user.display_name)

    keep_filter = request.form.get("status_filter", "")
    return redirect(url_for("client_portal.matches", status=keep_filter or None))
