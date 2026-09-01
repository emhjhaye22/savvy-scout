"""Approval workflow (SPEC.md B1), plus the routing that ties Phase 1 triage
outcomes into Phase 2 scope reads (B2) and Victoria escalation with an
auto-brief (B3). Nothing here proceeds without an owner's click; every
rejection or park requires a reason.

Escalation to Victoria (SPEC.md B3) is owner-mediated, not automatic: a
gate FLAG or Gate 3's MAYBE no longer escalates the instant Phase 1 triage
finishes (see triage.gates.triage_notice / sweep.runner.triage_pending) --
it routes to the automated Phase 2 scope read first, same as a PASS, so the
owner has the AI read alongside the original gate flag before deciding.
The owner then marks any notice in their queue "Victoria decision" (see
mark_victoria_decision below) for their own judgement reasons, whether or
not Phase 1 flagged it. retriage_and_route below is the one exception: a
config correction can still escalate a re-triaged notice directly, since
that notice was never routed through Phase 2 in the first place."""

import logging
import os
import sqlite3
from datetime import datetime, timezone

from savvy_scout.escalation.brief import (
    DEFAULT_BRIEFS_DIR,
    build_original_notice_pdf,
    record_brief,
)
from savvy_scout.escalation.word_documents import (
    build_capture_brief_docx,
    build_internal_addendum_docx,
)
from savvy_scout.export.trifork_pipeline import update_configured_trifork_pipeline
from savvy_scout.logging_util import log_audit, log_status_change
from savvy_scout.models.notice import Status, validate_transition
from savvy_scout.notifications import NotificationError, send_victoria_escalation_email
from savvy_scout.triage.gates import _sector_cpv_scope, retriage_notice
from savvy_scout.triage.scope_read import run_scope_read, save_scope_read

logger = logging.getLogger(__name__)


