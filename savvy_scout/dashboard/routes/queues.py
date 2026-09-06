"""Per-status queues, approve/reject/park actions (SPEC.md B1), plus B3's
manual "Victoria decision" mark and Victoria's own decision route."""

import json
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.notifications import STAGE_GROUPS, victoria_sourced_reject_sql
from savvy_scout.dashboard.scope_filter import in_scope_filter_sql
from savvy_scout.escalation.brief import mark_emailed
from savvy_scout.dashboard.routes.shortlists import is_shortlisted
from savvy_scout.graph.mail import send_escalation_email as graph_send_escalation_email
from savvy_scout.models.notice import Notice
from savvy_scout.sources.ocds_parser import ParsedNotice
from savvy_scout.sweep.dedupe import upsert_notice
from savvy_scout.triage.gates import triage_notice
from savvy_scout.triage.scope_read import get_scope_read_client
from savvy_scout.workflow import approvals

STAGE_STATUSES_BY_SLUG = {slug: statuses for slug, _css_class, _label, statuses in STAGE_GROUPS}

# Friendly labels for BID_DOCUMENT_TYPES (savvy_scout/sources/ocds_parser.py),
# shown on the notice detail page's Bid Documents section.
BID_DOCUMENT_TYPE_LABELS = {
    "biddingDocuments": "Invitation to Tender / Bidding Documents",
    "technicalSpecifications": "Technical Specification",
    "technicalSelectionCriteria": "Technical Selection Criteria (PQQ)",
    "economicSelectionCriteria": "Economic/Financial Selection Criteria (PQQ)",
    "eligibilityCriteria": "Eligibility Criteria",
    "evaluationCriteria": "Evaluation Criteria",
    "clarifications": "Clarifications",
    "submissionDocuments": "Submission Documents",
    "procurementPlan": "Procurement Plan",
    "marketStudies": "Market Study",
    "contractSummary": "Contract Summary",
}

queues_bp = Blueprint("queues", __name__)


def _build_pipeline_stages(notice, triage_run, phase2_assessment, escalation_brief, status_history):
    """A fixed, always-the-same-shape progress tracker for notice_detail.html
    (2026-07-30). Every notice used to show a different, unpredictable
    combination of cards depending on which optional data existed for it,
    which read as "random" to a reviewer clicking between notices. This
    reconstructs "which stages did this specific notice actually go through"
    from status_history + the current status, so every notice shows the same
    list of steps -- just with different done/current/skipped/pending marks."""
    ever_reached = {h["to_status"] for h in status_history}
    ever_reached.add(notice["status"])
    current = notice["status"]
    TERMINAL = {"APPROVED", "CAPTURE_BRIEF_DRAFTED", "DOCS_DOWNLOADED", "CALENDARED",
                "ACTIVE", "REJECTED", "PARKED", "MONITORING"}

    stages = []

    stages.append({
        "label": "Swept & Extracted",
        "state": "done",
        "detail": f"First seen {notice['first_seen_at'][:10]}" if notice["first_seen_at"] else "First seen",
    })

    if triage_run:
        stages.append({
            "label": "Phase 1 Gates",
            "state": "done",
            "detail": f"Headline: {triage_run['headline_outcome']}",
        })
    else:
        stages.append({"label": "Phase 1 Gates", "state": "current" if current == "NEW" else "pending",
                        "detail": "Not yet triaged"})

    needed_human_review = bool({"TO_REVIEW", "HANDOFF"} & ever_reached)
    if needed_human_review:
        state = "current" if current in ("TO_REVIEW", "HANDOFF") else "done"
        stages.append({"label": "Phase 1 Human Review", "state": state,
                        "detail": "Gate flagged/failed this for a manual double-check"})
    elif triage_run:
        stages.append({"label": "Phase 1 Human Review", "state": "skipped",
                        "detail": "Not needed — Phase 1 passed clean"})
    else:
        stages.append({"label": "Phase 1 Human Review", "state": "pending", "detail": ""})

    if phase2_assessment:
        stages.append({"label": "Phase 2 AI Scope Read", "state": "done",
                        "detail": f"Overall: {phase2_assessment['overall_rating']}"})
    elif current == "PHASE2_SCOPED":
        stages.append({"label": "Phase 2 AI Scope Read", "state": "current", "detail": "Awaiting scope read"})
    elif "PHASE2_SCOPED" in ever_reached:
        stages.append({"label": "Phase 2 AI Scope Read", "state": "skipped",
                        "detail": "Legacy item — no AI assessment recorded"})
    else:
        stages.append({"label": "Phase 2 AI Scope Read", "state": "pending", "detail": ""})

    if "AWAITING_PHASE2_APPROVAL" in ever_reached:
        state = "current" if current == "AWAITING_PHASE2_APPROVAL" else "done"
        stages.append({"label": "Phase 2 Approval", "state": state, "detail": "Owner reviews AI read + extracted facts"})
    else:
        stages.append({"label": "Phase 2 Approval", "state": "pending", "detail": ""})

    if "ESCALATED_TO_VICTORIA" in ever_reached:
        state = "current" if current == "ESCALATED_TO_VICTORIA" else "done"
        detail = "Awaiting Victoria's decision"
        if escalation_brief and escalation_brief["emailed_at"]:
            detail = f"Brief emailed {escalation_brief['emailed_at'][:10]}"
        stages.append({"label": "Escalated to Victoria", "state": state, "detail": detail})

    if current in TERMINAL:
        stages.append({"label": "Final Outcome", "state": "done", "detail": current.replace("_", " ").title()})
    else:
        stages.append({"label": "Final Outcome", "state": "pending", "detail": ""})

    return stages


