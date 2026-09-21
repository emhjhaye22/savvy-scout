"""2026-09-21 explicit request ("check all... nothing filtered" for Admin):
the /queue Approval Queue previously scoped Phase 1/Phase 2 rows to
`owner = current_user.display_name` unconditionally, and hid the Escalated
queue from anyone but Victoria. Admin should see every owner's rows and a
read-only view of Escalated, without gaining Victoria's actual decision
authority (victoria_decision stays is_account_approver-only) and without
regressing Victoria's own pre-existing behaviour (she still sees nothing in
Phase 1/Phase 2 -- she only ever acts via Escalated)."""

from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def _insert_notice(
    conn, ref: str, status: str, owner: str | None,
    sector: str = "Fintech", cpv_primary: str = "72200000", uk_stage: str = "UK3",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO notices (
            ref, ocid, title, buyer, source, notice_type, uk_stage, status, sector, owner,
            indicative_value, cpv_primary, cpv_primary_inferred, cpv_additional, deadline,
            text_blob, tender_status, lot_statuses, tender_period_end, pme_due_date,
            future_notice_date, contract_end_date, is_award, raw_json, first_seen_at,
            last_swept_at, created_at, updated_at
        ) VALUES (
            ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?,
            NULL, ?, 0, NULL, NULL,
            '', NULL, NULL, NULL, NULL,
            NULL, NULL, 0, '{}', ?,
            ?, ?, ?
        )
        """,
        (
            ref, f"Title {ref}", f"Buyer {ref}", "Find a Tender", uk_stage, status, sector, owner,
            cpv_primary, now, now, now, now,
        ),
    )
    return cur.lastrowid


def _build_app(tmp_path):
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    trifork_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    now = datetime.now(timezone.utc).isoformat()
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        ("adminowner", generate_password_hash("testpass"), "Admin Owner", now, trifork_id),
    )
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at, client_id) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        ("victoria", generate_password_hash("testpass"), "Victoria", now, trifork_id),
    )
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", now, trifork_id),
    )
    setup_conn.commit()
    setup_conn.close()

    settings = Settings(
        db_path=db_path,
        lookback_days=7,
        find_a_tender_base_url="",
        contracts_finder_base_url="",
        flask_secret_key="test-key",
        ms_graph_tenant_id=None,
        ms_graph_client_id=None,
        ms_graph_client_secret=None,
        ms_graph_sender_upn=None,
    )
    app = create_app(settings)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app, db_path


def _login(app, username):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": "testpass"})
    return client


def test_admin_sees_every_owners_phase1_and_phase2_rows(tmp_path):
    app, db_path = _build_app(tmp_path)
    conn = get_connection(db_path)
    _insert_notice(conn, "REF-MARK-P1", "TO_REVIEW", owner="Mark")
    _insert_notice(conn, "REF-OTHER-P1", "TO_REVIEW", owner="Someone Else")
    _insert_notice(conn, "REF-OTHER-P2", "AWAITING_PHASE2_APPROVAL", owner="Someone Else")
    conn.commit()

    admin = _login(app, "adminowner")
    html = admin.get("/queue").get_data(as_text=True)
    assert "REF-MARK-P1" in html
    assert "REF-OTHER-P1" in html
    assert "REF-OTHER-P2" in html

    mark = _login(app, "mark")
    html = mark.get("/queue").get_data(as_text=True)
    assert "REF-MARK-P1" in html
    assert "REF-OTHER-P1" not in html
    assert "REF-OTHER-P2" not in html


def test_account_approver_still_sees_no_phase1_or_phase2_section(tmp_path):
    """Regression guard: a near-miss during this fix briefly used a combined
    "admin or approver" flag to bypass the owner filter, which would have
    newly exposed every owner's Phase 1/Phase 2 rows to Victoria -- she must
    keep seeing nothing there, exactly as before this change (she only acts
    once a notice reaches her via Escalated)."""
    app, db_path = _build_app(tmp_path)
    conn = get_connection(db_path)
    _insert_notice(conn, "REF-MARK-P1", "TO_REVIEW", owner="Mark")
    _insert_notice(conn, "REF-OTHER-P2", "AWAITING_PHASE2_APPROVAL", owner="Someone Else")
    conn.commit()

    victoria = _login(app, "victoria")
    html = victoria.get("/queue").get_data(as_text=True)
    # Both sections are entirely absent for Victoria (queue.html gates them
    # on `not current_user.is_account_approver`) -- REF assertions aren't
    # useful here since a notice's title also legitimately appears in the
    # topbar's unrelated "Recent activity" notification dropdown regardless
    # of role.
    assert "Ready for your review" not in html
    assert "Needs a second look" not in html


def test_admin_sees_escalated_queue_read_only(tmp_path):
    app, db_path = _build_app(tmp_path)
    conn = get_connection(db_path)
    _insert_notice(conn, "REF-ESCALATED", "ESCALATED_TO_VICTORIA", owner="Mark")
    conn.commit()

    admin = _login(app, "adminowner")
    html = admin.get("/queue").get_data(as_text=True)
    assert "Waiting on Victoria" in html
    assert "REF-ESCALATED" in html
    assert "View only" in html
    assert "victoria-decision" not in html and "Submit Decision" not in html

    victoria = _login(app, "victoria")
    html = victoria.get("/queue").get_data(as_text=True)
    assert "REF-ESCALATED" in html
    assert "Submit Decision" in html


def test_admin_view_subtitle_shown_only_for_admin(tmp_path):
    app, db_path = _build_app(tmp_path)

    admin = _login(app, "adminowner")
    html = admin.get("/queue").get_data(as_text=True)
    assert "Admin view" in html

    mark = _login(app, "mark")
    html = mark.get("/queue").get_data(as_text=True)
    assert "Admin view" not in html