class NotAuthorized(PermissionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_trifork_tracker(conn: sqlite3.Connection) -> None:
    try:
        result = update_configured_trifork_pipeline(conn)
        if result:
            logger.info("Trifork pipeline tracker updated at %s", result["output_path"])
    except Exception:
        logger.exception("Owner decision completed, but Trifork pipeline tracker sync failed")


def _get_notice(conn: sqlite3.Connection, notice_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if row is None:
        raise ValueError(f"No notice with id {notice_id}")
    return row


def _require_owner_or_victoria(
    notice_row: sqlite3.Row, actor_display_name: str, actor_is_victoria: bool
) -> None:
    if actor_is_victoria:
        return
    if notice_row["owner"] != actor_display_name:
        raise NotAuthorized(
            f"{actor_display_name} is not the owner of notice {notice_row['ref']} "
            f"(owner: {notice_row['owner'] or 'unassigned'})"
        )


def _transition(
    conn: sqlite3.Connection,
    notice_row: sqlite3.Row,
    to_status: Status,
    actor: str,
    reason: str | None = None,
) -> None:
    current = Status(notice_row["status"])
    validate_transition(current, to_status)
    conn.execute(
        "UPDATE notices SET status = ?, updated_at = ? WHERE id = ?",
        (to_status.value, _now(), notice_row["id"]),
    )
    conn.commit()
    log_status_change(conn, notice_row["id"], current.value, to_status.value, actor, reason)


def _auto_reject_if_unowned(conn: sqlite3.Connection, notice_id: int, actor: str) -> None:
    """A notice that just reached AWAITING_PHASE2_APPROVAL with no owner
    (a contested sector, or a sector with no owner configured) has nobody to
    review it -- every queue filters by owner, so it would sit there
    invisibly forever otherwise (2026-07-28 decision, same call as the
    unowned-FAIL case in triage.gates.triage_notice). Auto-closes it instead,
    flagged auto_rejected_unowned so a later config fix can still recover it
    via retriage_and_route -- unlike a human's own REJECTED decision."""
    notice_row = _get_notice(conn, notice_id)
    if notice_row["owner"] is not None:
        return
    if Status(notice_row["status"]) != Status.AWAITING_PHASE2_APPROVAL:
        return
    current = Status(notice_row["status"])
    validate_transition(current, Status.REJECTED)
    conn.execute(
        "UPDATE notices SET status = ?, auto_rejected_unowned = 1, updated_at = ? WHERE id = ?",
        (Status.REJECTED.value, _now(), notice_id),
    )
    conn.commit()
    log_status_change(
        conn, notice_id, current.value, Status.REJECTED.value, actor,
        "Auto-rejected: contested or unowned sector, nobody to review it.",
    )


def escalate_to_victoria(
    conn: sqlite3.Connection,
    notice_id: int,
    actor: str,
    trigger_reason: str,
    briefs_dir: str = DEFAULT_BRIEFS_DIR,
) -> str:
    """Moves a notice to ESCALATED_TO_VICTORIA, generates its review
    document set, and emails Victoria directly (2026-08-09) -- previously
    the only notification route was the manual "Send Brief Email" button,
    which needs Microsoft Graph configured (it isn't in every environment).
    This SMTP-based email is best-effort: a missing Victoria email or a down
    SMTP server must not stop the escalation itself from completing."""
    notice_row = _get_notice(conn, notice_id)
    _transition(conn, notice_row, Status.ESCALATED_TO_VICTORIA, actor, trigger_reason)

    addendum_path = build_internal_addendum_docx(conn, notice_id, output_dir=briefs_dir)
    record_brief(conn, notice_id, trigger_reason, addendum_path, actor, brief_type="INTERNAL_ADDENDUM")
    capture_path = build_capture_brief_docx(conn, notice_id, output_dir=briefs_dir)
    record_brief(conn, notice_id, trigger_reason, capture_path, actor, brief_type="CAPTURE_BRIEF")
    notice_path = build_original_notice_pdf(conn, notice_id, output_dir=briefs_dir)
    record_brief(conn, notice_id, trigger_reason, notice_path, actor, brief_type="ORIGINAL_NOTICE")
    log_audit(conn, "notice", str(notice_id), "escalation_brief_generated", actor, trigger_reason)
    _sync_trifork_tracker(conn)

    _notify_victoria_of_escalation(conn, notice_id, trigger_reason)
    return addendum_path


def _notify_victoria_of_escalation(conn: sqlite3.Connection, notice_id: int, trigger_reason: str) -> None:
    victoria = conn.execute("SELECT email FROM users WHERE is_victoria = 1 LIMIT 1").fetchone()
    if not victoria or not victoria["email"]:
        logger.debug("No email on file for Victoria; skipping escalation email for notice %s.", notice_id)
        return

    notice = _get_notice(conn, notice_id)
    assessment = conn.execute(
        "SELECT overall_rating, overall_reasoning FROM phase2_assessments WHERE notice_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()
    app_url = (os.environ.get("SAVVY_SCOUT_APP_BASE_URL") or "").rstrip("/")

    try:
        send_victoria_escalation_email(
            victoria["email"], notice_id, notice["ref"], notice["title"], notice["buyer"],
            notice["sector"], notice["owner"], notice["indicative_value"], notice["deadline"],
            assessment["overall_rating"] if assessment else None,
            assessment["overall_reasoning"] if assessment else None,
            trigger_reason, app_url,
        )
    except NotificationError as exc:
        logger.warning("Could not send Victoria escalation email for notice %s: %s", notice_id, exc)


def approve_phase1(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    actor_is_victoria: bool,
    anthropic_client,
    scope_read_fn=run_scope_read,
) -> None:
    """Owner approves a Phase 1 result: runs the B2 scope read, then queues
    it for Phase 2 approval. The scope read runs before any status change, so
    an API failure (missing key, refusal, network error) leaves the notice
    untouched in TO_REVIEW rather than stranded mid-transition with no path
    back. scope_read_fn defaults to the Anthropic path for backward
    compatibility; pass triage.scope_read.get_scope_read_client(settings)'s
    result to use whichever provider SCOPE_READ_PROVIDER selects instead."""
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria)

    assessment = scope_read_fn(anthropic_client, conn, notice_row)

    _transition(conn, notice_row, Status.PHASE2_SCOPED, actor_display_name, "Phase 1 fail overturned")
    save_scope_read(conn, notice_id, assessment)
    log_audit(
        conn,
        "notice",
        str(notice_id),
        "scope_read_completed",
        "system_scope_read",
        None,
        {"overall_rating": assessment["overall"]["rating"]},
    )

    notice_row = _get_notice(conn, notice_id)
    _transition(
        conn, notice_row, Status.AWAITING_PHASE2_APPROVAL, actor_display_name, "Scope read complete"
    )


def reject_notice(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    actor_is_victoria: bool,
    reason: str,
) -> None:
    if not reason or not reason.strip():
        raise ValueError("A rejection reason is required.")
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria)
    was_owner_phase2_review = (
        Status(notice_row["status"]) == Status.AWAITING_PHASE2_APPROVAL
        and actor_display_name == "Mark"
    )
    _transition(conn, notice_row, Status.REJECTED, actor_display_name, reason)
    if was_owner_phase2_review:
        _sync_trifork_tracker(conn)


def approve_phase2(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    actor_is_victoria: bool,
    reason: str | None = None,
) -> None:
    """Owner confirms a Phase 2 result (2026-07-30 policy): owner approval is
    a gate BEFORE Victoria, never a substitute for her -- only
    victoria_decision('approve') reaches APPROVED. The AI's
    PURSUE/FLAG/DECLINE rating is informative only; the owner
    reviews it and any of the three ratings can be approved by the sector
    owner -- their own judgment can overrule a DECLINE. Approving always
    sends the notice to Victoria for her decision, with the same escalation
    brief either way."""
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria)
    if Status(notice_row["status"]) != Status.AWAITING_PHASE2_APPROVAL:
        raise ValueError(f"Notice {notice_row['ref']} is not in AWAITING_PHASE2_APPROVAL.")

    escalate_to_victoria(
        conn, notice_id, actor_display_name,
        reason or "owner_approved_phase2: sending for Victoria's decision",
    )


