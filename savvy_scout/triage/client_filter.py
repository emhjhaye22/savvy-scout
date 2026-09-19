"""Multi-client triage (2026-09-06): a new client's own filter, deliberately
separate from Trifork's 5-gate + AI capability-fit system in gates.py.
Trifork's setup stays exactly as it is; a new client gets a much simpler
structured filter instead -- CPV codes, keywords, notice type/stage,
region, and value range -- the same shape as a portal's own advanced
search (Find a Tender, Contracts Finder). Matching combines fields with
AND (every configured field must match) and values within one field with
OR (any one CPV prefix, any one keyword, etc.)."""

import json
import re
import sqlite3
from datetime import datetime, timezone

from savvy_scout.triage.sector_classifier import _haystack, contains_keyword

# Duplicates dashboard/routes/competitor_intel.py's _parse_gbp rather than
# importing it: this module is a triage-layer dependency of the sweep
# pipeline, and importing anything under dashboard.* would pull in the
# whole Flask dashboard package (routes import each other, and several
# import sweep.runner), creating a circular import back to this module.
_VALUE_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*GBP\s*$", re.IGNORECASE)


def _parse_gbp(value: str | None) -> float | None:
    if not value:
        return None
    m = _VALUE_RE.match(value)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _load_list(value: str | None) -> list[str]:
    if not value:
        return []
    return json.loads(value)


def evaluate_client_filter(client_filter: sqlite3.Row, notice_row: sqlite3.Row) -> tuple[str, str]:
    """Returns (outcome, reason). outcome is PASS or FAIL -- there's no
    FLAG/MAYBE tier here, unlike Trifork's gates: a structured filter is a
    yes/no match on each configured field, not a judgement call. A field
    left unconfigured (empty/NULL) doesn't constrain the match at all."""
    cpv_prefixes = _load_list(client_filter["cpv_prefixes"])
    keywords = _load_list(client_filter["keywords"])
    notice_types = _load_list(client_filter["notice_types"])
    regions = _load_list(client_filter["regions"])
    min_value = client_filter["min_value"]
    max_value = client_filter["max_value"]

    if cpv_prefixes:
        cpv_primary = notice_row["cpv_primary"]
        if not cpv_primary or not any(cpv_primary.startswith(p) for p in cpv_prefixes):
            return "FAIL", f"CPV {cpv_primary or 'UNVERIFIED'} doesn't match any configured prefix ({', '.join(cpv_prefixes)})."

    if keywords:
        haystack = _haystack(notice_row["buyer"], notice_row["text_blob"] or "")
        if not any(contains_keyword(haystack, kw) for kw in keywords):
            return "FAIL", f"No configured keyword ({', '.join(keywords)}) found in the notice text."

    if notice_types:
        if notice_row["uk_stage"] not in notice_types:
            return "FAIL", f"Notice stage {notice_row['uk_stage']} isn't in the configured notice types ({', '.join(notice_types)})."

    if regions:
        if notice_row["buyer_region"] not in regions:
            return "FAIL", f"Buyer region {notice_row['buyer_region'] or 'UNVERIFIED'} isn't in the configured regions ({', '.join(regions)})."

    if min_value is not None or max_value is not None:
        parsed_value = _parse_gbp(notice_row["indicative_value"])
        # A notice with no stated value yet (common pre-award) doesn't fail
        # a value filter -- there's nothing to compare, and excluding it
        # would silently drop early-stage opportunities that just haven't
        # published a value.
        if parsed_value is not None:
            if min_value is not None and parsed_value < min_value:
                return "FAIL", f"Value £{parsed_value:,.0f} is below the configured minimum £{min_value:,.0f}."
            if max_value is not None and parsed_value > max_value:
                return "FAIL", f"Value £{parsed_value:,.0f} is above the configured maximum £{max_value:,.0f}."

    return "PASS", "Matches every configured filter field."


