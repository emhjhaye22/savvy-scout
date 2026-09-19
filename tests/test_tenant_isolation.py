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
    trifork_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, 1, 0, ?, ?)",
        ("victoria", generate_password_hash("testpass"), "Victoria", datetime.now(timezone.utc).isoformat(), trifork_id),
    )
    now = datetime.now(timezone.utc).isoformat()
    setup_conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES ('Acme Construction', 1, ?, 'Mark')",
        (now,),
    )
    acme_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO client_filters (client_id, cpv_prefixes, keywords, notice_types, regions, "
        "min_value, max_value, updated_at, updated_by) VALUES (?, '[\"45\"]', NULL, NULL, NULL, NULL, NULL, ?, 'Mark')",
        (acme_id, now),
    )
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, 0, 0, ?, ?)",
        ("acmeuser", generate_password_hash("testpass"), "Acme User", now, acme_id),
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


def _logged_in_client(app, username):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": "testpass"})
    return client


GATED_ROUTES = ["/", "/queue", "/opportunities", "/signals", "/competitor-intel", "/draft-assist", "/settings", "/admin/"]


@pytest.mark.parametrize("route", GATED_ROUTES)
def test_non_trifork_tenant_redirected_away_from_trifork_pipeline(app, route):
    """The tenant-isolation gate (dashboard/__init__.py's before_request):
    without it, the very first non-Trifork login would see Trifork's
    entire live pipeline -- gate results, AI assessments, escalation
    briefs. A tenant should be bounced to /welcome from every one of
    these, no exceptions."""
    client = _logged_in_client(app, "acmeuser")
    resp = client.get(route, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/welcome"


@pytest.mark.parametrize("route", GATED_ROUTES)
def test_trifork_user_not_redirected(app, route):
    """The gate must be a complete no-op for every existing Trifork login
    -- zero behavior change for Mark/Victoria and anyone else in daily use
    today."""
    client = _logged_in_client(app, "victoria")
    resp = client.get(route, follow_redirects=False)
    assert resp.status_code != 302 or resp.headers.get("Location") != "/welcome"


def test_non_trifork_tenant_can_reach_welcome_and_my_matches(app):
    client = _logged_in_client(app, "acmeuser")
    assert client.get("/welcome").status_code == 200
    assert client.get("/my-matches").status_code == 200


def test_non_trifork_tenant_can_still_log_out(app):
    client = _logged_in_client(app, "acmeuser")
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] != "/welcome"


def test_sidebar_hides_trifork_only_links_for_tenant(app):
    """2026-09-19 regression: base.html's sidebar is shared by every
    authenticated page. Without gating it on is_trifork too, a tenant
    login would see a full-looking menu (Overview, Approval Queue, Signals,
    etc.) whose links all silently redirect back to Welcome via the
    tenant-isolation gate the moment they're clicked -- confusing, not
    just restricted."""
    client = _logged_in_client(app, "acmeuser")
    html = client.get("/my-matches").get_data(as_text=True)
    assert 'nav-label">Your Matches<' in html
    assert 'nav-label">Approval Queue<' not in html
    assert 'nav-label">Competitor Intel<' not in html
    assert "Workflow Stages" not in html


def test_sidebar_shows_full_menu_for_trifork_user(app):
    client = _logged_in_client(app, "victoria")
    html = client.get("/").get_data(as_text=True)
    assert 'nav-label">Approval Queue<' in html
    assert 'nav-label">Competitor Intel<' in html
