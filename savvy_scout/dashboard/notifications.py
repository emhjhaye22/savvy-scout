"""Shared "needs attention" / "recent activity" counts, used by the global
topbar notification dropdowns (every page) via the context processor in
dashboard/__init__.py. home.py no longer renders these as full panels on the
Overview page -- they live in the topbar instead -- but the underlying
queries are the same ones that used to live there, just centralised here so
there's a single source of truth."""

import sqlite3
from datetime import datetime, timedelta, timezone

from savvy_scout.dashboard.scope_filter import in_scope_filter_sql

# Sidebar "Workflow Stages" legend groups (slug, css class, label, statuses).
# Shared with queues.opportunities, which accepts stage=<slug> to filter by
# the whole group rather than one exact status.
STAGE_GROUPS = [
    ("to_review", "phase1", "To Review / Handoff", ["TO_REVIEW", "HANDOFF"]),
    # 2026-07-30: split out of one combined "Phase 2 — Scope Read + Review"
    # bucket. That single link mixed PHASE2_SCOPED (still waiting for the AI
    # read to even run) with AWAITING_PHASE2_APPROVAL (read done, actually
    # ready for the owner to review) -- confusing next to the Opportunities
    # page's "Phase 2 Queue" pill, which only ever counted the latter. Now
    # both sidebar rows and that pill agree on what "ready for review" means.
    ("phase2_scoped", "phase2", "Phase 2 — Awaiting AI Read", ["PHASE2_SCOPED"]),
    ("phase2_review", "phase2", "Phase 2 — Ready for Review", ["AWAITING_PHASE2_APPROVAL"]),
    ("escalated", "escalated", "Escalated to Victoria", ["ESCALATED_TO_VICTORIA"]),
    ("approved", "approved", "Approved / Capture Brief / Active",
     ["APPROVED", "CAPTURE_BRIEF_DRAFTED", "DOCS_DOWNLOADED", "CALENDARED", "ACTIVE"]),
    ("rejected", "rejected", "Rejected / Parked", ["REJECTED", "PARKED"]),
]


def victoria_sourced_reject_sql(alias: str = "notices") -> str:
    """A notice only counts as "Rejected/Parked" (2026-07-30) if Victoria
    herself made that call -- i.e. its most recent status_history row is a
    direct ESCALATED_TO_VICTORIA -> REJECTED/PARKED transition (what
    victoria_decision writes). A sector owner's own Phase 1 or Phase 2
    reject/park is still a real, valid decision and still changes
    notices.status to REJECTED/PARKED -- it just doesn't count in this
    particular bucket, which is meant to reflect Victoria's own final calls
    specifically. `alias` is the notices table's alias/name in the query
    this gets spliced into."""
    return f"""EXISTS (
        SELECT 1 FROM status_history sh
        WHERE sh.notice_id = {alias}.id
          AND sh.to_status = {alias}.status
          AND sh.from_status = 'ESCALATED_TO_VICTORIA'
          AND sh.id = (SELECT MAX(id) FROM status_history WHERE notice_id = {alias}.id)
    )"""


# Victoria only ever acts on a notice once it's ESCALATED_TO_VICTORIA --
# Phase 1 ("To Review") and Phase 2 ("Awaiting AI Read" / "Ready for
# Review") are owner-level stages she has no action on (2026-08-09,
# explicit request). She still keeps the two stages downstream of her own
# decision (Approved, Rejected/Parked).
VICTORIA_STAGE_SLUGS = {"escalated", "approved", "rejected"}


def get_sidebar_stage_counts(conn: sqlite3.Connection, owner: str, is_approver: int) -> list[dict]:
    """One count per Workflow Stages row: Victoria sees only the stages she
    actually acts on (see VICTORIA_STAGE_SLUGS), every other owner sees
    every stage but scoped to only their own notices -- same owner-scoping
    rule as the rest of the dashboard (queues, notifications). 2026-07-30:
    also scoped to in_scope_filter_sql (real sector, CPV within that
    sector's scope, UK1-4) for consistency with the Overview and
    Opportunities -- including the active queues, by explicit choice,
    accepting that a text-only Gate 2 fail with an out-of-range CPV won't
    show here."""
    in_scope_where, in_scope_params = in_scope_filter_sql(conn)
    counts = []
    for slug, css_class, label, statuses in STAGE_GROUPS:
        if is_approver and slug not in VICTORIA_STAGE_SLUGS:
            continue
        placeholders = ",".join("?" for _ in statuses)
        extra_where = f" AND {victoria_sourced_reject_sql('notices')}" if slug == "rejected" else ""
        if is_approver:
            count = conn.execute(
                f"SELECT COUNT(*) FROM notices WHERE status IN ({placeholders}) AND {in_scope_where}{extra_where}",
                (*statuses, *in_scope_params),
            ).fetchone()[0]
        else:
            count = conn.execute(
                f"SELECT COUNT(*) FROM notices WHERE status IN ({placeholders}) AND {in_scope_where} AND owner = ?{extra_where}",
                (*statuses, *in_scope_params, owner),
            ).fetchone()[0]
        counts.append({"slug": slug, "css_class": css_class, "label": label, "count": count})
    return counts


def get_notification_context(conn: sqlite3.Connection, owner: str, is_approver: int) -> dict:
    in_scope_where, in_scope_params = in_scope_filter_sql(conn)
    attention_count = conn.execute(
        f"SELECT COUNT(*) FROM notices WHERE status IN ('TO_REVIEW', 'AWAITING_PHASE2_APPROVAL') "
        f"AND {in_scope_where} AND (owner = ? OR ? = 1)",
        (*in_scope_params, owner, is_approver),
    ).fetchone()[0]
    attention_rows = conn.execute(
        f"""
        SELECT n.id, n.ref, n.title, n.buyer, n.owner, n.status, n.deadline,
               tr.headline_outcome
        FROM notices n
        LEFT JOIN triage_runs tr ON tr.id = (
            SELECT MAX(id) FROM triage_runs WHERE notice_id = n.id
        )
        WHERE n.status IN ('TO_REVIEW', 'AWAITING_PHASE2_APPROVAL')
          AND {in_scope_where}
          AND (n.owner = ? OR ? = 1)
        ORDER BY n.deadline IS NULL, n.deadline ASC
        LIMIT 5
        """,
        (*in_scope_params, owner, is_approver),
    ).fetchall()

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    activity_count = conn.execute(
        """
        SELECT COUNT(*) FROM status_history sh
        JOIN notices n ON n.id = sh.notice_id
        WHERE sh.changed_at >= ? AND (n.owner = ? OR ? = 1)
        """,
        (since, owner, is_approver),
    ).fetchone()[0]
    if is_approver:
        activity_rows = conn.execute(
            """
            SELECT sh.changed_at, sh.changed_by, sh.from_status, sh.to_status, sh.reason,
                   n.id AS notice_id, n.ref, n.title
            FROM status_history sh
            JOIN notices n ON n.id = sh.notice_id
            ORDER BY sh.id DESC
            LIMIT 5
            """
        ).fetchall()
    else:
        activity_rows = conn.execute(
            """
            SELECT sh.changed_at, sh.changed_by, sh.from_status, sh.to_status, sh.reason,
                   n.id AS notice_id, n.ref, n.title
            FROM status_history sh
            JOIN notices n ON n.id = sh.notice_id
            WHERE n.owner = ?
            ORDER BY sh.id DESC
            LIMIT 5
            """,
            (owner,),
        ).fetchall()

    return {
        "attention_count": attention_count,
        "attention_rows": attention_rows,
        "activity_count": activity_count,
        "activity_rows": activity_rows,
    }