def run_client_triage(conn: sqlite3.Connection, client_id: int) -> int:
    """Evaluates one client's filter against every notice, recording a
    client_triage_results row per notice. Safe to re-run any time (e.g.
    after the client's filter changes, or for a newly-added client against
    the full existing notice backlog) -- results are upserted, not
    appended. Never touches Trifork's own triage_runs/gate_results tables
    or triage_notice(); this is a completely separate evaluation path."""
    client_filter = conn.execute(
        "SELECT * FROM client_filters WHERE client_id = ?", (client_id,)
    ).fetchone()
    if client_filter is None:
        return 0

    notices = conn.execute(
        "SELECT id, cpv_primary, buyer, text_blob, uk_stage, buyer_region, indicative_value FROM notices"
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for notice in notices:
        outcome, reason = evaluate_client_filter(client_filter, notice)
        conn.execute(
            "INSERT INTO client_triage_results (client_id, notice_id, outcome, reason, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_id, notice_id) DO UPDATE SET outcome = excluded.outcome, "
            "reason = excluded.reason, evaluated_at = excluded.evaluated_at",
            (client_id, notice["id"], outcome, reason, now),
        )
        count += 1
    conn.commit()
    return count


def client_filter_is_empty(filter_fields: dict) -> bool:
    """True when every field of a submitted client filter is unconfigured.
    evaluate_client_filter() treats an unconfigured field as "no
    constraint" (2026-09-19) -- an entirely empty filter would therefore
    PASS every notice in the backlog, landing a brand-new tenant's first
    login on the full undifferentiated notice stream instead of a
    meaningful match set. Called from admin.py before add_client()/
    update_client_filter() write the row."""
    return not (
        filter_fields["cpv_prefixes"]
        or filter_fields["keywords"]
        or filter_fields["notice_types"]
        or filter_fields["regions"]
        or filter_fields["min_value"]
        or filter_fields["max_value"]
    )


def get_client_matches(conn: sqlite3.Connection, client_id: int, status_filter: str | None = None) -> list[sqlite3.Row]:
    """Every notice currently passing client_id's filter, most recently
    evaluated first, joined with any recorded Shortlisted/Rejected action.
    Shared by the admin-only Client Matches screen (admin.py) and the
    tenant-facing self-service portal (client_portal.py) -- both need the
    exact same PASS-outcome + action-state view, scoped only by which
    client_id is passed in."""
    query = (
        "SELECT n.id, n.ref, n.title, n.buyer, n.cpv_primary, n.uk_stage, n.buyer_region, "
        "n.indicative_value, n.notice_url, r.reason, r.evaluated_at, "
        "COALESCE(a.status, 'NEW') AS action_status, a.note "
        "FROM client_triage_results r JOIN notices n ON n.id = r.notice_id "
        "LEFT JOIN client_notice_actions a ON a.client_id = r.client_id AND a.notice_id = r.notice_id "
        "WHERE r.client_id = ? AND r.outcome = 'PASS'"
    )
    params: list = [client_id]
    if status_filter:
        query += " AND COALESCE(a.status, 'NEW') = ?"
        params.append(status_filter)
    query += " ORDER BY r.evaluated_at DESC LIMIT 500"
    return conn.execute(query, params).fetchall()


def get_client_match_status_counts(conn: sqlite3.Connection, client_id: int) -> dict:
    return {
        row["action_status"]: row["cnt"]
        for row in conn.execute(
            "SELECT COALESCE(a.status, 'NEW') AS action_status, COUNT(*) AS cnt "
            "FROM client_triage_results r "
            "LEFT JOIN client_notice_actions a ON a.client_id = r.client_id AND a.notice_id = r.notice_id "
            "WHERE r.client_id = ? AND r.outcome = 'PASS' GROUP BY action_status",
            (client_id,),
        ).fetchall()
    }


def record_client_notice_status(
    conn: sqlite3.Connection, client_id: int, notice_id: int, status: str, note: str, actor: str
) -> None:
    conn.execute(
        "INSERT INTO client_notice_actions (client_id, notice_id, status, note, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client_id, notice_id) DO UPDATE SET status = excluded.status, note = excluded.note, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (client_id, notice_id, status, note, datetime.now(timezone.utc).isoformat(), actor),
    )
    conn.commit()


def run_client_triage_for_notice(conn: sqlite3.Connection, notice_row: sqlite3.Row) -> None:
    """Evaluates every active, non-Trifork client's filter against one
    notice -- called from the sweep pipeline so new notices get evaluated
    per-client going forward without waiting for a manual re-run. Trifork
    is deliberately excluded here: its notices are only ever evaluated by
    gates.py's triage_notice(), never by this module."""
    clients = conn.execute(
        "SELECT id FROM clients WHERE is_active = 1 AND name != 'Trifork'"
    ).fetchall()
    if not clients:
        return

    now = datetime.now(timezone.utc).isoformat()
    for client in clients:
        client_filter = conn.execute(
            "SELECT * FROM client_filters WHERE client_id = ?", (client["id"],)
        ).fetchone()
        if client_filter is None:
            continue
        outcome, reason = evaluate_client_filter(client_filter, notice_row)
        conn.execute(
            "INSERT INTO client_triage_results (client_id, notice_id, outcome, reason, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_id, notice_id) DO UPDATE SET outcome = excluded.outcome, "
            "reason = excluded.reason, evaluated_at = excluded.evaluated_at",
            (client["id"], notice_row["id"], outcome, reason, now),
        )
    conn.commit()
