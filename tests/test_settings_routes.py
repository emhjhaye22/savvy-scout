from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all
from savvy_scout.export.trifork_pipeline import HEADERS
from savvy_scout.graph.mail import ALLOWED_DOMAIN


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


def _logged_in_client(app):
    client = app.test_client()
    client.post("/login", data={"username": "mark", "password": "testpass"})
    return client


def test_settings_requires_login(app):
    client = app.test_client()
    resp = client.get("/settings")
    assert resp.status_code in (302, 401)


def test_settings_shows_whitelist_domain(app):
    client = _logged_in_client(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f"@{ALLOWED_DOMAIN}" in body
    assert "not editable" in body.lower() or "not configurable" in body.lower()


def test_settings_shows_every_tracker_column(app):
    client = _logged_in_client(app)
    resp = client.get("/settings")
    body = resp.data.decode()
    for column in HEADERS:
        assert column in body


def test_settings_shows_sweep_history_section(app):
    """Moved here from Overview 2026-09-06 -- operational/diagnostic detail,
    not a daily-glance business metric."""
    client = _logged_in_client(app)
    resp = client.get("/settings")
    body = resp.data.decode()
    assert "Sweep history" in body
    assert "No sweeps recorded yet" in body


def test_settings_has_no_whitelist_editing_form(app):
    """The whitelist is a hard, non-bypassable check in graph/mail.py
    (SPEC.md non-negotiable 2) -- this page must never grow a form that
    implies it can be edited from the UI. (The sidebar's sweep-now/
    generate-reports forms are shared by every page via base.html and
    aren't what this guards against -- neither posts anywhere near
    /settings.)"""
    client = _logged_in_client(app)
    resp = client.get("/settings")
    body = resp.data.decode()
    assert 'action="{}/whitelist"'.format("/settings") not in body
    assert "add sender" not in body.lower()
    assert "remove sender" not in body.lower()