def park_notice(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    actor_is_victoria: bool,
    reason: str,
) -> None:
    if not reason or not reason.strip():
        raise ValueError("A reason is required to park a notice.")
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria)
    _transition(conn, notice_row, Status.PARKED, actor_display_name, reason)


def mark_docs_downloaded(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    actor_is_victoria: bool,
) -> None:
    """Owner confirms they've grabbed the bid documents (ITT/PQQ/spec --
    see notices.bid_documents_json) from the source portal, for their own
    tracking. Reachable from APPROVED or CAPTURE_BRIEF_DRAFTED (both already
    allowed in ALLOWED_TRANSITIONS) -- this status existed in the state
    machine since the beginning but had no route/action wired to it until
    2026-07-30."""
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria)
    if Status(notice_row["status"]) not in (Status.APPROVED, Status.CAPTURE_BRIEF_DRAFTED):
        raise ValueError(
            f"Notice {notice_row['ref']} must be Approved or have a Capture Brief drafted "
            f"first (status: {notice_row['status']})."
        )
    _transition(conn, notice_row, Status.DOCS_DOWNLOADED, actor_display_name, "Bid documents downloaded")


def mark_victoria_decision(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    reason: str,
    briefs_dir: str = DEFAULT_BRIEFS_DIR,
) -> str:
    """An owner marks a notice (not already gate-flagged) for Victoria's
    decision, triggering the same auto-brief as an automatic escalation.

    Only reachable once Phase 2 is done (AWAITING_PHASE2_APPROVAL): Victoria
    should only ever see a notice alongside its Phase 2 AI scope read, never
    straight off a raw Phase 1 gate flag (2026-07-21 policy)."""
    if not reason or not reason.strip():
        raise ValueError("A reason is required to mark a notice for Victoria's decision.")
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria=False)
    if Status(notice_row["status"]) != Status.AWAITING_PHASE2_APPROVAL:
        raise ValueError(
            f"Notice {notice_row['ref']} is not in AWAITING_PHASE2_APPROVAL "
            f"(status: {notice_row['status']}); Phase 2 must complete before "
            "escalating to Victoria."
        )
    return escalate_to_victoria(
        conn,
        notice_id,
        actor_display_name,
        f"owner_marked_victoria_decision: {reason}",
        briefs_dir=briefs_dir,
    )


