import json
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
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at) "
        "VALUES (?, ?, ?, 0, 1, ?)",
        ("emhjhaye", generate_password_hash("testpass"), "emhjhaye", datetime.now(timezone.utc).isoformat()),
    )
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at) "
        "VALUES (?, ?, ?, 1, 0, ?)",
        ("victoria", generate_password_hash("testpass"), "Victoria", datetime.now(timezone.utc).isoformat()),
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
    return flask_app


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


def _logged_in_client(app, username):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": "testpass"})
    return client


def _admin_client(app):
    return _logged_in_client(app, "emhjhaye")


def _insert_notice(conn, ref, cpv_primary="45200000", text_blob="construction of a new bridge"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, status, cpv_primary, text_blob, "
        "raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A notice', 'A Buyer', 'Find a Tender', 'UK3', 'NEW', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, cpv_primary, text_blob, now, now, now, now),
    )
    conn.commit()


def test_admin_index_shows_clients_section_for_admin(app):
    client = _admin_client(app)
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert b"Clients" in resp.data
    assert b"Add a client" in resp.data


def test_admin_index_hides_clients_section_from_victoria(app):
    client = _logged_in_client(app, "victoria")
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert b"Add a client" not in resp.data


def test_add_client_creates_client_and_filter(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    _insert_notice(conn, "REF-B", cpv_primary="72500000")

    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={"name": "Acme Construction", "cpv_prefixes": "45", "notice_types": ["UK3", "UK4"]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"evaluated 2 existing notices" in resp.data

    row = conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone()
    assert row is not None
    filter_row = conn.execute("SELECT * FROM client_filters WHERE client_id = ?", (row["id"],)).fetchone()
    assert json.loads(filter_row["cpv_prefixes"]) == ["45"]

    results = conn.execute("SELECT outcome FROM client_triage_results WHERE client_id = ?", (row["id"],)).fetchall()
    outcomes = {r["outcome"] for r in results}
    assert outcomes == {"PASS", "FAIL"}


def test_add_client_rejects_trifork_name(app):
    client = _admin_client(app)
    resp = client.post("/admin/clients/add", data={"name": "Trifork"}, follow_redirects=True)
    assert b"reserved" in resp.data


def test_add_client_rejects_duplicate_name(app):
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction"})
    resp = client.post("/admin/clients/add", data={"name": "Acme Construction"}, follow_redirects=True)
    assert b"already exists" in resp.data


def test_non_admin_cannot_add_client(app):
    client = _logged_in_client(app, "victoria")
    resp = client.post("/admin/clients/add", data={"name": "Acme Construction"}, follow_redirects=True)
    conn = _db(app)
    row = conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone()
    assert row is None
    assert b"Only the admin account" in resp.data


def test_update_client_filter_reevaluates_notices(app):
    conn = _db(app)
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    client_id = conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]

    _insert_notice(conn, "REF-A", cpv_primary="72500000")

    resp = client.post(
        f"/admin/clients/{client_id}/update-filter",
        data={"cpv_prefixes": "72"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    result = conn.execute(
        "SELECT outcome FROM client_triage_results WHERE client_id = ? AND notice_id = "
        "(SELECT id FROM notices WHERE ref = 'REF-A')",
        (client_id,),
    ).fetchone()
    assert result["outcome"] == "PASS"


def test_toggle_client_active(app):
    conn = _db(app)
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction"})
    client_id = conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]

    client.post(f"/admin/clients/{client_id}/toggle-active")
    row = conn.execute("SELECT is_active FROM clients WHERE id = ?", (client_id,)).fetchone()
    assert row["is_active"] == 0

    client.post(f"/admin/clients/{client_id}/toggle-active")
    row = conn.execute("SELECT is_active FROM clients WHERE id = ?", (client_id,)).fetchone()
    assert row["is_active"] == 1


def test_client_matches_view_shows_matched_notices(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    _insert_notice(conn, "REF-B", cpv_primary="72500000")

    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    client_id = conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]

    resp = client.get(f"/admin/clients/{client_id}/matches")
    assert resp.status_code == 200
    assert b"REF-A" in resp.data
    assert b"REF-B" not in resp.data


def test_client_matches_view_denied_to_non_admin(app):
    client = _logged_in_client(app, "victoria")
    resp = client.get("/admin/clients/1/matches", follow_redirects=True)
    assert b"Only the admin account" in resp.data


def _add_client_and_get_id(client, conn, name="Acme Construction", **form):
    form.setdefault("cpv_prefixes", "45")
    client.post("/admin/clients/add", data={"name": name, **form})
    return conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()["id"]


def test_matched_notice_defaults_to_new_status(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)

    resp = client.get(f"/admin/clients/{client_id}/matches")
    table_body = resp.data.decode().split("<tbody>", 1)[1]
    assert "cna-status-NEW" in table_body
    assert "cna-status-SHORTLISTED" not in table_body


def test_set_client_notice_status_to_shortlisted_with_note(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    resp = client.post(
        f"/admin/clients/{client_id}/notices/{notice_id}/set-status",
        data={"status": "SHORTLISTED", "note": "Looks promising"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchone()
    assert row["status"] == "SHORTLISTED"
    assert row["note"] == "Looks promising"
    assert b"Shortlisted" in resp.data
    assert b"Looks promising" in resp.data


def test_set_client_notice_status_upserts_on_repeat(app):
    """Changing a decision updates the existing row, not a duplicate."""
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    client.post(f"/admin/clients/{client_id}/notices/{notice_id}/set-status", data={"status": "SHORTLISTED"})
    client.post(f"/admin/clients/{client_id}/notices/{notice_id}/set-status", data={"status": "REJECTED"})

    rows = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "REJECTED"


def test_set_client_notice_status_rejects_invalid_status(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    resp = client.post(
        f"/admin/clients/{client_id}/notices/{notice_id}/set-status",
        data={"status": "BOGUS"},
        follow_redirects=True,
    )
    assert b"Invalid status" in resp.data
    row = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchone()
    assert row is None


def test_client_matches_status_filter(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000", text_blob="construction bridge one")
    _insert_notice(conn, "REF-B", cpv_primary="45300000", text_blob="construction bridge two")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_a = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    client.post(f"/admin/clients/{client_id}/notices/{notice_a}/set-status", data={"status": "SHORTLISTED"})

    resp = client.get(f"/admin/clients/{client_id}/matches?status=SHORTLISTED")
    body = resp.data.decode()
    assert "REF-A" in body
    assert "REF-B" not in body

    resp = client.get(f"/admin/clients/{client_id}/matches?status=NEW")
    body = resp.data.decode()
    assert "REF-A" not in body
    assert "REF-B" in body


def test_set_client_notice_status_denied_to_non_admin(app):
    client = _logged_in_client(app, "victoria")
    resp = client.post("/admin/clients/1/notices/1/set-status", data={"status": "SHORTLISTED"}, follow_redirects=True)
    assert b"Only the admin account" in resp.data
