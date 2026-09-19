from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    trifork_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, 0, 1, ?, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", datetime.now(timezone.utc).isoformat(), trifork_id),
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
    flask_app = create_app(settings)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


def _logged_in_client(app, username):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": "testpass"})
    return client


def _insert_notice(conn, ref, cpv_primary="45200000", text_blob="construction of a new bridge"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, status, cpv_primary, text_blob, "
        "raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A notice', 'A Buyer', 'Find a Tender', 'UK3', 'NEW', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, cpv_primary, text_blob, now, now, now, now),
    )
    conn.commit()


def _add_client(conn, name, is_active=1, cpv_prefix="45"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES (?, ?, ?, 'Mark')",
        (name, is_active, now),
    )
    client_id = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO client_filters (client_id, cpv_prefixes, keywords, notice_types, regions, "
        "min_value, max_value, updated_at, updated_by) VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, ?, 'Mark')",
        (client_id, f'["{cpv_prefix}"]', now),
    )
    return client_id


def _add_tenant_user(conn, username, display_name, client_id):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, 0, 0, ?, ?)",
        (username, generate_password_hash("testpass"), display_name, now, client_id),
    )
    conn.commit()


def test_tenant_sees_own_matches(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    _insert_notice(conn, "REF-B", cpv_primary="72500000")
    client_id = _add_client(conn, "Acme Construction")

    from savvy_scout.triage.client_filter import run_client_triage
    run_client_triage(conn, client_id)
    _add_tenant_user(conn, "acmeuser", "Acme User", client_id)

    client = _logged_in_client(app, "acmeuser")
    resp = client.get("/my-matches")
    assert resp.status_code == 200
    assert b"REF-A" in resp.data
    assert b"REF-B" not in resp.data


def test_tenant_can_set_status(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client_id = _add_client(conn, "Acme Construction")

    from savvy_scout.triage.client_filter import run_client_triage
    run_client_triage(conn, client_id)
    _add_tenant_user(conn, "acmeuser", "Acme User", client_id)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    client = _logged_in_client(app, "acmeuser")
    resp = client.post(
        f"/my-matches/{notice_id}/set-status",
        data={"status": "SHORTLISTED", "note": "Good fit"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchone()
    assert row["status"] == "SHORTLISTED"
    assert row["note"] == "Good fit"


def test_tenant_cannot_see_another_clients_matches(app):
    """IDOR check: client_portal never takes a client_id from the URL, so
    there's no parameter to manipulate -- confirm a tenant's own view is
    always scoped to their own client_id regardless of what other clients'
    data exists in the same database."""
    conn = _db(app)
    _insert_notice(conn, "REF-ACME", cpv_primary="45200000")
    _insert_notice(conn, "REF-OTHER", cpv_primary="72500000")
    acme_id = _add_client(conn, "Acme Construction", cpv_prefix="45")
    other_id = _add_client(conn, "Other Co", cpv_prefix="72")

    from savvy_scout.triage.client_filter import run_client_triage
    run_client_triage(conn, acme_id)
    run_client_triage(conn, other_id)
    _add_tenant_user(conn, "acmeuser", "Acme User", acme_id)
    _add_tenant_user(conn, "otheruser", "Other User", other_id)

    other_notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-OTHER'").fetchone()["id"]

    acme_client = _logged_in_client(app, "acmeuser")
    resp = acme_client.get("/my-matches")
    body = resp.data.decode()
    assert "REF-ACME" in body
    assert "REF-OTHER" not in body

    # Attempting to set status on a notice that isn't even in Acme's own
    # match set still only ever writes under the caller's OWN client_id
    # (record_client_notice_status takes client_id from
    # _require_tenant_client, never from the request) -- it can create a
    # stray action row for Acme, but must never touch Other Co's.
    acme_client.post(f"/my-matches/{other_notice_id}/set-status", data={"status": "SHORTLISTED"})
    other_action = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (other_id, other_notice_id)
    ).fetchone()
    assert other_action is None


def test_trifork_user_redirected_away_from_my_matches(app):
    client = _logged_in_client(app, "mark")
    resp = client.get("/my-matches", follow_redirects=True)
    assert b"active client filter" in resp.data


def test_paused_client_user_redirected_away_from_my_matches(app):
    conn = _db(app)
    client_id = _add_client(conn, "Acme Construction", is_active=0)
    _add_tenant_user(conn, "acmeuser", "Acme User", client_id)

    client = _logged_in_client(app, "acmeuser")
    resp = client.get("/my-matches", follow_redirects=True)
    assert b"active client filter" in resp.data