def _process_pending_phase2(conn, owner: str | None) -> int:
    """Process notices in PHASE2_SCOPED status: run scope read and move to
    AWAITING_PHASE2_APPROVAL. Silently skips if the configured provider's key
    is not available. owner=None (Victoria) processes the whole pipeline;
    a sector owner's display_name scopes it to just their own notices, so
    clicking this button doesn't also spend API calls on other owners'
    reads -- matches the now-owner-scoped "N notices waiting" banner."""
    try:
        settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
        client, scope_read_fn = get_scope_read_client(settings)
        return approvals.process_pending_phase2_scope_reads(
            conn, client, scope_read_fn=scope_read_fn, owner=owner
        )
    except Exception as e:
        current_app.logger.warning("Phase 2 AI processing failed: %s", e)
        return 0


@queues_bp.route("/notices/add-manual", methods=["GET", "POST"])
@login_required
def add_notice_manual():
    """Manual notice entry for sources the automated sweep can't reach --
    currently just eTendersNI, whose search form requires solving a
    mandatory CAPTCHA (a deliberate anti-automation control we won't
    script around, see config_sources' notes on that source). A person
    searches the real site themselves in their own browser and enters
    what they find here, so it still flows through the same Phase 1/
    Phase 2 pipeline as anything the automated sweep pulls in, instead of
    being tracked separately outside the app."""
    if request.method == "POST":
        conn = get_db()
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("queues.add_notice_manual"))

        buyer = request.form.get("buyer", "").strip() or None
        notice_url = request.form.get("notice_url", "").strip() or None
        deadline = request.form.get("deadline", "").strip() or None
        uk_stage = request.form.get("uk_stage") or "UK3"
        description = request.form.get("description", "").strip()
        ref = request.form.get("ref", "").strip() or f"MANUAL-{int(datetime.now(timezone.utc).timestamp())}"

        notice = Notice(
            ref=ref,
            title=title,
            buyer=buyer,
            source="eTendersNI (manual entry)",
            notice_type=None,
            uk_stage=uk_stage,
            raw_json="{}",
            deadline=deadline,
            notice_url=notice_url,
        )
        parsed = ParsedNotice(
            notice=notice,
            text_blob=f"{title}\n{description}".lower(),
            tender_status=None,
            tender_period_end=deadline,
        )
        notice_id = upsert_notice(conn, parsed, actor=f"manual_entry:{current_user.display_name}")
        triage_notice(conn, notice_id, actor=current_user.display_name)
        flash(f"Notice added and triaged: {ref}")
        return redirect(url_for("queues.notice_detail", notice_id=notice_id))

    return render_template("add_notice_manual.html")


