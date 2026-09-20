from datetime import date, datetime, timezone

from savvy_scout.reporting.victoria_export import export_victoria_package


def _ensure_mark_user(conn):
    """owner_display_names() (2026-09-20, replaces the static OWNER_NAMES
    tuple) reads real user rows -- unlike the old hardcoded list, it needs
    an actual "Mark" account to exist for status_history rows attributed to
    him to be found."""
    existing = conn.execute("SELECT 1 FROM users WHERE display_name = 'Mark'").fetchone()
    if existing:
        return
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, created_at, client_id) "
        "VALUES ('mark', 'x', 'Mark', 'admin', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), trifork_id),
    )
    conn.commit()


def _escalated_notice(conn, ref):
    _ensure_mark_user(conn)
    now = datetime.now(timezone.utc).isoformat()
    notice_id = conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, status, sector, owner, "
        "indicative_value, cpv_primary, deadline, text_blob, first_seen_at, last_swept_at, "
        "created_at, updated_at, raw_json) VALUES (?, ?, 'Buyer', 'Find a Tender', 'UK3', "
        "'ESCALATED_TO_VICTORIA', 'Fintech', 'Mark', 'GBP 100000', '72200000', '2026-08-20', "
        "'Requirement', ?, ?, ?, ?, '{}')",
        (ref, f"Title {ref}", now, now, now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO status_history (notice_id, from_status, to_status, changed_by, changed_at, reason) "
        "VALUES (?, 'AWAITING_PHASE2_APPROVAL', 'ESCALATED_TO_VICTORIA', 'Mark', ?, 'Escalated')",
        (notice_id, now),
    )
    conn.execute(
        "INSERT INTO phase2_assessments (notice_id, capability_fit_rating, capability_fit_reasoning, "
        "competitor_position_rating, competitor_position_reasoning, right_to_win_rating, "
        "right_to_win_reasoning, overall_rating, overall_reasoning, open_questions, model_used, created_at) "
        "VALUES (?, 'HIGH', 'Strong fit', 'UNKNOWN', 'Unknown', 'MED', 'Plausible', 'FLAG', "
        "'Owner-reviewed recommendation', '[]', 'test', ?)",
        (notice_id, now),
    )
    conn.commit()
    return notice_id


def test_monthly_report_uses_previous_complete_month_not_current(conn, tmp_path):
    # Regression (2026-08-16): the current calendar month isn't finished
    # yet, so exporting on 16 August must produce a July report, not a
    # premature (and misleadingly named) August one.
    _escalated_notice(conn, "REF-1")
    result = export_victoria_package(
        conn, str(tmp_path), reference_date=date(2026, 8, 16), owner="Mark",
    )
    assert result["monthly_report"].endswith("Trifork Scouting Monthly Report 2026-07.docx")


def test_january_wraps_to_previous_december(conn, tmp_path):
    _escalated_notice(conn, "REF-2")
    result = export_victoria_package(
        conn, str(tmp_path), reference_date=date(2026, 1, 15), owner="Mark",
    )
    assert result["monthly_report"].endswith("Trifork Scouting Monthly Report 2025-12.docx")
