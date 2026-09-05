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
        "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", 0, datetime.now(timezone.utc).isoformat()),
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


def _logged_in_client(app):
    client = app.test_client()
    client.post("/login", data={"username": "mark", "password": "testpass"})
    return client


def _insert_notice(conn, ref, title="An opportunity", buyer="A Buyer", sector="Fintech"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, status, source, uk_stage, "
        "raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'NEW', 'Find a Tender', 'UK3', '{}', ?, ?, ?, ?)",
        (ref, title, buyer, sector, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def test_shortlists_requires_login(app):
    client = app.test_client()
    resp = client.get("/shortlists")
    assert resp.status_code in (302, 401)


def test_shortlists_empty_state(app):
    client = _logged_in_client(app)
    resp = client.get("/shortlists")
    assert resp.status_code == 200
    assert b"Nothing shortlisted yet" in resp.data


def test_notice_detail_shows_unshortlisted_by_default(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A")
    client = _logged_in_client(app)
    resp = client.get(f"/notices/{notice_id}")
    assert resp.status_code == 200
    assert "☆ Shortlist".encode() in resp.data


def test_toggle_adds_and_shows_on_shortlists_page(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A", title="Widget Supply Contract")
    client = _logged_in_client(app)

    resp = client.post("/shortlists/toggle", data={"notice_id": notice_id}, follow_redirects=True)
    assert resp.status_code == 200

    row = conn.execute("SELECT * FROM shortlisted_notices WHERE notice_id = ?", (notice_id,)).fetchone()
    assert row is not None
    assert row["added_by"] == "Mark"

    list_resp = client.get("/shortlists")
    assert b"Widget Supply Contract" in list_resp.data

    detail_resp = client.get(f"/notices/{notice_id}")
    assert "★ Shortlisted".encode() in detail_resp.data


def test_toggle_twice_removes_it(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A")
    client = _logged_in_client(app)

    client.post("/shortlists/toggle", data={"notice_id": notice_id})
    client.post("/shortlists/toggle", data={"notice_id": notice_id})

    row = conn.execute("SELECT * FROM shortlisted_notices WHERE notice_id = ?", (notice_id,)).fetchone()
    assert row is None

    list_resp = client.get("/shortlists")
    assert b"Nothing shortlisted yet" in list_resp.data


def test_toggle_redirects_to_next_param(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A")
    client = _logged_in_client(app)

    resp = client.post(
        "/shortlists/toggle",
        data={"notice_id": notice_id, "next": f"/notices/{notice_id}"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/notices/{notice_id}"