def victoria_decision(
    conn: sqlite3.Connection,
    notice_id: int,
    actor_display_name: str,
    decision: str,
    reason: str | None = None,
) -> None:
    """Victoria's decision on an ESCALATED_TO_VICTORIA notice: unlocks
    (approve), parks or rejects it. decision is one of 'approve', 'park',
    'reject'."""
    notice_row = _get_notice(conn, notice_id)
    if Status(notice_row["status"]) != Status.ESCALATED_TO_VICTORIA:
        raise ValueError(f"Notice {notice_row['ref']} is not awaiting a Victoria decision.")

    if decision == "approve":
        _transition(conn, notice_row, Status.APPROVED, actor_display_name, reason)
        capture_brief = conn.execute(
            "SELECT id FROM escalation_briefs WHERE notice_id = ? AND brief_type = 'CAPTURE_BRIEF' "
            "ORDER BY id DESC LIMIT 1",
            (notice_id,),
        ).fetchone()
        if capture_brief is None:
            capture_path = build_capture_brief_docx(conn, notice_id, DEFAULT_BRIEFS_DIR)
            record_brief(
                conn,
                notice_id,
                "victoria_go_capture_brief",
                capture_path,
                actor_display_name,
                brief_type="CAPTURE_BRIEF",
            )
        notice_row = _get_notice(conn, notice_id)
        _transition(conn, notice_row, Status.CAPTURE_BRIEF_DRAFTED, actor_display_name, "Capture brief drafted")
    elif decision == "park":
        if not reason:
            raise ValueError("A reason is required to park a notice.")
        _transition(conn, notice_row, Status.PARKED, actor_display_name, reason)
    elif decision == "reject":
        if not reason:
            raise ValueError("A reason is required to reject a notice.")
        _transition(conn, notice_row, Status.REJECTED, actor_display_name, reason)
    else:
        raise ValueError(f"Unknown decision '{decision}'")