@queues_bp.route("/queue")
@login_required
def index():
    conn = get_db()
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    in_scope_where, in_scope_params = in_scope_filter_sql(conn)

    # Owner-scoped (2026-07-30). Victoria never sees this at all (2026-08-09):
    # Phase 2 scope reads are an owner-level concern, not hers -- she only
    # acts once a notice reaches her Escalated queue, after the owner has
    # already handled Phase 1 and Phase 2 themselves.
    if current_user.is_victoria:
        phase2_pending_count = 0
    else:
        phase2_pending_count = conn.execute(
            f"SELECT COUNT(*) FROM notices WHERE status = 'PHASE2_SCOPED' AND {in_scope_where} AND owner = ?",
            (*in_scope_params, current_user.display_name),
        ).fetchone()[0]

    # Phase 1 queue: machine FAILs awaiting owner double-check (TO_REVIEW).
    # Filter strictly by owner display_name -- Victoria is never a sector
    # owner (see db/seed_config.py), so this naturally resolves to nothing
    # for her. She only acts once a notice reaches her Escalated queue
    # below, after the owner has done Phase 1 and Phase 2 themselves; she
    # can still browse everything read-only via /opportunities.
    # 2026-07-30: also scoped to in_scope_filter_sql, consistent with the
    # Overview/sidebar/Opportunities -- by explicit choice, even though a
    # text-only Gate 2 fail with an out-of-range CPV won't show here.
    phase1_rows = conn.execute(f"""
        SELECT n.id, n.ref, n.title, n.buyer, n.owner, n.sector,
               n.indicative_value, n.deadline, n.uk_stage, n.cpv_primary,
               tr.headline_outcome, tr.headline_reason
        FROM notices n
        LEFT JOIN triage_runs tr ON tr.id = (
            SELECT MAX(id) FROM triage_runs WHERE notice_id = n.id
        )
                WHERE n.status IN ('TO_REVIEW', 'HANDOFF')
          AND {in_scope_where}
          AND n.owner = ?
        ORDER BY n.deadline IS NULL, n.deadline ASC
    """, (*in_scope_params, current_user.display_name)).fetchall()

    # Phase 2 queue: scope read done (or manually advanced), awaiting owner
    # confirmation. Same owner-only filter as Phase 1 above.
    phase2_rows = conn.execute(f"""
        SELECT n.id, n.ref, n.title, n.buyer, n.owner, n.sector,
               n.indicative_value, n.deadline, n.uk_stage,
               p.capability_fit_rating, p.right_to_win_rating, p.overall_rating,
               p.overall_reasoning
        FROM notices n
        LEFT JOIN phase2_assessments p ON p.id = (
            SELECT MAX(id) FROM phase2_assessments WHERE notice_id = n.id
        )
        WHERE n.status = 'AWAITING_PHASE2_APPROVAL'
          AND {in_scope_where}
          AND n.owner = ?
        ORDER BY n.deadline IS NULL, n.deadline ASC
    """, (*in_scope_params, current_user.display_name)).fetchall()

    escalated_rows = []
    if current_user.is_victoria:
        # Every ESCALATED_TO_VICTORIA notice has been through Phase 2 first
        # (2026-07-21 policy: escalation is only reachable from
        # AWAITING_PHASE2_APPROVAL) and always gets an escalation_briefs row
        # written synchronously by escalate_to_victoria at the moment it's
        # escalated -- so phase2_assessment and docx_path are never missing
        # here. Surfacing the Phase 2 rating (PURSUE/FLAG/DECLINE) so Victoria
        # can see it without opening each notice.
        escalated_rows = conn.execute(f"""
            SELECT n.id, n.ref, n.title, n.buyer, n.owner, n.sector,
                   n.indicative_value, n.deadline, n.uk_stage,
                   tr.headline_outcome, tr.headline_reason,
                   p.overall_rating, p.overall_reasoning,
                   eb.id AS brief_id, eb.trigger_reason, eb.emailed_at, eb.docx_path
            FROM notices n
            LEFT JOIN triage_runs tr ON tr.id = (
                SELECT MAX(id) FROM triage_runs WHERE notice_id = n.id
            )
            LEFT JOIN phase2_assessments p ON p.id = (
                SELECT MAX(id) FROM phase2_assessments WHERE notice_id = n.id
            )
            LEFT JOIN escalation_briefs eb ON eb.id = (
                SELECT MAX(id) FROM escalation_briefs
                WHERE notice_id = n.id AND brief_type = 'INTERNAL_ADDENDUM'
            )
            WHERE n.status = 'ESCALATED_TO_VICTORIA'
              AND {in_scope_where}
            ORDER BY n.deadline IS NULL, n.deadline ASC
        """, tuple(in_scope_params)).fetchall()

    return render_template(
        "queue.html",
        phase1_rows=phase1_rows,
        phase2_rows=phase2_rows,
        escalated_rows=escalated_rows,
        phase2_pending_count=phase2_pending_count,
        phase2_ready=settings.scope_read_ready,
        scope_read_key_name="OPENAI_API_KEY" if settings.scope_read_provider == "openai" else "ANTHROPIC_API_KEY",
    )


