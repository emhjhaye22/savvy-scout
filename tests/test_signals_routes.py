from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.dashboard.routes.signals import _urgency
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


def _insert_notice_and_expiry(conn, ref, sector, end_date, review_date):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, value_amount_gross, status, "
        "source, uk_stage, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A renewal candidate', 'A Test Buyer', ?, '72500000', 450000, 'ACTIVE', "
        "'Find a Tender', 'UK4', '{}', ?, ?, ?, ?)",
        (ref, sector, now, now, now, now),
    )
    conn.execute(
        "INSERT INTO contract_expiry (notice_ref, buyer, title, end_date, review_date, source_ref, created_at) "
        "VALUES (?, 'A Test Buyer', 'A renewal candidate', ?, ?, ?, ?)",
        (ref, end_date, review_date, ref, now),
    )
    conn.commit()


def test_signals_requires_login(app):
    client = app.test_client()
    resp = client.get("/signals")
    assert resp.status_code in (302, 401)


def test_signals_empty_state(app):
    client = _logged_in_client(app)
    resp = client.get("/signals")
    assert resp.status_code == 200
    assert b"No renewals currently" in resp.data


def test_signals_shows_a_renewal_with_notice_link(app):
    conn = _db(app)
    now = datetime.now(timezone.utc)
    _insert_notice_and_expiry(
        conn, "REF-1", "Energy",
        end_date=(now + timedelta(days=200)).isoformat(),
        review_date=(now + timedelta(days=20)).isoformat(),
    )
    client = _logged_in_client(app)
    resp = client.get("/signals")
    assert resp.status_code == 200
    assert b"A renewal candidate" in resp.data
    assert b"A Test Buyer" in resp.data
    assert b"Energy" in resp.data
    assert b"/notices/" in resp.data


def test_signals_urgency_buckets_and_counts(app):
    conn = _db(app)
    now = datetime.now(timezone.utc)
    _insert_notice_and_expiry(
        conn, "REF-DUE-NOW", "Fintech",
        end_date=(now + timedelta(days=30)).isoformat(),
        review_date=(now - timedelta(days=1)).isoformat(),
    )
    _insert_notice_and_expiry(
        conn, "REF-DUE-SOON", "Fintech",
        end_date=(now + timedelta(days=60)).isoformat(),
        review_date=(now + timedelta(days=10)).isoformat(),
    )
    _insert_notice_and_expiry(
        conn, "REF-UPCOMING", "Fintech",
        end_date=(now + timedelta(days=300)).isoformat(),
        review_date=(now + timedelta(days=100)).isoformat(),
    )
    client = _logged_in_client(app)
    resp = client.get("/signals")
    assert resp.status_code == 200
    assert b"Review due now" in resp.data
    assert b"Due soon" in resp.data
    assert b"Upcoming" in resp.data
    assert b"(1)" in resp.data


class TestUrgencyHelper:
    def test_review_date_passed_is_due_now(self):
        now = datetime.now(timezone.utc)
        past_review = (now - timedelta(days=1)).isoformat()
        far_end = (now + timedelta(days=200)).isoformat()
        assert _urgency(past_review, far_end) == "due_now"

    def test_end_date_within_90_days_is_due_soon(self):
        now = datetime.now(timezone.utc)
        future_review = (now + timedelta(days=10)).isoformat()
        near_end = (now + timedelta(days=60)).isoformat()
        assert _urgency(future_review, near_end) == "due_soon"

    def test_far_out_end_date_is_upcoming(self):
        now = datetime.now(timezone.utc)
        future_review = (now + timedelta(days=100)).isoformat()
        far_end = (now + timedelta(days=300)).isoformat()
        assert _urgency(future_review, far_end) == "upcoming"

    def test_missing_review_date_defaults_to_upcoming(self):
        assert _urgency(None, None) == "upcoming"
