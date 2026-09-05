"""Shortlists: one flat save-list for opportunities (2026-09-05 clarification
-- no named or multiple lists, since there's a single scouting desk with no
need to separate saved items by list or share them between owners). A star
toggle lives on the notice detail page; this page lists everything saved."""

from datetime import datetime, timezone

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db

shortlists_bp = Blueprint("shortlists", __name__)


def is_shortlisted(conn, notice_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM shortlisted_notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    return row is not None


@shortlists_bp.route("/shortlists")
@login_required
def index():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT sl.id AS shortlist_id, sl.added_by, sl.added_at,
               n.id AS notice_id, n.ref, n.title, n.buyer, n.sector, n.status,
               n.indicative_value, n.deadline
        FROM shortlisted_notices sl
        JOIN notices n ON n.id = sl.notice_id
        ORDER BY sl.added_at DESC
        """
    ).fetchall()
    return render_template("shortlists.html", items=rows)


@shortlists_bp.route("/shortlists/toggle", methods=["POST"])
@login_required
def toggle():
    notice_id = request.form.get("notice_id", type=int)
    conn = get_db()
    if notice_id is not None:
        if is_shortlisted(conn, notice_id):
            conn.execute("DELETE FROM shortlisted_notices WHERE notice_id = ?", (notice_id,))
        else:
            conn.execute(
                "INSERT INTO shortlisted_notices (notice_id, added_by, added_at) VALUES (?, ?, ?)",
                (notice_id, current_user.display_name, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    next_url = request.form.get("next") or url_for("shortlists.index")
    return redirect(next_url)
