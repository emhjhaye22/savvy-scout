import re
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def _build_app(tmp_path, csrf_enabled):
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    trifork_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", 0, datetime.now(timezone.utc).isoformat(), trifork_id),
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
    flask_app.config["WTF_CSRF_ENABLED"] = csrf_enabled
    return flask_app


@pytest.fixture
def app(tmp_path):
    return _build_app(tmp_path, csrf_enabled=False)


@pytest.fixture
def csrf_app(tmp_path):
    # CSRF is disabled for every other test in this file (and across this
    # suite) for convenience -- that blind spot is exactly what let the
    # save/delete profile forms ship without a csrf_token hidden field
    # (2026-09-19, caught only by a live smoke test against a running
    # server, not by the test suite). This fixture exists so that specific
    # regression has real coverage.
    return _build_app(tmp_path, csrf_enabled=True)


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


def _logged_in_client(app):
    client = app.test_client()
    client.post("/login", data={"username": "mark", "password": "testpass"})
    return client


def _insert_notice(conn, ref, title, cpv_primary="72200000", indicative_value=None, buyer="A Buyer", owner="Mark"):
    # Fintech is seeded (seed_config.py) with only CPV prefixes 72 (IT
    # services) and 48 (software) in scope -- anything else is silently
    # excluded by scope_filter.in_scope_filter_sql, same as the real
    # Opportunities screen. owner must be "Mark" to be visible to the
    # non-Victoria test user (queues.opportunities scopes non-Victoria users
    # to their own notices).
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, owner, sector, status, source, uk_stage, cpv_primary, "
        "indicative_value, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'Fintech', 'NEW', 'Find a Tender', 'UK3', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, title, buyer, owner, cpv_primary, indicative_value, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def test_save_profile_creates_client_scoped_row(app):
    client = _logged_in_client(app)
    resp = client.post(
        "/opportunities/profiles/save",
        data={"name": "Aerospace", "q": "radar", "sector": "", "cpv_prefix": "34", "min_value": "1000", "max_value": "50000"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Saved profile" in resp.data
    assert b"Aerospace" in resp.data

    conn = _db(app)
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    row = conn.execute("SELECT * FROM search_profiles WHERE name = 'Aerospace'").fetchone()
    assert row is not None
    assert row["client_id"] == trifork_id
    assert row["keyword"] == "radar"
    assert row["cpv_prefix"] == "34"
    assert row["min_value"] == 1000
    assert row["max_value"] == 50000


def test_save_profile_requires_name(app):
    client = _logged_in_client(app)
    resp = client.post("/opportunities/profiles/save", data={"name": "  "}, follow_redirects=True)
    assert b"Profile name is required" in resp.data
    conn = _db(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM search_profiles").fetchone()["n"] == 0


def test_saving_same_name_twice_updates_in_place(app):
    client = _logged_in_client(app)
    client.post("/opportunities/profiles/save", data={"name": "Aerospace", "q": "radar"})
    client.post("/opportunities/profiles/save", data={"name": "Aerospace", "q": "satellite"})

    conn = _db(app)
    rows = conn.execute("SELECT * FROM search_profiles WHERE name = 'Aerospace'").fetchall()
    assert len(rows) == 1
    assert rows[0]["keyword"] == "satellite"


def test_opportunities_keyword_filter_matches_title(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", "Radar maintenance contract")
    _insert_notice(conn, "REF-B", "Cleaning services contract")

    client = _logged_in_client(app)
    resp = client.get("/opportunities?q=radar")
    body = resp.data.decode()
    assert "Radar maintenance contract" in body
    assert "Cleaning services contract" not in body


def test_opportunities_cpv_prefix_filter(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", "IT services contract", cpv_primary="72200000")
    _insert_notice(conn, "REF-B", "Software licensing contract", cpv_primary="48000000")

    client = _logged_in_client(app)
    resp = client.get("/opportunities?cpv_prefix=72")
    body = resp.data.decode()
    assert "IT services contract" in body
    assert "Software licensing contract" not in body


def test_opportunities_value_range_filter(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", "Cheap contract", indicative_value="5000 GBP")
    _insert_notice(conn, "REF-B", "Mid contract", indicative_value="50000 GBP")
    _insert_notice(conn, "REF-C", "Expensive contract", indicative_value="500000 GBP")

    client = _logged_in_client(app)
    resp = client.get("/opportunities?min_value=10000&max_value=100000")
    body = resp.data.decode()
    assert "Mid contract" in body
    assert "Cheap contract" not in body
    assert "Expensive contract" not in body


def test_opportunities_profile_id_applies_saved_filters(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", "Radar maintenance", cpv_primary="72200000")
    _insert_notice(conn, "REF-B", "Radar unrelated CPV", cpv_primary="48000000")
    _insert_notice(conn, "REF-C", "Cleaning services", cpv_primary="72200000")

    client = _logged_in_client(app)
    client.post("/opportunities/profiles/save", data={"name": "Defence Radar", "q": "radar", "cpv_prefix": "72"})
    profile_id = conn.execute("SELECT id FROM search_profiles WHERE name = 'Defence Radar'").fetchone()["id"]

    resp = client.get(f"/opportunities?profile_id={profile_id}")
    body = resp.data.decode()
    assert "Radar maintenance" in body
    assert "Radar unrelated CPV" not in body
    assert "Cleaning services" not in body


def test_opportunities_unknown_profile_id_ignored(app):
    client = _logged_in_client(app)
    resp = client.get("/opportunities?profile_id=99999")
    assert resp.status_code == 200


def test_delete_profile_removes_it(app):
    conn = _db(app)
    client = _logged_in_client(app)
    client.post("/opportunities/profiles/save", data={"name": "Temp"})
    profile_id = conn.execute("SELECT id FROM search_profiles WHERE name = 'Temp'").fetchone()["id"]

    resp = client.post(f"/opportunities/profiles/{profile_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert conn.execute("SELECT * FROM search_profiles WHERE id = ?", (profile_id,)).fetchone() is None


def test_delete_profile_scoped_to_own_client(app):
    conn = _db(app)
    other_client_id = conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES ('Other Org', 1, ?, 'system') "
        "RETURNING id",
        (datetime.now(timezone.utc).isoformat(),),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO search_profiles (client_id, name, created_by, created_at) VALUES (?, 'Not Yours', 'system', ?)",
        (other_client_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    other_profile_id = conn.execute("SELECT id FROM search_profiles WHERE name = 'Not Yours'").fetchone()["id"]

    client = _logged_in_client(app)
    client.post(f"/opportunities/profiles/{other_profile_id}/delete", follow_redirects=True)

    assert conn.execute("SELECT * FROM search_profiles WHERE id = ?", (other_profile_id,)).fetchone() is not None


def _csrf_token(html: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token field found in page"
    return match.group(1).decode()


def test_save_and_delete_profile_forms_carry_a_csrf_token(csrf_app):
    """Regression test for a real bug (2026-09-19): the save/delete profile
    forms were built without a csrf_token hidden field, so both silently
    400'd with CSRF protection enabled -- invisible in every other test in
    this file because WTF_CSRF_ENABLED is off there. Only a live smoke test
    against a running server caught it; this pins it down permanently."""
    client = csrf_app.test_client()
    login_page = client.get("/login")
    client.post(
        "/login",
        data={"username": "mark", "password": "testpass", "csrf_token": _csrf_token(login_page.data)},
    )

    opps_page = client.get("/opportunities")
    assert opps_page.status_code == 200
    token = _csrf_token(opps_page.data)

    save_resp = client.post(
        "/opportunities/profiles/save",
        data={"name": "CSRF Check", "csrf_token": token},
        follow_redirects=True,
    )
    assert save_resp.status_code == 200
    assert b"Saved profile" in save_resp.data

    conn = _db(csrf_app)
    profile_id = conn.execute("SELECT id FROM search_profiles WHERE name = 'CSRF Check'").fetchone()["id"]

    delete_resp = client.post(
        f"/opportunities/profiles/{profile_id}/delete",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert delete_resp.status_code == 200
    assert conn.execute("SELECT * FROM search_profiles WHERE id = ?", (profile_id,)).fetchone() is None
