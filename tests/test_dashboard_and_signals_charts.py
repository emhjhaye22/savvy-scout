from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.dashboard.routes.signals import _renewals_by_month
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


def _insert_award(conn, ref, sector, indicative_value):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, indicative_value, status, "
        "source, uk_stage, raw_json, first_seen_at, last_swept_at, created_at, updated_at, is_award) "
        "VALUES (?, 'An award', 'A Buyer', ?, '72500000', ?, 'ACTIVE', 'Find a Tender', 'UK5', "
        "'{}', ?, ?, ?, ?, 1)",
        (ref, sector, indicative_value, now, now, now, now),
    )
    conn.commit()


def test_dashboard_shows_sector_spend_chart(app):
    conn = _db(app)
    # config_owner_map is seeded with the six real sectors already; use one
    # of them for real.
    sector = conn.execute("SELECT sector FROM config_owner_map LIMIT 1").fetchone()["sector"]
    _insert_award(conn, "REF-A", sector, "250000 GBP")

    client = _logged_in_client(app)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Market size by sector" in body
    assert "£250,000" in body


def test_dashboard_sector_spend_empty_state_when_no_priced_awards(app):
    client = _logged_in_client(app)
    resp = client.get("/")
    assert b"No sectors have a priced award notice yet" in resp.data


def test_renewals_by_month_buckets_by_end_date_month():
    now = datetime.now(timezone.utc)
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
    signals = [
        {"end_date": now.isoformat()},
        {"end_date": now.isoformat()},
        {"end_date": next_month.isoformat()},
        {"end_date": None},  # must not crash on a missing end_date
    ]
    result = _renewals_by_month(signals, months_ahead=3)
    assert result[0]["value"] == 2
    assert result[1]["value"] == 1
    assert sum(r["value"] for r in result) == 3


def test_renewals_by_month_ignores_dates_outside_window():
    far_future = (datetime.now(timezone.utc) + timedelta(days=800)).isoformat()
    result = _renewals_by_month([{"end_date": far_future}], months_ahead=3)
    assert sum(r["value"] for r in result) == 0


def test_signals_page_shows_renewals_chart(app):
    conn = _db(app)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO contract_expiry (notice_ref, buyer, title, end_date, review_date, source_ref, created_at) "
        "VALUES ('REF-A', 'Buyer', 'Renewal A', ?, ?, 'REF-A', ?)",
        (now.isoformat(), now.isoformat(), now.isoformat()),
    )
    conn.commit()

    client = _logged_in_client(app)
    resp = client.get("/signals")
    assert resp.status_code == 200
    assert b"Renewals by month" in resp.data
