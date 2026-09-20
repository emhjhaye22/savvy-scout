from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db, slugify, unique_client_slug
from savvy_scout.db.seed_config import seed_all


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    trifork_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin, created_at, client_id) "
        "VALUES ('mark', ?, 'Mark', 1, ?, ?)",
        (generate_password_hash("testpass"), datetime.now(timezone.utc).isoformat(), trifork_id),
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


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


def _add_client(conn, name):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, slug, is_active, created_at, created_by) VALUES (?, ?, 1, ?, 'Mark')",
        (name, unique_client_slug(conn, name), now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM clients WHERE name = ?", (name,)).fetchone()


def _add_tenant_user(conn, username, display_name, client_id, role="account_user"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, generate_password_hash("testpass"), display_name, role, now, client_id),
    )
    conn.commit()


def test_slugify_normalizes_name():
    assert slugify("Acme Construction") == "acme-construction"
    assert slugify("O'Brien & Sons Ltd.") == "o-brien-sons-ltd"


def test_unique_client_slug_disambiguates_collisions(app):
    conn = _db(app)
    first = unique_client_slug(conn, "Acme Ltd")
    conn.execute(
        "INSERT INTO clients (name, slug, is_active, created_at, created_by) VALUES ('Acme Ltd', ?, 1, 'now', 'Mark')",
        (first,),
    )
    conn.commit()
    second = unique_client_slug(conn, "Acme Ltd")
    assert first != second
    assert second == "acme-ltd-2"


def test_trifork_has_a_slug_after_boot(app):
    conn = _db(app)
    row = conn.execute("SELECT slug FROM clients WHERE name = 'Trifork'").fetchone()
    assert row["slug"] == "trifork"


def test_client_login_page_shows_client_name_and_hides_quick_access(app):
    conn = _db(app)
    client = _add_client(conn, "Acme Construction")

    resp = app.test_client().get(f"/login/{client['slug']}")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "Sign in to Acme Construction" in body
    assert "Quick access" not in body


def test_client_login_unknown_slug_shows_generic_login_with_error(app):
    resp = app.test_client().get("/login/does-not-exist")
    assert resp.status_code == 200
    assert b"That portal link" in resp.data
    assert b"Quick access" in resp.data


def test_client_login_authenticates_and_redirects_regardless_of_slug(app):
    """The slug is a branding/convenience layer, not a security boundary
    (2026-09-20 explicit design) -- logging in via a DIFFERENT client's
    link with valid credentials still authenticates and redirects the
    same way; the tenant-isolation gate (unchanged) is what actually
    governs access on the next request."""
    conn = _db(app)
    acme = _add_client(conn, "Acme Construction")
    other = _add_client(conn, "Other Co")
    _add_tenant_user(conn, "acmeuser", "Acme User", acme["id"], role="account_user")

    client = app.test_client()
    resp = client.post(
        f"/login/{other['slug']}",
        data={"username": "acmeuser", "password": "testpass"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # Redirected through welcome -> tenant-isolation gate, same as always;
    # not stuck on the "Other Co" login page.
    assert b"Sign in" not in resp.data


def test_generic_login_still_shows_quick_access(app):
    resp = app.test_client().get("/login")
    body = resp.data.decode()
    assert "Quick access" in body
    assert "Sign in to" not in body
