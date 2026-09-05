"""Draft assist: AI-drafted first answers to pasted-in selection-
questionnaire / bid-response questions, plus a price-guidance panel built
from real historical award values in the same sector -- no AI call needed
for pricing, since notices.indicative_value on past UK5 award notices is
real data already captured by the sweep (same field Competitor Intel reads).

Entry point is a shortlisted opportunity (?notice_id=<id>): drafting only
makes sense in the context of a specific bid, and Shortlists is already
"opportunities I'm actively pursuing," so it's the natural picker rather
than building a second one."""

import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.routes.competitor_intel import _parse_gbp
from savvy_scout.triage.draft_assist import get_draft_assist_client

draft_assist_bp = Blueprint("draft_assist", __name__)


def _price_guidance(conn: sqlite3.Connection, sector: str | None) -> dict:
    if not sector:
        return {"count": 0, "priced_count": 0}
    rows = conn.execute(
        "SELECT indicative_value FROM notices WHERE is_award = 1 AND sector = ?",
        (sector,),
    ).fetchall()
    values = sorted(v for v in (_parse_gbp(r["indicative_value"]) for r in rows) if v is not None)
    if not values:
        return {"count": len(rows), "priced_count": 0}
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    return {
        "count": len(rows),
        "priced_count": n,
        "low": values[0],
        "high": values[-1],
        "median": median,
    }


@draft_assist_bp.route("/draft-assist")
@login_required
def index():
    conn = get_db()
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]

    shortlisted = conn.execute(
        """
        SELECT n.id AS notice_id, n.ref, n.title, n.sector
        FROM shortlisted_notices sl
        JOIN notices n ON n.id = sl.notice_id
        ORDER BY sl.added_at DESC
        """
    ).fetchall()

    notice = None
    price_guidance = None
    draft_items = []
    notice_id = request.args.get("notice_id", type=int)
    if notice_id is not None:
        notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
        if notice is not None:
            price_guidance = _price_guidance(conn, notice["sector"])
            draft_items = conn.execute(
                "SELECT * FROM draft_assist_items WHERE notice_id = ? ORDER BY id DESC",
                (notice_id,),
            ).fetchall()

    return render_template(
        "draft_assist.html",
        shortlisted=shortlisted,
        notice=notice,
        price_guidance=price_guidance,
        draft_items=draft_items,
        scope_read_ready=settings.scope_read_ready,
    )


@draft_assist_bp.route("/draft-assist/draft", methods=["POST"])
@login_required
def draft():
    notice_id = request.form.get("notice_id", type=int)
    question_text = (request.form.get("question_text") or "").strip()
    if not notice_id or not question_text:
        flash("A question is required to draft an answer.", "error")
        return redirect(url_for("draft_assist.index", notice_id=notice_id))

    conn = get_db()
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()

    try:
        client, draft_fn, model_name = get_draft_assist_client(settings)
        answer = draft_fn(client, conn, notice_row, question_text)
    except Exception as exc:
        current_app.logger.warning("Draft assist AI call failed: %s", exc)
        flash(
            "Could not draft an answer -- check the AI provider configuration "
            "(same setup Phase 2 scope reads use) and try again.",
            "error",
        )
        return redirect(url_for("draft_assist.index", notice_id=notice_id))

    conn.execute(
        "INSERT INTO draft_assist_items "
        "(notice_id, question_text, draft_answer, status, model_used, created_by, created_at) "
        "VALUES (?, ?, ?, 'DRAFTED', ?, ?, ?)",
        (notice_id, question_text, answer, model_name, current_user.display_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    flash("Draft answer generated -- PROVISIONAL, review before use.")
    return redirect(url_for("draft_assist.index", notice_id=notice_id))


@draft_assist_bp.route("/draft-assist/review", methods=["POST"])
@login_required
def review():
    item_id = request.form.get("item_id", type=int)
    notice_id = request.form.get("notice_id", type=int)
    decision = request.form.get("decision")
    if item_id is not None and decision in ("REVIEWED_USED", "REVIEWED_REJECTED"):
        conn = get_db()
        conn.execute(
            "UPDATE draft_assist_items SET status = ?, reviewed_at = ? WHERE id = ?",
            (decision, datetime.now(timezone.utc).isoformat(), item_id),
        )
        conn.commit()
    return redirect(url_for("draft_assist.index", notice_id=notice_id))
