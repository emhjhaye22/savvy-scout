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
    for username, display_name in [
        ("victoria", "Victoria"), ("kanvesh", "Kanvesh"), ("mark", "Mark"), ("hammad", "Hammad"),
    ]:
        setup_conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                username,
                generate_password_hash("testpass"),
                display_name,
                int(display_name == "Victoria"),
                datetime.now(timezone.utc).isoformat(),
            ),
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


@pytest.fixture
def victoria_client(app):
    return _logged_in_client(app, "victoria")


@pytest.fixture
def mark_client(app):
    return _logged_in_client(app, "mark")


@pytest.fixture
def hammad_client(app):
    return _logged_in_client(app, "hammad")


def test_admin_index_requires_correction_authority(hammad_client):
    resp = hammad_client.get("/admin/", follow_redirects=True)
    assert b"Only Victoria or the admin account" in resp.data


def test_admin_index_allows_mark_correction_authority(mark_client):
    """2026-08-09: Mark was granted the same rule-correction authority as
    Victoria (explicit request), on top of his existing is_admin
    account-management authority -- distinct from is_admin, which only
    gates Manage Users. Kanvesh lost this same authority on 2026-09-01 when
    scouting consolidated to Mark alone."""
    resp = mark_client.get("/admin/")
    assert b"config_sector_keywords" in resp.data


def test_admin_index_shows_sector_keywords_table(victoria_client):
    resp = victoria_client.get("/admin/")
    assert b"config_sector_keywords" in resp.data


def test_add_row_to_sector_keywords(victoria_client, app):
    resp = victoria_client.post(
        "/admin/config/config_sector_keywords/add",
        data={"sector": "Fintech", "keyword": "Barclays", "reason": "Confirmed named buyer"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = _db(app)
    row = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE keyword = 'Barclays'"
    ).fetchone()
    assert row is not None
    assert row["sector"] == "Fintech"

    correction = conn.execute("SELECT * FROM rule_corrections ORDER BY id DESC LIMIT 1").fetchone()
    assert correction["entered_by"] == "Victoria"
    assert "Barclays" in correction["description"]


def test_add_row_requires_reason(victoria_client, app):
    resp = victoria_client.post(
        "/admin/config/config_sector_keywords/add",
        data={"sector": "Fintech", "keyword": "HSBC"},
        follow_redirects=True,
    )
    assert b"reason is required" in resp.data.lower()

    conn = _db(app)
    row = conn.execute("SELECT * FROM config_sector_keywords WHERE keyword = 'HSBC'").fetchone()
    assert row is None


def test_add_row_rejects_missing_required_field(victoria_client, app):
    resp = victoria_client.post(
        "/admin/config/config_sector_keywords/add",
        data={"sector": "Fintech", "reason": "test"},  # 'keyword' is required and missing
        follow_redirects=True,
    )
    assert b"missing required field" in resp.data.lower()

    conn = _db(app)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM config_sector_keywords WHERE sector = 'Fintech'"
    ).fetchone()["n"]
    # unchanged from the seeded set (nothing malformed got inserted)
    assert count > 0  # sanity: seeded rows exist
    row = conn.execute("SELECT * FROM config_sector_keywords WHERE keyword = ''").fetchone()
    assert row is None


def test_add_row_denied_for_non_correction_authority(hammad_client, app):
    hammad_client.post(
        "/admin/config/config_sector_keywords/add",
        data={"sector": "Fintech", "keyword": "Monzo", "reason": "test"},
        follow_redirects=True,
    )
    conn = _db(app)
    row = conn.execute("SELECT * FROM config_sector_keywords WHERE keyword = 'Monzo'").fetchone()
    assert row is None


def test_add_row_unknown_table_rejected(victoria_client):
    resp = victoria_client.post(
        "/admin/config/notices/add",  # not in EDITABLE_TABLES, even though it's a real table
        data={"ref": "HACK-1", "reason": "test"},
        follow_redirects=True,
    )
    assert b"unknown config table" in resp.data.lower()


def test_update_row_stamps_updated_by_and_at(victoria_client, app):
    conn = _db(app)
    row = conn.execute("SELECT * FROM config_owner_map WHERE sector = 'Fintech'").fetchone()

    victoria_client.post(
        f"/admin/config/config_owner_map/{row['id']}/update",
        data={"owner": "Mark", "reason": "no change, just confirming the stamp"},
        follow_redirects=True,
    )

    updated = conn.execute("SELECT * FROM config_owner_map WHERE id = ?", (row["id"],)).fetchone()
    assert updated["updated_by"] == "Victoria"
    assert updated["updated_at"] != row["updated_at"]


def test_delete_row_removes_row_and_logs_correction(victoria_client, app):
    conn = _db(app)
    row = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE sector = 'Fintech'"
    ).fetchone()

    resp = victoria_client.post(
        f"/admin/config/config_sector_keywords/{row['id']}/delete",
        data={"reason": "duplicate keyword"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    deleted = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE id = ?", (row["id"],)
    ).fetchone()
    assert deleted is None

    correction = conn.execute("SELECT * FROM rule_corrections ORDER BY id DESC LIMIT 1").fetchone()
    assert correction["entered_by"] == "Victoria"
    assert correction["table_affected"] == "config_sector_keywords"
    assert "Deleted row" in correction["description"]


def test_delete_row_requires_reason(victoria_client, app):
    conn = _db(app)
    row = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE sector = 'Fintech'"
    ).fetchone()

    resp = victoria_client.post(
        f"/admin/config/config_sector_keywords/{row['id']}/delete",
        data={},
        follow_redirects=True,
    )
    assert b"reason is required" in resp.data.lower()

    still_there = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE id = ?", (row["id"],)
    ).fetchone()
    assert still_there is not None


def test_delete_row_denied_for_non_correction_authority(hammad_client, app):
    conn = _db(app)
    row = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE sector = 'Fintech'"
    ).fetchone()

    hammad_client.post(
        f"/admin/config/config_sector_keywords/{row['id']}/delete",
        data={"reason": "test"},
        follow_redirects=True,
    )

    still_there = conn.execute(
        "SELECT * FROM config_sector_keywords WHERE id = ?", (row["id"],)
    ).fetchone()
    assert still_there is not None


def test_delete_row_unknown_table_rejected(victoria_client):
    resp = victoria_client.post(
        "/admin/config/notices/1/delete",
        data={"reason": "test"},
        follow_redirects=True,
    )
    assert b"unknown config table" in resp.data.lower()


def test_manual_sweep_button_and_route(victoria_client, app, monkeypatch):
    called = {}

    def fake_run_sweep(conn, settings, **kwargs):
        called["db_path"] = settings.db_path
        return {"pulled": 3, "expiring_leads": 1, "triaged": 2}

    monkeypatch.setattr("savvy_scout.dashboard.routes.home.run_sweep", fake_run_sweep)

    resp = victoria_client.get("/")
    assert b"Run sweep now" in resp.data

    resp = victoria_client.post("/sweep-now", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Sweep complete: pulled 3 notices" in resp.data
    assert called["db_path"] == app.config["SAVVY_SCOUT_DB_PATH"]
