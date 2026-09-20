from datetime import datetime, timedelta, timezone

from savvy_scout import scheduler
from savvy_scout.config import Settings


def _settings(report_recipient_email="mark@bidsavvy.io"):
    return Settings(
        db_path=":memory:",
        lookback_days=7,
        find_a_tender_base_url="",
        contracts_finder_base_url="",
        flask_secret_key="test-key",
        ms_graph_tenant_id=None,
        ms_graph_client_id=None,
        ms_graph_client_secret=None,
        ms_graph_sender_upn=None,
        report_recipient_email=report_recipient_email,
    )


def _trifork_id(conn):
    return conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]


def test_report_recipients_includes_victoria_and_mark(conn):
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at, client_id) "
        "VALUES ('victoria', 'x', 'Victoria', 'victoria.milan@bidsavvy.io', 'account_approver', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), _trifork_id(conn)),
    )
    conn.commit()

    recipients = scheduler._report_recipients(conn, _settings())

    assert recipients == ["mark@bidsavvy.io", "victoria.milan@bidsavvy.io"]


def test_report_recipients_deduplicates_when_mark_is_also_victoria(conn):
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at, client_id) "
        "VALUES ('victoria', 'x', 'Victoria', 'mark@bidsavvy.io', 'account_approver', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), _trifork_id(conn)),
    )
    conn.commit()

    recipients = scheduler._report_recipients(conn, _settings())

    assert recipients == ["mark@bidsavvy.io"]


def test_report_recipients_empty_when_none_configured(conn):
    assert scheduler._report_recipients(conn, _settings(report_recipient_email=None)) == []


def _escalate_notice(conn, ref, deadline=None, overall_rating=None, capability_fit_rating=None):
    now = datetime.now(timezone.utc).isoformat()
    notice_id = conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, status, sector, owner, "
        "indicative_value, cpv_primary, deadline, text_blob, first_seen_at, last_swept_at, "
        "created_at, updated_at, raw_json) VALUES (?, ?, 'Buyer', 'Find a Tender', 'UK3', "
        "'ESCALATED_TO_VICTORIA', 'Fintech', 'Mark', 'GBP 100000', '72200000', ?, 'Requirement', "
        "?, ?, ?, ?, '{}')",
        (ref, f"Title {ref}", deadline, now, now, now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO status_history (notice_id, from_status, to_status, changed_by, changed_at, reason) "
        "VALUES (?, 'AWAITING_PHASE2_APPROVAL', 'ESCALATED_TO_VICTORIA', 'Mark', ?, 'Owner decision')",
        (notice_id, now),
    )
    if overall_rating:
        conn.execute(
            "INSERT INTO phase2_assessments (notice_id, capability_fit_rating, capability_fit_reasoning, "
            "competitor_position_rating, competitor_position_reasoning, right_to_win_rating, "
            "right_to_win_reasoning, overall_rating, overall_reasoning, open_questions, model_used, created_at) "
            "VALUES (?, ?, 'Strong fit', 'UNKNOWN', 'Unknown', 'MED', 'Plausible', ?, "
            "'Owner-reviewed recommendation', '[]', 'test', ?)",
            (notice_id, capability_fit_rating, overall_rating, now),
        )
    conn.commit()
    return notice_id


def test_victoria_reminder_flags_near_deadline_as_urgent(conn, monkeypatch):
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at, client_id) "
        "VALUES ('victoria', 'x', 'Victoria', 'victoria.milan@bidsavvy.io', 'account_approver', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), _trifork_id(conn)),
    )
    near_deadline = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _escalate_notice(conn, "REF-URGENT", deadline=near_deadline, overall_rating="FLAG", capability_fit_rating="MED")

    calls = []
    monkeypatch.setattr(
        scheduler, "send_victoria_reminder_digest_email",
        lambda to, urgent, high_value, app_url: calls.append((to, urgent, high_value)),
    )
    monkeypatch.setattr(scheduler, "load_settings", lambda: _settings())
    monkeypatch.setattr(scheduler, "get_connection", lambda path: conn)

    scheduler.run_victoria_reminder_job()

    assert len(calls) == 1
    to, urgent, high_value = calls[0]
    assert to == "victoria.milan@bidsavvy.io"
    assert len(urgent) == 1
    assert urgent[0]["ref"] == "REF-URGENT"
    assert high_value == []


def test_victoria_reminder_flags_pursue_as_high_value_regardless_of_deadline(conn, monkeypatch):
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at, client_id) "
        "VALUES ('victoria', 'x', 'Victoria', 'victoria.milan@bidsavvy.io', 'account_approver', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), _trifork_id(conn)),
    )
    far_deadline = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    _escalate_notice(conn, "REF-STRONG", deadline=far_deadline, overall_rating="PURSUE", capability_fit_rating="HIGH")

    calls = []
    monkeypatch.setattr(
        scheduler, "send_victoria_reminder_digest_email",
        lambda to, urgent, high_value, app_url: calls.append((urgent, high_value)),
    )
    monkeypatch.setattr(scheduler, "load_settings", lambda: _settings())
    monkeypatch.setattr(scheduler, "get_connection", lambda path: conn)

    scheduler.run_victoria_reminder_job()

    assert len(calls) == 1
    urgent, high_value = calls[0]
    assert urgent == []
    assert len(high_value) == 1
    assert high_value[0]["ref"] == "REF-STRONG"


def test_victoria_reminder_skips_sending_when_nothing_qualifies(conn, monkeypatch):
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, created_at, client_id) "
        "VALUES ('victoria', 'x', 'Victoria', 'victoria.milan@bidsavvy.io', 'account_approver', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), _trifork_id(conn)),
    )
    far_deadline = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    _escalate_notice(conn, "REF-MEDIOCRE", deadline=far_deadline, overall_rating="FLAG", capability_fit_rating="MED")

    calls = []
    monkeypatch.setattr(
        scheduler, "send_victoria_reminder_digest_email",
        lambda to, urgent, high_value, app_url: calls.append(True),
    )
    monkeypatch.setattr(scheduler, "load_settings", lambda: _settings())
    monkeypatch.setattr(scheduler, "get_connection", lambda path: conn)

    scheduler.run_victoria_reminder_job()

    assert calls == []