def retriage_and_route(
    conn: sqlite3.Connection,
    notice_id: int,
    actor: str = "system_retriage",
) -> str:
    """Re-evaluates a notice's gates (e.g. after a sector keyword correction)
    and routes it to wherever the new headline outcome now points, if that
    differs from where it's sitting. Only ever touches a notice either still
    in TO_REVIEW, or one this app itself auto-rejected for having no
    sector/owner (notices.auto_rejected_unowned) -- refuses everything else,
    so a keyword fix can never silently overwrite a real decision a human
    already made. An auto-rejected notice is brought back into TO_REVIEW
    first (clearing the flag) before re-evaluating, so the rest of this
    function doesn't need to care which entry point it came from.

    Routing matches triage_notice's current rules: PASS/FLAG/MAYBE all go to
    PHASE2_SCOPED for the automated Phase 2 scope read (no more auto-escalate
    on a fresh FLAG -- see the module docstring above), MONITOR to MONITOR,
    FAIL stays put."""
    notice_row = _get_notice(conn, notice_id)
    current_status = Status(notice_row["status"])
    if current_status == Status.REJECTED and notice_row["auto_rejected_unowned"]:
        _transition(conn, notice_row, Status.TO_REVIEW, actor, "Re-opened for retriage after a config fix")
        conn.execute("UPDATE notices SET auto_rejected_unowned = 0 WHERE id = ?", (notice_id,))
        conn.commit()
    elif current_status != Status.TO_REVIEW:
        raise ValueError(
            f"Refusing to re-triage notice {notice_row['ref']}: status is "
            f"{notice_row['status']}, not TO_REVIEW (or an auto-rejected, "
            "unowned notice). Re-triage only applies to notices no human "
            "has acted on yet."
        )

    headline_out = retriage_notice(conn, notice_id, actor)
    notice_row = _get_notice(conn, notice_id)  # refresh: sector/owner may have just changed

    if headline_out == "MONITOR":
        _transition(conn, notice_row, Status.MONITORING, actor, "Re-triage: now MONITORING")
    elif headline_out in ("PASS", "FLAG", "MAYBE"):
        _transition(
            conn, notice_row, Status.PHASE2_SCOPED, actor,
            f"Re-triage: now {headline_out}, proceeding to Phase 2 scope read",
        )
    elif headline_out == "FAIL":
        # Same deterministic-vs-fuzzy distinction as triage_notice, checked
        # across ALL gates from the latest run -- a notice can independently
        # FAIL two gates at once (e.g. a text-based Gate 2 fail term AND an
        # already-closed Gate 4), and headline_out/headline_gate only ever
        # surfaces the first FAIL in gate order. No owner, a Gate 3 UK5
        # stage, a Gate 4 already-closed/awarded tender, or a sector-scoped
        # CPV mismatch on ANY gate all auto-reject again; a fuzzy FAIL with
        # an owner and no other deterministic gate failing still gets a
        # real double-check.
        latest_run = conn.execute(
            "SELECT id FROM triage_runs WHERE notice_id = ? ORDER BY id DESC LIMIT 1", (notice_id,)
        ).fetchone()
        gate_outcomes = {
            r["gate_number"]: r["outcome"]
            for r in conn.execute(
                "SELECT gate_number, outcome FROM gate_results WHERE triage_run_id = ?",
                (latest_run["id"],),
            ).fetchall()
        } if latest_run else {}
        scoped_prefixes = _sector_cpv_scope(conn, notice_row["sector"]) if notice_row["sector"] else None
        cpv_scope_fail = bool(
            gate_outcomes.get("gate2") == "FAIL" and scoped_prefixes is not None and notice_row["cpv_primary"]
            and not any(notice_row["cpv_primary"].startswith(p) for p in scoped_prefixes)
        )
        is_deterministic_fail = (
            gate_outcomes.get("gate3") == "FAIL" or gate_outcomes.get("gate4") == "FAIL" or cpv_scope_fail
        )
        if notice_row["owner"] is None or is_deterministic_fail:
            conn.execute(
                "UPDATE notices SET status = ?, auto_rejected_unowned = 1, updated_at = ? WHERE id = ?",
                (Status.REJECTED.value, _now(), notice_id),
            )
            conn.commit()
            log_status_change(
                conn, notice_id, Status.TO_REVIEW.value, Status.REJECTED.value, actor,
                "Re-triage: still no human judgment call needed here, auto-rejected again.",
            )
        # else: fuzzy FAIL with an owner -- stays in TO_REVIEW, unchanged.

    return headline_out


def bring_back_escalated_for_gate_retriage(
    conn: sqlite3.Connection, actor: str = "system_gate_order_correction"
) -> dict:
    """One-time-use bulk operation for a gate/config correction (e.g. the
    2026-08-15 gate-order fix, SavvyScout_Gate_Logic_Final.md): every notice
    currently ESCALATED_TO_VICTORIA had its Phase 1 gates evaluated under
    whatever gate logic was live at the time, which may now be stale. Re-runs
    Phase 1 (retriage_notice -- records a fresh triage_run/gate_results,
    doesn't touch status) for each, then sends it back to PHASE2_SCOPED so
    the owner reviews the refreshed Phase 1 result -- and, if they still
    approve, a fresh Phase 2 scope read -- before it reaches Victoria again.
    Existing Phase 2 assessment/escalation-brief rows are left as historical
    audit trail, not deleted. Returns counts of what changed."""
    candidate_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM notices WHERE status = ?", (Status.ESCALATED_TO_VICTORIA.value,)
        ).fetchall()
    ]
    counts = {"checked": len(candidate_ids), "sent_to_phase2": 0}
    for notice_id in candidate_ids:
        retriage_notice(conn, notice_id, actor)
        notice_row = _get_notice(conn, notice_id)
        _transition(
            conn, notice_row, Status.PHASE2_SCOPED, actor,
            "Sent back to Phase 2 for owner review: Phase 1 gates re-evaluated under an "
            "updated gate configuration",
        )
        counts["sent_to_phase2"] += 1
    return counts