@queues_bp.route("/queue/process-phase2", methods=["POST"])
@login_required
def process_phase2():
    conn = get_db()
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    if not settings.scope_read_ready:
        key_name = "OPENAI_API_KEY" if settings.scope_read_provider == "openai" else "ANTHROPIC_API_KEY"
        flash(f"Set {key_name} in .env to run Phase 2 scope reads.", "error")
        return redirect(url_for("queues.index"))
    owner = None if current_user.is_victoria else current_user.display_name
    processed = _process_pending_phase2(conn, owner)
    if processed:
        flash(f"Completed {processed} Phase 2 AI scope read{'s' if processed != 1 else ''}.")
    else:
        flash("No Phase 2 AI scope reads completed. Check the AI provider configuration and try again.", "error")
    return redirect(url_for("queues.index"))


@queues_bp.route("/queue/advance-phase2-manual", methods=["POST"])
@login_required
def advance_phase2_manual():
    conn = get_db()
    count = approvals.advance_pending_phase2_without_scope_read(
        conn, current_user.display_name,
        owner=None if current_user.is_victoria else current_user.display_name,
    )
    if count:
        flash(f"{count} notice{'s' if count != 1 else ''} advanced to Phase 2 approval without an AI scope read.")
    else:
        flash("No notices were waiting for Phase 2.")
    return redirect(url_for("queues.index"))


