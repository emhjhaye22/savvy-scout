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
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


@pytest.fixture
def mark_client(app):
    client = app.test_client()
    client.post("/login", data={"username": "mark", "password": "testpass"})
    return client


def test_add_notice_manual_form_renders(mark_client):
    resp = mark_client.get("/notices/add-manual")
    assert resp.status_code == 200
    assert b"eTendersNI" in resp.data
    assert b"CAPTCHA" in resp.data


def test_add_notice_manual_requires_title(mark_client):
    resp = mark_client.post("/notices/add-manual", data={"title": ""}, follow_redirects=True)
    assert b"Title is required" in resp.data


def test_add_notice_manual_creates_and_triages_a_notice(app, mark_client):
    resp = mark_client.post(
        "/notices/add-manual",
        data={
            "title": "Digital case management system for a Northern Ireland council",
            "buyer": "Some NI Council",
            "description": "bespoke build of a digital case management platform, open tender",
            "uk_stage": "UK3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    conn = _db(app)
    row = conn.execute(
        "SELECT * FROM notices WHERE source = 'eTendersNI (manual entry)'"
    ).fetchone()
    assert row is not None
    assert row["title"] == "Digital case management system for a Northern Ireland council"
    assert row["status"] != "NEW"  # triage_notice ran and advanced it past NEW

    triage_run = conn.execute(
        "SELECT * FROM triage_runs WHERE notice_id = ?", (row["id"],)
    ).fetchone()
    assert triage_run is not None


def test_add_notice_manual_auto_generates_ref_when_blank(app, mark_client):
    mark_client.post(
        "/notices/add-manual",
        data={"title": "A notice with no reference given", "description": "some description"},
    )
    conn = _db(app)
    row = conn.execute(
        "SELECT ref FROM notices WHERE title = 'A notice with no reference given'"
    ).fetchone()
    assert row["ref"].startswith("MANUAL-")
