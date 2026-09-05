from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.dashboard.routes.competitor_intel import _parse_gbp
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


def _insert_award(conn, ref, buyer, sector, supplier_name, indicative_value):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, indicative_value, status, "
        "source, uk_stage, raw_json, first_seen_at, last_swept_at, created_at, updated_at, is_award, supplier_name) "
        "VALUES (?, 'An awarded contract', ?, ?, '72500000', ?, 'ACTIVE', 'Find a Tender', 'UK5', "
        "'{}', ?, ?, ?, ?, 1, ?)",
        (ref, buyer, sector, indicative_value, now, now, now, now, supplier_name),
    )
    conn.commit()


def test_competitor_intel_requires_login(app):
    client = app.test_client()
    resp = client.get("/competitor-intel")
    assert resp.status_code in (302, 401)


def test_competitor_intel_empty_state(app):
    client = _logged_in_client(app)
    resp = client.get("/competitor-intel")
    assert resp.status_code == 200
    assert b"No award notices" in resp.data


def test_competitor_intel_aggregates_awards_and_parses_value(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "NHS Buyer", "NHS and Healthcare", "Acme Ltd", "833156.96 GBP")
    _insert_award(conn, "REF-B", "Another Buyer", "Central and Local Government", "Acme Ltd", "250000 GBP")
    _insert_award(conn, "REF-C", "Third Buyer", "NHS and Healthcare", "Acme Ltd", None)

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Acme Ltd" in body
    assert "1,083,157" in body
    assert "across 2 of 3 award" in body
    assert "NHS and Healthcare" in body
    assert "Central and Local Government" in body


def test_competitor_intel_buyers_tab(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Named Buyer", "Fintech", "Acme Ltd", "10000 GBP")

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel?tab=buyers")
    assert resp.status_code == 200
    assert b"A Named Buyer" in resp.data


def test_watch_toggle_round_trips(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    client = _logged_in_client(app)

    resp = client.post("/competitor-intel/watch", data={"supplier_name": "Acme Ltd"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"watched" in resp.data
    row = conn.execute("SELECT * FROM watched_competitors WHERE supplier_name = 'Acme Ltd'").fetchone()
    assert row is not None

    client.post("/competitor-intel/watch", data={"supplier_name": "Acme Ltd"})
    row = conn.execute("SELECT * FROM watched_competitors WHERE supplier_name = 'Acme Ltd'").fetchone()
    assert row is None


class TestParseGbp:
    def test_parses_plain_integer(self):
        assert _parse_gbp("250000 GBP") == 250000.0

    def test_parses_decimal(self):
        assert _parse_gbp("833156.96 GBP") == 833156.96

    def test_returns_none_for_missing(self):
        assert _parse_gbp(None) is None
        assert _parse_gbp("") is None

    def test_returns_none_for_unparseable(self):
        assert _parse_gbp("unknown") is None