@queues_bp.route("/notices/<int:notice_id>")
@login_required
def notice_detail(notice_id):
    conn = get_db()
    notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if not notice:
        abort(404)

    triage_run = conn.execute(
        "SELECT * FROM triage_runs WHERE notice_id = ? ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()

    # Scoped to the latest triage_run only -- a notice can have several runs
    # (retriage after a config fix, or moving between statuses during a bulk
    # redo), and showing every historical run's gate results alongside each
    # other reads as duplicated/contradictory cards for the same gate.
    gate_results = conn.execute(
        "SELECT * FROM gate_results WHERE triage_run_id = ? ORDER BY gate_number",
        (triage_run["id"],),
    ).fetchall() if triage_run else []

    phase2_assessment = conn.execute(
        "SELECT * FROM phase2_assessments WHERE notice_id = ? ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()

    status_history = conn.execute(
        "SELECT * FROM status_history WHERE notice_id = ? ORDER BY id",
        (notice_id,),
    ).fetchall()

    escalation_brief = conn.execute(
        "SELECT * FROM escalation_briefs WHERE notice_id = ? AND brief_type = 'INTERNAL_ADDENDUM' "
        "ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()
    brief_artifacts = conn.execute(
        "SELECT * FROM escalation_briefs WHERE notice_id = ? ORDER BY "
        "CASE brief_type WHEN 'INTERNAL_ADDENDUM' THEN 1 WHEN 'CAPTURE_BRIEF' THEN 2 "
        "WHEN 'ORIGINAL_NOTICE' THEN 3 ELSE 4 END, id DESC",
        (notice_id,),
    ).fetchall()

    try:
        lot_statuses = json.loads(notice["lot_statuses"]) if notice["lot_statuses"] else []
    except (ValueError, TypeError):
        lot_statuses = []

    try:
        cpv_additional = json.loads(notice["cpv_additional"]) if notice["cpv_additional"] else []
    except (ValueError, TypeError):
        cpv_additional = []

    try:
        bid_documents = json.loads(notice["bid_documents_json"]) if notice["bid_documents_json"] else []
    except (ValueError, TypeError):
        bid_documents = []
    for doc in bid_documents:
        doc["label"] = BID_DOCUMENT_TYPE_LABELS.get(doc.get("documentType"), doc.get("documentType") or "Document")

    # Prev/next within the same status queue for this user
    owner_filter = current_user.display_name
    is_vic = int(current_user.is_victoria)
    siblings = conn.execute("""
        SELECT id FROM notices
        WHERE status = ? AND (owner = ? OR ? = 1)
        ORDER BY deadline IS NULL, deadline ASC, id ASC
    """, (notice["status"], owner_filter, is_vic)).fetchall()
    sibling_ids = [r["id"] for r in siblings]

    prev_id = next_id = None
    if notice_id in sibling_ids:
        idx = sibling_ids.index(notice_id)
        if idx > 0:
            prev_id = sibling_ids[idx - 1]
        if idx < len(sibling_ids) - 1:
            next_id = sibling_ids[idx + 1]

    pipeline_stages = _build_pipeline_stages(
        notice, triage_run, phase2_assessment, escalation_brief, status_history
    )

    shortlisted = is_shortlisted(conn, notice_id)

    return render_template(
        "notice_detail.html",
        notice=notice,
        shortlisted=shortlisted,
        gate_results=gate_results,
        triage_run=triage_run,
        phase2_assessment=phase2_assessment,
        status_history=status_history,
        escalation_brief=escalation_brief,
        brief_artifacts=brief_artifacts,
        lot_statuses=lot_statuses,
        cpv_additional=cpv_additional,
        bid_documents=bid_documents,
        prev_id=prev_id,
        next_id=next_id,
        queue_total=len(sibling_ids),
        queue_position=sibling_ids.index(notice_id) + 1 if notice_id in sibling_ids else None,
        pipeline_stages=pipeline_stages,
    )



# Priority tiers for the Opportunities list (2026-09-05 UI alignment,
# "surface what to work on next"): ranks every notice by how much it needs
# Mark's attention right now, using the same AI/gate judgement the app
# already computes rather than a keyword-matched feed like the commercial
# competitor tools researched for this build. Lower tier = higher priority.
# A notice AWAITING_PHASE2_APPROVAL with no phase2_assessments row at all
# (advanced without an AI read) falls through to the plain
# AWAITING_PHASE2_APPROVAL case below the rating-specific ones, landing in
# tier 2 same as a FLAG rating -- it still needs a human decision, just
# without an AI opinion to lean on.
PRIORITY_TIER_SQL = """
    CASE
        WHEN n.status = 'AWAITING_PHASE2_APPROVAL' AND p2.overall_rating = 'PURSUE' THEN 1
        WHEN n.status = 'AWAITING_PHASE2_APPROVAL' AND p2.overall_rating = 'FLAG' THEN 2
        WHEN n.status IN ('TO_REVIEW', 'HANDOFF') THEN 2
        WHEN n.status = 'AWAITING_PHASE2_APPROVAL' AND p2.overall_rating = 'DECLINE' THEN 4
        WHEN n.status = 'AWAITING_PHASE2_APPROVAL' THEN 2
        WHEN n.status IN ('NEW', 'PHASE1_TRIAGED', 'PHASE2_SCOPED') THEN 3
        WHEN n.status IN ('REJECTED', 'PARKED') THEN 6
        ELSE 5
    END
"""


@queues_bp.route("/opportunities")
@login_required
def opportunities():
    """All notices across all statuses - the full pipeline view."""
    conn = get_db()

    status_filter = request.args.get("status", "")
    sector_filter = request.args.get("sector", "")
    stage_filter = request.args.get("stage", "")
    sort_filter = request.args.get("sort", "priority")

    # 2026-07-30: scoped to in_scope_filter_sql throughout (real sector, CPV
    # within that sector's scope, UK1-4), consistent with the Overview,
    # sidebar, and notifications -- by explicit choice, even for the active
    # queues, even though a text-only Gate 2 fail with an out-of-range CPV
    # won't show here.
    in_scope_where, in_scope_params = in_scope_filter_sql(conn)

    query = f"""
        SELECT n.id, n.ref, n.title, n.buyer, n.owner, n.sector, n.status,
               n.indicative_value, n.deadline, n.uk_stage, n.cpv_primary,
               n.first_seen_at, n.first_published_at, n.published_at, n.updated_at,
               n.publish_date_unknown, n.source,
               tr.headline_outcome, tr.headline_reason,
               p2.overall_rating,
               ({PRIORITY_TIER_SQL}) AS priority_tier
        FROM notices n
        LEFT JOIN triage_runs tr ON tr.id = (
            SELECT MAX(id) FROM triage_runs WHERE notice_id = n.id
        )
        LEFT JOIN phase2_assessments p2 ON p2.id = (
            SELECT MAX(id) FROM phase2_assessments WHERE notice_id = n.id
        )
        WHERE {in_scope_where}
    """
    params: list = list(in_scope_params)

    # 2026-07-30: strictly owner = current_user for every status, not just
    # the active-review ones. This used to let the OR clause fall through to
    # "true" for every other status (Escalated, Approved, Rejected, ...),
    # so e.g. Mark's "Escalated" pill showed his own count but clicking it
    # actually listed every sector's escalated notices -- the pill and the
    # list it linked to disagreed. Victoria still sees everything.
    if not current_user.is_victoria:
        query += " AND n.owner = ?"
        params.append(current_user.display_name)

    if status_filter:
        query += " AND n.status = ?"
        params.append(status_filter)

    if stage_filter and stage_filter in STAGE_STATUSES_BY_SLUG:
        statuses = STAGE_STATUSES_BY_SLUG[stage_filter]
        query += f" AND n.status IN ({','.join('?' for _ in statuses)})"
        params.extend(statuses)
        if stage_filter == "rejected":
            query += f" AND {victoria_sourced_reject_sql('n')}"

    if sector_filter:
        query += " AND n.sector = ?"
        params.append(sector_filter)

    if sort_filter == "newest":
        query += " ORDER BY n.first_seen_at DESC LIMIT 500"
    else:
        sort_filter = "priority"
        query += (
            " ORDER BY priority_tier ASC, n.deadline IS NULL, n.deadline ASC, "
            "n.first_seen_at DESC LIMIT 500"
        )

    notices = conn.execute(query, params).fetchall()

    # Master-detail preview pane (2026-09-06 UI alignment, modeled on
    # Contracts Advance's "All Contracts" screen): clicking a row shows a
    # read-only summary here instead of a full page navigation, so triaging
    # many notices doesn't cost a page load each. It's a preview only --
    # the full notice_detail page (all actions, documents, escalation
    # history) is still one click away, not duplicated here.
    selected_id = request.args.get("selected", type=int)
    selected_notice = None
    selected_phase2 = None
    if selected_id is not None:
        selected_notice = conn.execute(
            "SELECT n.*, tr.headline_outcome, tr.headline_reason "
            "FROM notices n LEFT JOIN triage_runs tr ON tr.id = "
            "(SELECT MAX(id) FROM triage_runs WHERE notice_id = n.id) WHERE n.id = ?",
            (selected_id,),
        ).fetchone()
        if selected_notice is not None:
            selected_phase2 = conn.execute(
                "SELECT * FROM phase2_assessments WHERE notice_id = ? ORDER BY id DESC LIMIT 1",
                (selected_id,),
            ).fetchone()

    # Get distinct sectors for filter dropdown
    sectors = [r[0] for r in conn.execute(
        "SELECT DISTINCT sector FROM notices WHERE sector IS NOT NULL ORDER BY sector"
    ).fetchall()]

    # Status counts for the summary bar -- owner-scoped for everyone except
    # Victoria, same rule as the sidebar's Workflow Stages counts and the
    # main list above. Previously this counted every owner's notices
    # regardless of who was looking, so e.g. Hammad's "To Review" pill showed
    # the whole pipeline's count instead of just his own.
    if current_user.is_victoria:
        status_counts = {r[0]: r[1] for r in conn.execute(
            f"SELECT status, COUNT(*) FROM notices WHERE {in_scope_where} GROUP BY status",
            tuple(in_scope_params),
        ).fetchall()}
    else:
        status_counts = {r[0]: r[1] for r in conn.execute(
            f"SELECT status, COUNT(*) FROM notices WHERE {in_scope_where} AND owner = ? GROUP BY status",
            (*in_scope_params, current_user.display_name),
        ).fetchall()}

    return render_template(
        "opportunities.html",
        notices=notices,
        sectors=sectors,
        status_counts=status_counts,
        status_filter=status_filter,
        sector_filter=sector_filter,
        stage_filter=stage_filter,
        sort_filter=sort_filter,
        selected_id=selected_id,
        selected_notice=selected_notice,
        selected_phase2=selected_phase2,
    )


@queues_bp.route("/opportunities/bulk-shortlist", methods=["POST"])
@login_required
def bulk_shortlist():
    """Bulk add/remove selected rows to Shortlists. Deliberately kept to
    this and mark-docs-downloaded only (2026-09-06) -- approve/reject/park
    all carry real state-machine consequences (a required reason, or a
    paid AI scope-read call) that shouldn't fire unattended across a
    multi-select; shortlisting is purely a save-list toggle, safe either
    way."""
    conn = get_db()
    notice_ids = request.form.getlist("notice_ids", type=int)
    action = request.form.get("bulk_action")
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for notice_id in notice_ids:
        already = is_shortlisted(conn, notice_id)
        if action == "add" and not already:
            conn.execute(
                "INSERT INTO shortlisted_notices (notice_id, added_by, added_at) VALUES (?, ?, ?)",
                (notice_id, current_user.display_name, now),
            )
            count += 1
        elif action == "remove" and already:
            conn.execute("DELETE FROM shortlisted_notices WHERE notice_id = ?", (notice_id,))
            count += 1
    conn.commit()
    if count:
        verb = "Added" if action == "add" else "Removed"
        flash(f"{verb} {count} notice{'s' if count != 1 else ''} {'to' if action == 'add' else 'from'} Shortlists.")
    else:
        flash("No notices selected.", "error")
    return redirect(request.form.get("next") or url_for("queues.opportunities"))


@queues_bp.route("/opportunities/bulk-mark-docs-downloaded", methods=["POST"])
@login_required
def bulk_mark_docs_downloaded():
    conn = get_db()
    notice_ids = request.form.getlist("notice_ids", type=int)
    succeeded = 0
    failed = 0
    for notice_id in notice_ids:
        try:
            approvals.mark_docs_downloaded(
                conn, notice_id, current_user.display_name, current_user.is_victoria
            )
            succeeded += 1
        except (approvals.NotAuthorized, ValueError):
            failed += 1
    if succeeded:
        flash(f"Marked {succeeded} notice{'s' if succeeded != 1 else ''} as docs downloaded.")
    if failed:
        flash(
            f"Skipped {failed} notice{'s' if failed != 1 else ''} not in Approved/Capture Brief Drafted status.",
            "error",
        )
    if not succeeded and not failed:
        flash("No notices selected.", "error")
    return redirect(request.form.get("next") or url_for("queues.opportunities"))


@queues_bp.route("/notices/<int:notice_id>/approve", methods=["POST"])
@login_required
def approve(notice_id):
    conn = get_db()
    try:
        notice = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
        if not notice:
            raise ValueError("Notice not found.")

        if notice["status"] == "TO_REVIEW":
            settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
            client, scope_read_fn = get_scope_read_client(settings)
            approvals.approve_phase1(
                conn, notice_id, current_user.display_name, current_user.is_victoria,
                client, scope_read_fn=scope_read_fn,
            )
            flash("Fail overturned and sent for Phase 2 scope read.")
        elif notice["status"] == "AWAITING_PHASE2_APPROVAL":
            approvals.approve_phase2(
                conn, notice_id, current_user.display_name, current_user.is_victoria
            )
            flash("Approved by owner — sent to Victoria for her decision.")
        else:
            raise ValueError(f"Approve is not available for status {notice['status']}.")
    except (approvals.NotAuthorized, ValueError, RuntimeError) as exc:
        flash(str(exc), "error")
    except Exception as exc:  # noqa: BLE001 - most likely a missing/invalid ANTHROPIC_API_KEY
        flash(f"Scope read failed, notice left in its current queue: {exc}", "error")
    return redirect(url_for("queues.index"))


@queues_bp.route("/notices/<int:notice_id>/advance-phase2-manual", methods=["POST"])
@login_required
def advance_phase2_manual_single(notice_id):
    conn = get_db()
    try:
        approvals.advance_phase2_without_scope_read(
            conn, notice_id, current_user.display_name, current_user.is_victoria
        )
        flash("Advanced to Phase 2 approval without an AI scope read.")
    except (approvals.NotAuthorized, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("queues.notice_detail", notice_id=notice_id))


@queues_bp.route("/notices/<int:notice_id>/reject", methods=["POST"])
@login_required
def reject(notice_id):
    conn = get_db()
    try:
        approvals.reject_notice(
            conn, notice_id, current_user.display_name, current_user.is_victoria,
            request.form.get("reason", ""),
        )
        flash("Rejected.")
    except (approvals.NotAuthorized, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("queues.index"))


@queues_bp.route("/notices/<int:notice_id>/park", methods=["POST"])
@login_required
def park(notice_id):
    conn = get_db()
    try:
        approvals.park_notice(
            conn, notice_id, current_user.display_name, current_user.is_victoria,
            request.form.get("reason", ""),
        )
        flash("Parked.")
    except (approvals.NotAuthorized, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("queues.index"))


@queues_bp.route("/notices/<int:notice_id>/mark-docs-downloaded", methods=["POST"])
@login_required
def mark_docs_downloaded(notice_id):
    conn = get_db()
    try:
        approvals.mark_docs_downloaded(
            conn, notice_id, current_user.display_name, current_user.is_victoria
        )
        flash("Marked bid documents as downloaded.")
    except (approvals.NotAuthorized, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("queues.notice_detail", notice_id=notice_id))


@queues_bp.route("/notices/<int:notice_id>/mark-victoria-decision", methods=["POST"])
@login_required
def mark_victoria_decision(notice_id):
    conn = get_db()
    try:
        approvals.mark_victoria_decision(
            conn, notice_id, current_user.display_name, request.form.get("reason", "")
        )
        flash("Escalated to Victoria, brief generated.")
    except (approvals.NotAuthorized, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("queues.index"))


@queues_bp.route("/notices/<int:notice_id>/victoria-decision", methods=["POST"])
@login_required
def victoria_decision(notice_id):
    if not current_user.is_victoria:
        flash("Only Victoria can action an escalation.", "error")
        return redirect(url_for("queues.index"))
    conn = get_db()
    try:
        approvals.victoria_decision(
            conn,
            notice_id,
            current_user.display_name,
            request.form.get("decision", ""),
            request.form.get("reason") or None,
        )
        flash("Decision recorded.")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("queues.index"))


@queues_bp.route("/notices/<int:notice_id>/brief/<int:brief_id>")
@login_required
def view_brief(notice_id, brief_id):
    """Opens the generated PDF (Internal Addendum pre-decision, Capture
    Brief post-GO) directly in the browser -- 2026-08-09, previously the
    only way to see either was emailing it via Graph, and Graph isn't
    configured in every environment."""
    conn = get_db()
    notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if not notice:
        abort(404)
    if not current_user.is_victoria and notice["owner"] != current_user.display_name:
        flash("Only the owning sector lead or Victoria can view this document.", "error")
        return redirect(url_for("queues.index"))

    brief = conn.execute(
        "SELECT * FROM escalation_briefs WHERE id = ? AND notice_id = ?", (brief_id, notice_id)
    ).fetchone()
    if not brief or not os.path.exists(brief["docx_path"]):
        flash("Document not found -- it may not have been generated yet.", "error")
        return redirect(url_for("queues.notice_detail", notice_id=notice_id))

    label = brief["brief_type"].replace("_", " ").title()
    safe_ref = notice["ref"].replace("/", "-")
    # Briefs escalated before the 2026-08-09 PDF redesign are still real
    # .docx files on disk -- serve each by its actual on-disk extension
    # rather than assuming PDF, or a pre-redesign brief opens as a broken
    # "PDF" (wrong mimetype) instead of the Word doc it actually is.
    ext = os.path.splitext(brief["docx_path"])[1].lstrip(".").lower() or "pdf"
    mimetype = (
        "application/pdf" if ext == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return send_file(
        brief["docx_path"], mimetype=mimetype,
        as_attachment=False, download_name=f"{label} - {safe_ref}.{ext}",
    )


@queues_bp.route("/notices/<int:notice_id>/send-escalation-email", methods=["POST"])
@login_required
def send_escalation_email(notice_id):
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    if not settings.graph_configured:
        flash("Microsoft Graph is not configured yet, see the README.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    brief = conn.execute(
        "SELECT * FROM escalation_briefs WHERE notice_id = ? AND brief_type = 'INTERNAL_ADDENDUM' "
        "ORDER BY id DESC LIMIT 1", (notice_id,)
    ).fetchone()
    notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if not brief or not notice:
        flash("No escalation brief found for this notice.", "error")
        return redirect(url_for("queues.index"))

    if not current_user.is_victoria and notice["owner"] != current_user.display_name:
        flash("Only the owning sector lead can send this escalation.", "error")
        return redirect(url_for("queues.index"))

    recipient = "victoria.milan@bidsavvy.io"
    try:
        graph_send_escalation_email(
            recipient=recipient,
            subject=f"TRIAGE ESCALATION: {notice['title']}",
            body_text="Auto-generated provisional draft attached, for validation. Not a bid decision.",
            attachment_path=brief["docx_path"],
            sender_upn=settings.ms_graph_sender_upn,
            tenant_id=settings.ms_graph_tenant_id,
            client_id=settings.ms_graph_client_id,
            client_secret=settings.ms_graph_client_secret,
        )
        mark_emailed(conn, brief["id"], recipient)
        flash("Escalation email sent.")
    except Exception as exc:  # noqa: BLE001 - surface any Graph/whitelist failure to the UI
        flash(f"Failed to send: {exc}", "error")
    return redirect(url_for("queues.index"))
