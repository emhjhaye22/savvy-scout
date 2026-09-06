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


def _insert_award(conn, ref, supplier_name, sector="Fintech", cpv_primary="72500000", text_blob="cloud software platform delivery"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, indicative_value, status, "
        "source, uk_stage, text_blob, raw_json, first_seen_at, last_swept_at, created_at, updated_at, "
        "is_award, supplier_name) "
        "VALUES (?, 'An award', 'A Buyer', ?, ?, '10000 GBP', 'ACTIVE', 'Find a Tender', 'UK5', ?, "
        "'{}', ?, ?, ?, ?, 1, ?)",
        (ref, sector, cpv_primary, text_blob, now, now, now, now, supplier_name),
    )
    conn.commit()


def test_overview_shows_top_competitors_panel(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "Acme Ltd")

    client = _logged_in_client(app)
    resp = client.get("/")
    body = resp.data.decode()
    assert "Top competitors" in body
    assert "Acme Ltd" in body


def test_overview_top_competitors_excludes_irrelevant_suppliers(app):
    conn = _db(app)
    _insert_award(
        conn, "REF-B", "GR Taxis", sector="NHS and Healthcare",
        cpv_primary="33100000", text_blob="taxi transport services",
    )

    client = _logged_in_client(app)
    resp = client.get("/")
    assert b"GR Taxis" not in resp.data


def test_overview_no_longer_shows_sweep_history(app):
    client = _logged_in_client(app)
    resp = client.get("/")
    assert b"Sweep history" not in resp.data
