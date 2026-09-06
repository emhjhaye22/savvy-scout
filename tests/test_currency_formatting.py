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
        "VALUES (?, ?, ?, 0, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", datetime.now(timezone.utc).isoformat()),
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


class TestGbpFilter:
    def _filter(self, app):
        return app.jinja_env.filters["gbp"]

    def test_formats_plain_gbp_string(self, app):
        assert self._filter(app)("250000 GBP") == "£250,000"

    def test_formats_decimal_gbp_string(self, app):
        assert self._filter(app)("833156.96 GBP") == "£833,157"

    def test_returns_none_for_missing(self, app):
        assert self._filter(app)(None) is None
        assert self._filter(app)("") is None

    def test_returns_original_string_when_unparseable(self, app):
        assert self._filter(app)("some free-text estimate") == "some free-text estimate"


def _insert_notice_with_value(conn, ref, indicative_value):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, owner, status, source, uk_stage, "
        "cpv_primary, indicative_value, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A notice', 'A Buyer', 'Fintech', 'Mark', 'NEW', 'Find a Tender', 'UK3', "
        "'72200000', ?, '{}', ?, ?, ?, ?)",
        (ref, indicative_value, now, now, now, now),
    )
    conn.commit()


def test_opportunities_list_shows_formatted_value_not_raw_string(app):
    """Live report (2026-09-06): the VALUE column showed the raw OCDS-style
    string ("250000 GBP") instead of a formatted figure, inconsistent with
    every other place in the app that shows money."""
    conn = _db(app)
    _insert_notice_with_value(conn, "REF-A", "250000 GBP")

    client = _logged_in_client(app)
    resp = client.get("/opportunities")
    body = resp.data.decode()

    assert "£250,000" in body
    assert "250000 GBP" not in body