def retriage_all_unmatched(conn: sqlite3.Connection, actor: str = "system_retriage") -> dict:
    """Re-triages every notice with no sector assigned that's either still in
    TO_REVIEW or was auto-rejected for having no owner (the "obviously out of
    scope" Gate 1 FAIL/contested-FLAG bucket) -- the safe subset to bulk
    re-check after a sector keyword correction, since nothing there has had
    a human decision made on it yet. Returns counts of what changed."""
    candidate_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM notices WHERE sector IS NULL AND "
            "(status = ? OR (status = ? AND auto_rejected_unowned = 1))",
            (Status.TO_REVIEW.value, Status.REJECTED.value),
        ).fetchall()
    ]

    counts = {
        "checked": len(candidate_ids),
        "now_matched": 0,
        "sent_to_phase2": 0,
        "monitoring": 0,
        "still_unmatched": 0,
    }
    for notice_id in candidate_ids:
        headline_out = retriage_and_route(conn, notice_id, actor)
        notice_row = _get_notice(conn, notice_id)
        if notice_row["sector"]:
            counts["now_matched"] += 1
        else:
            counts["still_unmatched"] += 1
        if headline_out in ("PASS", "FLAG", "MAYBE"):
            counts["sent_to_phase2"] += 1
        elif headline_out == "MONITOR":
            counts["monitoring"] += 1
    return counts


def correct_pre_routing_fix_backlog(
    conn: sqlite3.Connection, actor: str = "system_backlog_correction"
) -> dict:
    """One-time correction for notices left behind by the retired
    auto_escalate_if_flagged behaviour (see the module docstring above): a
    gate FLAG or MAYBE used to escalate straight to Victoria the instant
    Phase 1 triage finished, before Phase 2 routing existed. Any such notice
    still sitting in ESCALATED_TO_VICTORIA has never had a Victoria decision
    recorded against it -- if it had, its status would have moved on -- so
    moving it into PHASE2_SCOPED overwrites nothing a human decided.

    Re-evaluates gates (unchanged logic; only routing changed today) and
    moves a re-confirmed FLAG/MAYBE from ESCALATED_TO_VICTORIA into
    PHASE2_SCOPED, same as a fresh triage would today. A notice already
    correctly sitting in TO_REVIEW (a FAIL, still routed
    there under the current rules) is left alone. Only ever touches
    notices still in TO_REVIEW or ESCALATED_TO_VICTORIA --
    current status is the proof no human decision has been recorded yet."""
    candidate_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM notices WHERE status IN ('TO_REVIEW', 'ESCALATED_TO_VICTORIA')"
        ).fetchall()
    ]

    counts = {"checked": len(candidate_ids), "moved_to_phase2": 0, "unchanged": 0}
    for notice_id in candidate_ids:
        notice_row = _get_notice(conn, notice_id)
        current_status = Status(notice_row["status"])
        headline_out = retriage_notice(conn, notice_id, actor)

        if headline_out in ("PASS", "FLAG", "MAYBE") and current_status == Status.ESCALATED_TO_VICTORIA:
            _transition(
                conn,
                notice_row,
                Status.PHASE2_SCOPED,
                actor,
                "Backlog correction: retired auto-escalate-on-flag routing, no Victoria "
                "decision was ever recorded for this notice",
            )
            counts["moved_to_phase2"] += 1
        else:
            counts["unchanged"] += 1

    return counts


