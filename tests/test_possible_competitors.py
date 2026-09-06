from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.dashboard.routes.competitor_intel import possible_competitors_for_notice
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def _insert_award(conn, ref, buyer, sector, supplier_name, cpv_primary="72500000", text_blob="delivery of a cloud software platform"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, indicative_value, status, "
        "source, uk_stage, text_blob, raw_json, first_seen_at, last_swept_at, created_at, updated_at, "
        "is_award, supplier_name) "
        "VALUES (?, 'An awarded contract', ?, ?, ?, '10000 GBP', 'ACTIVE', 'Find a Tender', 'UK5', "
        "?, '{}', ?, ?, ?, ?, 1, ?)",
        (ref, buyer, sector, cpv_primary, text_blob, now, now, now, now, supplier_name),
    )
    conn.commit()


def test_ranks_same_buyer_above_same_sector(conn):
    _insert_award(conn, "REF-A", "NHS Trust", "NHS and Healthcare", "Incumbent Ltd")
    _insert_award(conn, "REF-B", "Another Buyer", "NHS and Healthcare", "Other Player Ltd")
    _insert_award(conn, "REF-C", "Another Buyer", "NHS and Healthcare", "Other Player Ltd")

    result = possible_competitors_for_notice(conn, "NHS and Healthcare", "NHS Trust")

    assert result[0]["supplier_name"] == "Incumbent Ltd"
    assert result[0]["same_buyer"] is True
    assert result[1]["supplier_name"] == "Other Player Ltd"
    assert result[1]["same_buyer"] is False
    assert result[1]["award_count"] == 2


def test_excludes_irrelevant_suppliers(conn):
    _insert_award(conn, "REF-A", "NHS Trust", "NHS and Healthcare", "Real Competitor Ltd")
    _insert_award(
        conn, "REF-B", "NHS Trust", "NHS and Healthcare", "GR Taxis",
        cpv_primary="33100000", text_blob="provision of taxi transport services",
    )

    result = possible_competitors_for_notice(conn, "NHS and Healthcare", "NHS Trust")

    names = [r["supplier_name"] for r in result]
    assert "Real Competitor Ltd" in names
    assert "GR Taxis" not in names


def test_returns_empty_list_without_sector(conn):
    assert possible_competitors_for_notice(conn, None, "Some Buyer") == []


def test_returns_empty_list_when_no_relevant_history(conn):
    assert possible_competitors_for_notice(conn, "Fintech", "A Buyer") == []


def test_caps_at_ten_results(conn):
    for i in range(15):
        _insert_award(conn, f"REF-{i}", "A Buyer", "Fintech", f"Supplier {i}")

    result = possible_competitors_for_notice(conn, "Fintech", "A Buyer")
    assert len(result) == 10


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


def _insert_live_notice(conn, ref, buyer, sector):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, owner, status, source, uk_stage, "
        "cpv_primary, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A live opportunity', ?, ?, 'Mark', 'NEW', 'Find a Tender', 'UK3', "
        "'72200000', '{}', ?, ?, ?, ?)",
        (ref, buyer, sector, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def test_notice_detail_shows_possible_competitors_panel(app):
    conn = _db(app)
    _insert_award(conn, "REF-AWARD", "A Buyer", "Fintech", "Real Competitor Ltd", cpv_primary="72200000")
    notice_id = _insert_live_notice(conn, "REF-LIVE", "A Buyer", "Fintech")

    client = _logged_in_client(app)
    resp = client.get(f"/notices/{notice_id}")
    body = resp.data.decode()
    assert "Possible competitors" in body
    assert "Real Competitor Ltd" in body


def test_notice_detail_omits_panel_when_no_matches(app):
    conn = _db(app)
    notice_id = _insert_live_notice(conn, "REF-LIVE", "A Buyer", "Fintech")

    client = _logged_in_client(app)
    resp = client.get(f"/notices/{notice_id}")
    assert b"Possible competitors" not in resp.data
