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
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at) "
        "VALUES (?, ?, ?, 0, 1, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", datetime.now(timezone.utc).isoformat()),
    )
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at) "
        "VALUES (?, ?, ?, 1, 0, ?)",
        ("victoria", generate_password_hash("testpass"), "Victoria", datetime.now(timezone.utc).isoformat()),
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


def test_login_redirects_to_welcome(app):
    client = app.test_client()
    resp = client.post("/login", data={"username": "mark", "password": "testpass"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/welcome"


def test_welcome_page_greets_the_logged_in_user(app):
    client = _logged_in_client(app, "mark")
    resp = client.get("/welcome")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Welcome, Mark!" in html
    assert "Where would you like to go?" in html


def test_welcome_page_links_to_every_main_destination(app):
    client = _logged_in_client(app, "mark")
    html = client.get("/welcome").get_data(as_text=True)
    for label in [
        "Overview", "Approval Queue", "All Opportunities", "Signals",
        "Competitor Intel", "Shortlists", "Draft assist", "Settings", "Admin",
    ]:
        assert label in html


def test_welcome_page_hides_admin_link_for_non_admin_non_victoria_user(app):
    conn = _db(app)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        ("plainuser", generate_password_hash("testpass"), "Plain", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    client = _logged_in_client(app, "plainuser")
    html = client.get("/welcome").get_data(as_text=True)
    assert "Admin" not in html


def test_welcome_page_shows_clients_section_for_admin_with_trifork_first(app):
    conn = _db(app)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES ('Acme Construction', 1, ?, 'Mark')",
        (now,),
    )
    conn.commit()
    conn.close()

    client = _logged_in_client(app, "mark")
    html = client.get("/welcome").get_data(as_text=True)
    assert "Trifork" in html
    assert "Full workspace" in html
    assert "Acme Construction" in html
    assert "Matches only" in html
    assert html.index("Trifork") < html.index("Acme Construction")


def test_welcome_page_hides_clients_section_for_non_admin(app):
    client = _logged_in_client(app, "victoria")
    html = client.get("/welcome").get_data(as_text=True)
    assert "Matches only" not in html
    assert "Full workspace" not in html


def test_welcome_page_omits_inactive_clients(app):
    conn = _db(app)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES ('Paused Co', 0, ?, 'Mark')",
        (now,),
    )
    conn.commit()
    conn.close()

    client = _logged_in_client(app, "mark")
    html = client.get("/welcome").get_data(as_text=True)
    assert "Paused Co" not in html