def reclassify_phase2_scoped_backlog(
    conn: sqlite3.Connection, actor: str = "system_sector_fix"
) -> dict:
    """Re-evaluates gates for every notice still sitting in PHASE2_SCOPED
    after a Gate 1 sector-classification correction (see
    triage.sector_classifier's generic/identity keyword split). No human has
    seen any of these yet -- a Phase 2 assessment only exists once a notice
    reaches AWAITING_PHASE2_APPROVAL, and with no ANTHROPIC_API_KEY that step
    hasn't run -- so it's safe to recheck all of them.

    A notice that now computes FAIL (its old sector was a false positive from
    a bare industry-vocabulary keyword with no real product/capability
    coupling) moves back to TO_REVIEW, unowned, for the
    correct out-of-scope handling; _evaluate_and_record already clears its
    stale sector/owner. A notice that still computes PASS/FLAG/MAYBE stays in
    PHASE2_SCOPED, sector/owner unchanged."""
    candidate_ids = [
        row["id"] for row in conn.execute("SELECT id FROM notices WHERE status = 'PHASE2_SCOPED'").fetchall()
    ]

    counts = {"checked": len(candidate_ids), "moved_out_of_scope": 0, "unchanged": 0}
    for notice_id in candidate_ids:
        notice_row = _get_notice(conn, notice_id)
        headline_out = retriage_notice(conn, notice_id, actor)

        if headline_out == "FAIL":
            notice_row = _get_notice(conn, notice_id)
            _transition(
                conn,
                notice_row,
                Status.TO_REVIEW,
                actor,
                "Sector reclassification: no longer matches a confirmed sector after the "
                "Gate 1 bare-keyword coupling fix",
            )
            counts["moved_out_of_scope"] += 1
        else:
            counts["unchanged"] += 1

    return counts


def process_pending_phase2_scope_reads(
    conn: sqlite3.Connection, client, scope_read_fn=run_scope_read, owner: str | None = None
) -> int:
    """Processes notices in PHASE2_SCOPED status: runs scope read and moves
    each to AWAITING_PHASE2_APPROVAL. Called from the dashboard's "Process
    Phase 2" button to ensure Phase 2 items have assessments ready before
    owner review.

    Phase 2 assessment (ratings: HIGH/MED/LOW) is informative. Owner decides
    next action based on both Phase 1 outcome and Phase 2 ratings.

    scope_read_fn defaults to the Anthropic path for backward compatibility;
    pass triage.scope_read.get_scope_read_client(settings)'s result to use
    whichever provider SCOPE_READ_PROVIDER selects instead.

    owner=None (2026-07-30 default for Victoria, who has no sector of her
    own) processes the whole pipeline's pending queue; passing a sector
    owner's display_name scopes it to just their own notices, matching the
    "N notices are waiting for Phase 2 scope reads" banner they see -- a
    sector owner clicking that button shouldn't also spend API calls running
    other owners' AI reads.

    Returns count of processed items. Silently skips any that fail."""
    if owner is None:
        pending_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM notices WHERE status = ? ORDER BY deadline IS NULL, deadline ASC LIMIT 50",
                (Status.PHASE2_SCOPED.value,)
            ).fetchall()
        ]
    else:
        pending_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM notices WHERE status = ? AND owner = ? ORDER BY deadline IS NULL, deadline ASC LIMIT 50",
                (Status.PHASE2_SCOPED.value, owner)
            ).fetchall()
        ]

    processed = 0
    for notice_id in pending_ids:
        try:
            notice_row = _get_notice(conn, notice_id)
            assessment = scope_read_fn(client, conn, notice_row)
            
            _transition(
                conn, notice_row, Status.AWAITING_PHASE2_APPROVAL, "system_phase2",
                f"Phase 2 scope read complete: {assessment['overall']['rating']}"
            )
            save_scope_read(conn, notice_id, assessment)
            log_audit(
                conn, "notice", str(notice_id), "scope_read_completed",
                "system_phase2", None,
                {"overall_rating": assessment["overall"]["rating"]},
            )
            _auto_reject_if_unowned(conn, notice_id, "system_phase2")
            processed += 1
        except Exception as e:
            log_audit(
                conn, "notice", str(notice_id), "scope_read_failed",
                "system_phase2", str(e)
            )
            # Continue with next notice
    return processed


