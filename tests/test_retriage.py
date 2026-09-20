from savvy_scout.models.notice import Notice, Status
from savvy_scout.sources.ocds_parser import ParsedNotice
from savvy_scout.sweep.dedupe import upsert_notice
from savvy_scout.triage.gates import GateResult, headline_outcome, retriage_notice, triage_notice
from savvy_scout.workflow import approvals


def _make_notice(conn, ref, buyer, text_blob, cpv_primary=None):
    notice = Notice(
        ref=ref,
        title=f"Notice {ref}",
        buyer=buyer,
        source="Find a Tender",
        notice_type="UK3",
        uk_stage="UK3",
        raw_json="{}",
        cpv_primary=cpv_primary,
    )
    parsed = ParsedNotice(notice=notice, text_blob=text_blob, tender_status="active")
    notice_id = upsert_notice(conn, parsed)
    triage_notice(conn, notice_id)
    return notice_id


def test_retriage_notice_picks_up_a_newly_added_keyword(conn):
    # "Acme Utility Co" doesn't match "utilities" (singular vs plural), so
    # this FAILs Gate 1 on first pass, same as the real Scottish Hydro Electric
    # Power Distribution case that motivated this feature.
    notice_id = _make_notice(
        conn,
        "REF-RETRIAGE-1",
        "Acme Utility Co",
        "bespoke build of a direct award open tender for infrastructure monitoring systems",
        cpv_primary="72200000",
    )
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["sector"] is None
    # No sector match -> nobody to review it -> auto-rejected, but flagged
    # recoverable (unlike a human's own REJECTED decision).
    assert row["status"] == Status.REJECTED.value
    assert row["auto_rejected_unowned"] == 1

    conn.execute(
        "INSERT INTO config_sector_keywords (sector, keyword, notes) VALUES ('Energy', 'acme utility', NULL)"
    )
    conn.commit()

    new_headline = retriage_notice(conn, notice_id)
    assert new_headline == "PASS"

    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["sector"] == "Energy"
    assert row["owner"] == "Mark"


def test_retriage_notice_records_a_new_triage_run(conn):
    notice_id = _make_notice(conn, "REF-RETRIAGE-2", "Some Buyer", "unrelated text", cpv_primary="72200000")
    runs_before = conn.execute(
        "SELECT COUNT(*) AS n FROM triage_runs WHERE notice_id = ?", (notice_id,)
    ).fetchone()["n"]

    retriage_notice(conn, notice_id)

    runs_after = conn.execute(
        "SELECT COUNT(*) AS n FROM triage_runs WHERE notice_id = ?", (notice_id,)
    ).fetchone()["n"]
    assert runs_after == runs_before + 1


def test_headline_outcome_normalizes_legacy_maybe_to_flag():
    results = {
        "gate1": GateResult("PASS", "ok"),
        "gate2": GateResult("MAYBE", "legacy maybe state"),
        "gate3": GateResult("PASS", "ok"),
        "gate4": GateResult("PASS", "ok"),
        "gate5": GateResult("PASS", "ok"),
        "filter3": GateResult("PASS", "ok"),
    }

    _, outcome, reason = headline_outcome(results)

    assert outcome == "FLAG"
    assert "legacy maybe state" in reason


def test_retriage_and_route_refuses_non_pending_notice(conn):
    # Matches Fintech (bank + payments platform coupling) but FAILs Gate 2 on
    # "hardware" -> a real owner (Mark) is assigned, so this is a genuine
    # human decision to reject, not an auto-reject -- retriage must still
    # refuse to touch it, same as before.
    notice_id = _make_notice(
        conn, "REF-RETRIAGE-3", "Some Bank",
        "real-time payments platform integration, hardware appliance refresh",
    )
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["owner"] == "Mark"
    assert row["auto_rejected_unowned"] == 0
    approvals.reject_notice(conn, notice_id, "Victoria", "account_approver", "already decided")

    try:
        approvals.retriage_and_route(conn, notice_id)
        assert False, "expected a ValueError"
    except ValueError as exc:
        assert "not TO_REVIEW" in str(exc)


def test_retriage_and_route_sends_newly_flagged_to_phase2(conn):
    # Matches Energy via the new keyword, but nothing in Gate 2's term lists,
    # so it FLAGs at Gate 2 once Gate 1 stops being the (earlier) headline.
    # FLAG no longer auto-escalates (see workflow.approvals module docstring):
    # it goes to PHASE2_SCOPED like a PASS would, same as a fresh triage.
    notice_id = _make_notice(
        conn, "REF-RETRIAGE-4", "Acme Utility Co", "totally generic services with no gate 2 keyword"
    )
    conn.execute(
        "INSERT INTO config_sector_keywords (sector, keyword, notes) VALUES ('Energy', 'acme utility', NULL)"
    )
    conn.commit()

    headline = approvals.retriage_and_route(conn, notice_id)
    assert headline == "FLAG"

    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.PHASE2_SCOPED.value
    assert row["sector"] == "Energy"


def test_retriage_all_unmatched_only_touches_untouched_notices(conn):
    matched_after_fix = _make_notice(
        conn, "REF-RETRIAGE-5", "Acme Utility Co",
        "bespoke build of a direct award open tender", cpv_primary="72200000",
    )
    still_unmatched = _make_notice(conn, "REF-RETRIAGE-6", "Totally Unrelated Ltd", "nothing sector-specific")
    # A real sector/owner match (Fintech, via bank + payments platform
    # coupling) that a human then rejected -- a genuine decision, not an
    # auto-reject, and excluded by sector IS NULL regardless.
    already_rejected = _make_notice(
        conn, "REF-RETRIAGE-7", "Some Bank",
        "real-time payments platform integration, hardware appliance refresh",
    )
    approvals.reject_notice(conn, already_rejected, "Victoria", "account_approver", "pre-existing decision")

    conn.execute(
        "INSERT INTO config_sector_keywords (sector, keyword, notes) VALUES ('Energy', 'acme utility', NULL)"
    )
    conn.commit()

    counts = approvals.retriage_all_unmatched(conn)

    assert counts["checked"] == 2  # only the two still in AWAITING_PHASE1_APPROVAL with no sector
    assert counts["now_matched"] == 1
    assert counts["still_unmatched"] == 1

    matched_row = conn.execute(
        "SELECT * FROM notices WHERE id = ?", (matched_after_fix,)
    ).fetchone()
    assert matched_row["sector"] == "Energy"

    unmatched_row = conn.execute("SELECT * FROM notices WHERE id = ?", (still_unmatched,)).fetchone()
    assert unmatched_row["sector"] is None

    rejected_row = conn.execute("SELECT * FROM notices WHERE id = ?", (already_rejected,)).fetchone()
    assert rejected_row["status"] == Status.REJECTED.value  # untouched
