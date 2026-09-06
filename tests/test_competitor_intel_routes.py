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


def _insert_irrelevant_award(conn, ref, buyer, sector, supplier_name):
    """A win that's real (real buyer, real sector) but is not Trifork's
    type of work -- CPV 33xxx (medical devices) is a seeded Gate 2 CPV
    disqualifier, and there's no digital/software signal in the text, so
    gate2_type_of_work returns FAIL for this one specifically."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, indicative_value, status, "
        "source, uk_stage, text_blob, raw_json, first_seen_at, last_swept_at, created_at, updated_at, "
        "is_award, supplier_name) "
        "VALUES (?, 'Non-emergency patient transport', ?, ?, '33100000', NULL, 'ACTIVE', "
        "'Find a Tender', 'UK5', 'provision of taxi and ambulance transport services', '{}', "
        "?, ?, ?, ?, 1, ?)",
        (ref, buyer, sector, now, now, now, now, supplier_name),
    )
    conn.commit()


def test_competitors_tab_filters_out_irrelevant_suppliers_by_default(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    _insert_irrelevant_award(conn, "REF-B", "NHS Trust", "NHS and Healthcare", "GR Taxis")

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel")
    body = resp.data.decode()
    assert "Acme Ltd" in body
    assert "GR Taxis" not in body
    assert "Show all suppliers (+1 filtered out)" in body


def test_competitors_tab_show_all_reveals_irrelevant_suppliers(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    _insert_irrelevant_award(conn, "REF-B", "NHS Trust", "NHS and Healthcare", "GR Taxis")

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel?show=all")
    body = resp.data.decode()
    assert "Acme Ltd" in body
    assert "GR Taxis" in body
    assert "Not Trifork's type of work" in body
    assert "Show likely competitors only" in body


def test_competitor_relevant_if_any_award_is_relevant(app):
    """A supplier with one relevant win and one irrelevant one still counts
    as a real competitor -- relevance is "any", not "all"."""
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Mixed Supplier Ltd", "10000 GBP")
    _insert_irrelevant_award(conn, "REF-B", "NHS Trust", "NHS and Healthcare", "Mixed Supplier Ltd")

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel")
    assert b"Mixed Supplier Ltd" in resp.data


def _insert_award_with_date(conn, ref, buyer, sector, supplier_name, indicative_value, first_seen_at):
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, cpv_primary, indicative_value, status, "
        "source, uk_stage, raw_json, first_seen_at, last_swept_at, created_at, updated_at, is_award, supplier_name) "
        "VALUES (?, 'An awarded contract', ?, ?, '72500000', ?, 'ACTIVE', 'Find a Tender', 'UK5', "
        "'{}', ?, ?, ?, ?, 1, ?)",
        (ref, buyer, sector, indicative_value, first_seen_at, first_seen_at, first_seen_at, first_seen_at, supplier_name),
    )
    conn.commit()


def test_competitor_detail_requires_login(app):
    client = app.test_client()
    resp = client.get("/competitor-intel/detail?name=Acme Ltd")
    assert resp.status_code in (302, 401)


def test_competitor_detail_404s_for_unknown_supplier(app):
    client = _logged_in_client(app)
    resp = client.get("/competitor-intel/detail?name=Nobody Ever Won As This")
    assert resp.status_code == 404


def test_competitor_detail_shows_stats_and_tabs(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "Buyer One", "Fintech", "Acme Ltd", "250000 GBP")
    _insert_award(conn, "REF-B", "Buyer Two", "Aviation", "Acme Ltd", "150000 GBP")

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel/detail?name=Acme Ltd")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Acme Ltd" in body
    assert "£400,000" in body  # 250000 + 150000
    assert "Buyer One" in body
    assert "Buyer Two" in body
    assert "Fintech" in body
    assert "Aviation" in body
    assert "All Contracts" in body and "Buyers" in body and "Sectors" in body


def test_competitor_detail_chart_buckets_by_month(app):
    conn = _db(app)
    _insert_award_with_date(conn, "REF-A", "Buyer One", "Fintech", "Acme Ltd", "100000 GBP", "2026-01-15T00:00:00+00:00")
    _insert_award_with_date(conn, "REF-B", "Buyer Two", "Fintech", "Acme Ltd", "50000 GBP", "2026-01-20T00:00:00+00:00")
    _insert_award_with_date(conn, "REF-C", "Buyer Three", "Fintech", "Acme Ltd", "75000 GBP", "2026-03-01T00:00:00+00:00")

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel/detail?name=Acme Ltd")
    body = resp.data.decode()
    assert "Jan 2026" in body
    assert "Mar 2026" in body
    assert "£150,000" in body  # Jan bucket: 100000 + 50000
    assert "£75,000" in body  # Mar bucket


def test_competitor_detail_watch_toggle_links_back_to_detail_page(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    client = _logged_in_client(app)

    resp = client.post(
        "/competitor-intel/watch",
        data={"supplier_name": "Acme Ltd", "next": "/competitor-intel/detail?name=Acme Ltd"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/competitor-intel/detail?name=Acme%20Ltd"
    row = conn.execute("SELECT * FROM watched_competitors WHERE supplier_name = 'Acme Ltd'").fetchone()
    assert row is not None


def test_detail_page_shows_not_configured_when_no_api_key(app):
    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    client = _logged_in_client(app)

    resp = client.get("/competitor-intel/detail?name=Acme Ltd")
    body = resp.data.decode()
    assert "Not configured" in body
    assert "COMPANIES_HOUSE_API_KEY" in body


def test_detail_page_shows_company_record_when_found(app, monkeypatch):
    import dataclasses

    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    app.config["SAVVY_SCOUT_SETTINGS"] = dataclasses.replace(
        app.config["SAVVY_SCOUT_SETTINGS"], companies_house_api_key="test-key"
    )

    def fake_get_company_info(conn, supplier_name, api_key):
        assert supplier_name == "Acme Ltd"
        return {
            "company_name": "ACME LIMITED", "company_number": "01234567",
            "address": "1 High Street, London", "status": "active",
        }

    monkeypatch.setattr(
        "savvy_scout.dashboard.routes.competitor_intel.get_company_info", fake_get_company_info
    )

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel/detail?name=Acme Ltd")
    body = resp.data.decode()
    assert "ACME LIMITED" in body
    assert "01234567" in body
    assert "1 High Street, London" in body
    assert "find-and-update.company-information.service.gov.uk/company/01234567" in body


def test_detail_page_shows_no_match_found(app, monkeypatch):
    import dataclasses

    conn = _db(app)
    _insert_award(conn, "REF-A", "A Buyer", "Fintech", "Acme Ltd", "10000 GBP")
    app.config["SAVVY_SCOUT_SETTINGS"] = dataclasses.replace(
        app.config["SAVVY_SCOUT_SETTINGS"], companies_house_api_key="test-key"
    )
    monkeypatch.setattr(
        "savvy_scout.dashboard.routes.competitor_intel.get_company_info", lambda conn, name, key: None
    )

    client = _logged_in_client(app)
    resp = client.get("/competitor-intel/detail?name=Acme Ltd")
    assert b"No Companies House match found" in resp.data


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