def _advance_phase2_without_scope_read_unchecked(
    conn: sqlite3.Connection, notice_row: sqlite3.Row, actor: str
) -> None:
    """Core logic shared by the single-notice and bulk entry points below --
    no ownership check here, callers are responsible for it (or for having
    already scoped their notice selection to the right owner)."""
    if Status(notice_row["status"]) != Status.PHASE2_SCOPED:
        raise ValueError(
            f"Notice {notice_row['ref']} is not in PHASE2_SCOPED "
            f"(status: {notice_row['status']})."
        )
    _transition(
        conn, notice_row, Status.AWAITING_PHASE2_APPROVAL, actor,
        "Manually advanced to Phase 2 approval without an AI scope read; "
        "reviewed on extracted notice details only.",
    )
    _auto_reject_if_unowned(conn, notice_row["id"], actor)


def advance_phase2_without_scope_read(
    conn: sqlite3.Connection, notice_id: int, actor_display_name: str, actor_is_victoria: bool = False
) -> None:
    """Manually advances ONE PHASE2_SCOPED notice straight to
    AWAITING_PHASE2_APPROVAL without running the B2 AI scope read (2026-07-21
    decision). The extracted notice fields (buyer, supplier, CPV description,
    procurement method/details, indicative value, deadline, region -- see the
    Procurement Details card on notice_detail.html) already give the owner
    enough to review at Phase 2, so a notice no longer needs to wait
    indefinitely for an AI capability/competitor/right-to-win read that may
    never run (e.g. no ANTHROPIC_API_KEY configured). No phase2_assessments
    row is created here, so the Phase 2 queue correctly shows no AI rating
    for this notice -- that reflects "no AI read was done", not a bug.

    run_scope_read still runs opportunistically wherever an API key is
    configured (see process_pending_phase2_scope_reads); this is the manual
    fallback for when it is not, not a replacement for it.

    2026-07-30: now enforces _require_owner_or_victoria like every sibling
    mutation (approve/reject/park) -- previously missing here, which let any
    logged-in owner advance another owner's notice by guessing/opening its
    notice_id URL directly."""
    notice_row = _get_notice(conn, notice_id)
    _require_owner_or_victoria(notice_row, actor_display_name, actor_is_victoria)
    _advance_phase2_without_scope_read_unchecked(conn, notice_row, actor_display_name)


def advance_pending_phase2_without_scope_read(
    conn: sqlite3.Connection, actor: str = "system_phase2_manual", owner: str | None = None
) -> int:
    """Bulk version of advance_phase2_without_scope_read: advances every
    PHASE2_SCOPED notice at once. Returns the count advanced.

    owner=None processes every sector's pending notices -- was previously the
    ONLY behavior, which meant any single owner clicking this button on the
    dashboard bulk-advanced every OTHER owner's notices too, attributed to
    themselves as actor (2026-07-30 fix). Pass a sector owner's display_name
    to scope it to just their own, matching the same rule as
    process_pending_phase2_scope_reads; Victoria (no sector of her own)
    keeps the global behavior."""
    if owner is None:
        pending_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM notices WHERE status = ?", (Status.PHASE2_SCOPED.value,)
            ).fetchall()
        ]
    else:
        pending_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM notices WHERE status = ? AND owner = ?", (Status.PHASE2_SCOPED.value, owner)
            ).fetchall()
        ]
    for notice_id in pending_ids:
        notice_row = _get_notice(conn, notice_id)
        _advance_phase2_without_scope_read_unchecked(conn, notice_row, actor)
    return len(pending_ids)

