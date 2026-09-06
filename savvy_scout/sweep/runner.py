"""Orchestrates a full sweep: pull from every enabled config_sources row,
dedupe/upsert, run the expiry radar on award notices, then triage every
notice still in NEW status."""

import logging
import sqlite3
from datetime import datetime, timezone

from savvy_scout.config import Settings
from savvy_scout.sweep.dedupe import upsert_notice
from savvy_scout.sweep.expiry_radar import surface_if_expiring
from savvy_scout.sources.contracts_finder import sweep_contracts_finder
from savvy_scout.sources.contracts_finder_csv import sweep_contracts_finder_csv
from savvy_scout.sources.etendersni import sweep_etendersni
from savvy_scout.sources.find_a_tender import sweep_find_a_tender
from savvy_scout.sources.public_contracts_scotland import sweep_public_contracts_scotland
from savvy_scout.sources.sell2wales import sweep_sell2wales
from savvy_scout.triage.client_filter import run_client_triage_for_notice
from savvy_scout.triage.gates import triage_notice

logger = logging.getLogger(__name__)

# Dispatch key (config_sources.source_type) -> client function. Every client
# function has the same (base_url, lookback_days) -> Iterator[ParsedNotice]
# shape so a new source only needs a row here plus a config_sources entry --
# no changes to run_sweep itself.
SOURCE_REGISTRY = {
    "find_a_tender": sweep_find_a_tender,
    "contracts_finder": sweep_contracts_finder,
    # Closes a real coverage gap (2026-08-10, see contracts_finder_csv's
    # module docstring): notices syndicated via third-party portals are
    # absent from Contracts Finder's OCDS feed but present in its own CSV
    # export. Writes notices.source = "Contracts Finder" too, same as the
    # OCDS sweep -- sweep.dedupe's fuzzy title+buyer match merges the ones
    # both feeds see rather than duplicating them.
    "contracts_finder_csv": sweep_contracts_finder_csv,
    "public_contracts_scotland": sweep_public_contracts_scotland,
    "sell2wales": sweep_sell2wales,
    "etendersni": sweep_etendersni,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_sweep(conn: sqlite3.Connection, settings: Settings, triggered_by: str = "unknown") -> dict[str, int]:
    """triggered_by is a display_name (manual "Sweep now" click) or
    "scheduler" (the daily cron, see scheduler.py) -- recorded on the
    sweep_runs row so history shows who/what ran each sweep."""
    stats = {"pulled": 0, "expiring_leads": 0, "triaged": 0}

    run_id = conn.execute(
        "INSERT INTO sweep_runs (started_at, triggered_by) VALUES (?, ?)",
        (_now(), triggered_by),
    ).lastrowid
    conn.commit()

    source_rows = conn.execute(
        "SELECT name, source_type, base_url FROM config_sources WHERE enabled = 1"
    ).fetchall()

    for row in source_rows:
        sweep_fn = SOURCE_REGISTRY.get(row["source_type"])
        if sweep_fn is None:
            logger.warning(
                "Skipping sweep source %r: unrecognised source_type %r",
                row["name"], row["source_type"],
            )
            continue
        source_started_at = _now()
        source_pulled = 0
        error_message = None
        try:
            for parsed in sweep_fn(row["base_url"], settings.lookback_days):
                upsert_notice(conn, parsed)
                source_pulled += 1
                stats["pulled"] += 1
                if surface_if_expiring(conn, parsed):
                    stats["expiring_leads"] += 1
        except Exception as exc:
            logger.exception("Sweep source %r failed; continuing with remaining sources", row["name"])
            # Truncated: some client exceptions (e.g. an HTTPError) embed the
            # full response body, which can run to several KB and isn't
            # useful past the first line or two for "what went wrong."
            error_message = str(exc)[:1000]
        conn.execute(
            "INSERT INTO sweep_run_sources "
            "(sweep_run_id, source_name, status, pulled, error_message, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, row["name"], "failed" if error_message else "success",
                source_pulled, error_message, source_started_at, _now(),
            ),
        )
        conn.commit()

    stats["triaged"] = triage_pending(conn)

    conn.execute(
        "UPDATE sweep_runs SET finished_at = ?, total_pulled = ?, total_triaged = ?, total_expiring_leads = ? "
        "WHERE id = ?",
        (_now(), stats["pulled"], stats["triaged"], stats["expiring_leads"], run_id),
    )
    conn.commit()

    return stats


def get_recent_sweep_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Most recent sweep runs, each with its per-source results, for the
    Sweep History panel -- newest first."""
    runs = conn.execute(
        "SELECT * FROM sweep_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for run in runs:
        sources = conn.execute(
            "SELECT * FROM sweep_run_sources WHERE sweep_run_id = ? ORDER BY id", (run["id"],)
        ).fetchall()
        result.append({
            "id": run["id"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "triggered_by": run["triggered_by"],
            "total_pulled": run["total_pulled"],
            "total_triaged": run["total_triaged"],
            "total_expiring_leads": run["total_expiring_leads"],
            "sources": [
                {
                    "source_name": s["source_name"],
                    "status": s["status"],
                    "pulled": s["pulled"],
                    "error_message": s["error_message"],
                }
                for s in sources
            ],
        })
    return result


def triage_pending(conn: sqlite3.Connection) -> int:
    """Triages every notice still sitting in NEW status. Separated from
    run_sweep so the regression tool (A5) can re-triage notices already in
    the database without re-hitting the live APIs.

    PASS, FLAG and MAYBE headline outcomes all route straight to PHASE2_SCOPED
    for the automated Phase 2 scope read, bypassing the Phase 1 owner queue
    entirely (only FAIL lands in AWAITING_PHASE1_APPROVAL, for an owner
    double-check). A FLAG/MAYBE notice does not auto-escalate to Victoria at
    this point: the owner sees the Gate 1 flag alongside the Phase 2 AI read
    once it reaches AWAITING_PHASE2_APPROVAL, and marks it for Victoria's
    decision themselves (see workflow.approvals.mark_victoria_decision)."""
    pending_ids = [
        row["id"] for row in conn.execute("SELECT id FROM notices WHERE status = 'NEW'").fetchall()
    ]
    triaged = 0
    for notice_id in pending_ids:
        try:
            triage_notice(conn, notice_id)
            triaged += 1
        except Exception:
            # One bad notice (a transient DB lock conflict, a data edge case)
            # must not strand every notice after it in NEW -- caught here so
            # the daily sweep never silently leaves a growing backlog behind
            # a single failure (2026-07-30: found ~289 stuck this way after
            # the scheduled sweep ran alongside other DB activity).
            logger.exception("Triage failed for notice %s; continuing with remaining notices", notice_id)

        try:
            notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
            if notice_row is not None:
                run_client_triage_for_notice(conn, notice_row)
        except Exception:
            # Multi-client triage (2026-09-06) is a completely separate,
            # additive evaluation -- a failure here must never affect
            # Trifork's own triage result above, which has already
            # committed by this point.
            logger.exception("Client-filter triage failed for notice %s", notice_id)
    return triaged
