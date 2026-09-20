import json
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
        "VALUES (?, ?, ?, 0, 1, ?, ?)",
        ("emhjhaye", generate_password_hash("testpass"), "emhjhaye", datetime.now(timezone.utc).isoformat(), trifork_id),
    )
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, 1, 0, ?, ?)",
        ("victoria", generate_password_hash("testpass"), "Victoria", datetime.now(timezone.utc).isoformat(), trifork_id),
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


def _logged_in_client(app, username):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": "testpass"})
    return client


def _admin_client(app):
    return _logged_in_client(app, "emhjhaye")


def _insert_notice(conn, ref, cpv_primary="45200000", text_blob="construction of a new bridge"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, status, cpv_primary, text_blob, "
        "raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A notice', 'A Buyer', 'Find a Tender', 'UK3', 'NEW', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, cpv_primary, text_blob, now, now, now, now),
    )
    conn.commit()


def test_admin_index_shows_clients_section_for_admin(app):
    client = _admin_client(app)
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert b"Clients" in resp.data
    assert b'href="/admin/clients/new"' in resp.data


def test_admin_index_hides_clients_section_from_victoria(app):
    client = _logged_in_client(app, "victoria")
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert b'href="/admin/clients/new"' not in resp.data


def test_new_client_page_renders_for_admin(app):
    client = _admin_client(app)
    resp = client.get("/admin/clients/new")
    assert resp.status_code == 200
    assert b"Add a client" in resp.data
    assert b'action="/admin/clients/add"' in resp.data


def test_new_client_page_denied_to_non_admin(app):
    client = _logged_in_client(app, "victoria")
    resp = client.get("/admin/clients/new", follow_redirects=True)
    assert b"Only the admin account" in resp.data


def test_edit_client_page_shows_existing_filter(app):
    conn = _db(app)
    admin = _admin_client(app)
    client_id = _add_client_and_get_id(admin, conn)  # cpv_prefixes="45"

    resp = admin.get(f"/admin/clients/{client_id}/edit")
    assert resp.status_code == 200
    assert b"Acme Construction" in resp.data
    assert b'value="45"' in resp.data


def test_edit_client_page_rejects_trifork(app):
    conn = _db(app)
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    client = _admin_client(app)
    resp = client.get(f"/admin/clients/{trifork_id}/edit", follow_redirects=True)
    assert b"Client not found" in resp.data


def test_edit_client_page_denied_to_non_admin(app):
    client = _logged_in_client(app, "victoria")
    resp = client.get("/admin/clients/1/edit", follow_redirects=True)
    assert b"Only the admin account" in resp.data


def test_add_client_creates_client_and_filter(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    _insert_notice(conn, "REF-B", cpv_primary="72500000")

    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={"name": "Acme Construction", "cpv_prefixes": "45", "notice_types": ["UK3", "UK4"]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"evaluated 2 existing notices" in resp.data

    row = conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone()
    assert row is not None
    filter_row = conn.execute("SELECT * FROM client_filters WHERE client_id = ?", (row["id"],)).fetchone()
    assert json.loads(filter_row["cpv_prefixes"]) == ["45"]

    results = conn.execute("SELECT outcome FROM client_triage_results WHERE client_id = ?", (row["id"],)).fetchall()
    outcomes = {r["outcome"] for r in results}
    assert outcomes == {"PASS", "FAIL"}


def test_add_client_creates_account_user_and_approver_seats(app):
    """2026-09-20 explicit request: adding a client should let the admin
    invite its first Account User and Account Approver in the same save,
    instead of a separate trip to Manage Users for each."""
    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={
            "name": "Acme Construction",
            "cpv_prefixes": "45",
            "account_user_name": "Priya",
            "account_user_email": "priya@acme.example",
            "account_approver_name": "Jordan",
            "account_approver_email": "jordan@acme.example",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    conn = _db(app)
    client_row = conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone()
    user_row = conn.execute("SELECT * FROM users WHERE email = 'priya@acme.example'").fetchone()
    approver_row = conn.execute("SELECT * FROM users WHERE email = 'jordan@acme.example'").fetchone()

    assert user_row["role"] == "account_user"
    assert user_row["client_id"] == client_row["id"]
    assert user_row["display_name"] == "Priya"
    assert approver_row["role"] == "account_approver"
    assert approver_row["client_id"] == client_row["id"]


def test_add_client_without_seats_creates_no_extra_users(app):
    """Both seats are optional -- leaving them blank must not create
    anything, same as today's behavior before this feature existed."""
    conn = _db(app)
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert after == before


def test_add_client_rejects_seat_with_name_but_no_email(app):
    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={"name": "Acme Construction", "cpv_prefixes": "45", "account_user_name": "Priya"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"both required" in resp.data
    # Preserved on the re-rendered form, not lost.
    assert b'value="Priya"' in resp.data

    conn = _db(app)
    assert conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone() is None


def test_add_client_rejects_seats_sharing_an_email(app):
    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={
            "name": "Acme Construction",
            "cpv_prefixes": "45",
            "account_user_name": "Priya",
            "account_user_email": "shared@acme.example",
            "account_approver_name": "Jordan",
            "account_approver_email": "shared@acme.example",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"same email" in resp.data
    conn = _db(app)
    assert conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone() is None


def test_add_client_rejects_seat_email_already_in_use(app):
    conn = _db(app)
    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={
            "name": "Acme Construction",
            "cpv_prefixes": "45",
            # local part "emhjhaye" derives the same username as the
            # existing admin fixture account -- a real username collision.
            "account_user_name": "Duplicate",
            "account_user_email": "emhjhaye@example.com",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"already exists" in resp.data
    assert conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone() is None


def test_add_client_rejects_trifork_name(app):
    client = _admin_client(app)
    resp = client.post("/admin/clients/add", data={"name": "Trifork"}, follow_redirects=True)
    assert b"reserved" in resp.data


def test_add_client_rejects_duplicate_name(app):
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    resp = client.post(
        "/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"}, follow_redirects=True
    )
    assert b"already exists" in resp.data


def test_non_admin_cannot_add_client(app):
    client = _logged_in_client(app, "victoria")
    resp = client.post("/admin/clients/add", data={"name": "Acme Construction"}, follow_redirects=True)
    conn = _db(app)
    row = conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone()
    assert row is None
    assert b"Only the admin account" in resp.data


def test_update_client_filter_reevaluates_notices(app):
    conn = _db(app)
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    client_id = conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]

    _insert_notice(conn, "REF-A", cpv_primary="72500000")

    resp = client.post(
        f"/admin/clients/{client_id}/update-filter",
        data={"cpv_prefixes": "72"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    result = conn.execute(
        "SELECT outcome FROM client_triage_results WHERE client_id = ? AND notice_id = "
        "(SELECT id FROM notices WHERE ref = 'REF-A')",
        (client_id,),
    ).fetchone()
    assert result["outcome"] == "PASS"


def test_toggle_client_active(app):
    conn = _db(app)
    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    client_id = conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]

    client.post(f"/admin/clients/{client_id}/toggle-active")
    row = conn.execute("SELECT is_active FROM clients WHERE id = ?", (client_id,)).fetchone()
    assert row["is_active"] == 0

    client.post(f"/admin/clients/{client_id}/toggle-active")
    row = conn.execute("SELECT is_active FROM clients WHERE id = ?", (client_id,)).fetchone()
    assert row["is_active"] == 1


def test_client_matches_view_shows_matched_notices(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    _insert_notice(conn, "REF-B", cpv_primary="72500000")

    client = _admin_client(app)
    client.post("/admin/clients/add", data={"name": "Acme Construction", "cpv_prefixes": "45"})
    client_id = conn.execute("SELECT id FROM clients WHERE name = 'Acme Construction'").fetchone()["id"]

    resp = client.get(f"/admin/clients/{client_id}/matches")
    assert resp.status_code == 200
    assert b"REF-A" in resp.data
    assert b"REF-B" not in resp.data


def test_client_matches_view_denied_to_non_admin(app):
    client = _logged_in_client(app, "victoria")
    resp = client.get("/admin/clients/1/matches", follow_redirects=True)
    assert b"Only the admin account" in resp.data


def _add_client_and_get_id(client, conn, name="Acme Construction", **form):
    form.setdefault("cpv_prefixes", "45")
    client.post("/admin/clients/add", data={"name": name, **form})
    return conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()["id"]


def test_matched_notice_defaults_to_new_status(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)

    resp = client.get(f"/admin/clients/{client_id}/matches")
    table_body = resp.data.decode().split("<tbody>", 1)[1]
    assert ">New<" in table_body
    assert "badge-pass" not in table_body


def test_set_client_notice_status_to_shortlisted_with_note(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    resp = client.post(
        f"/admin/clients/{client_id}/notices/{notice_id}/set-status",
        data={"status": "SHORTLISTED", "note": "Looks promising"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchone()
    assert row["status"] == "SHORTLISTED"
    assert row["note"] == "Looks promising"
    assert b"Shortlisted" in resp.data
    assert b"Looks promising" in resp.data


def test_set_client_notice_status_upserts_on_repeat(app):
    """Changing a decision updates the existing row, not a duplicate."""
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    client.post(f"/admin/clients/{client_id}/notices/{notice_id}/set-status", data={"status": "SHORTLISTED"})
    client.post(f"/admin/clients/{client_id}/notices/{notice_id}/set-status", data={"status": "REJECTED"})

    rows = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "REJECTED"


def test_set_client_notice_status_rejects_invalid_status(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    resp = client.post(
        f"/admin/clients/{client_id}/notices/{notice_id}/set-status",
        data={"status": "BOGUS"},
        follow_redirects=True,
    )
    assert b"Invalid status" in resp.data
    row = conn.execute(
        "SELECT * FROM client_notice_actions WHERE client_id = ? AND notice_id = ?", (client_id, notice_id)
    ).fetchone()
    assert row is None


def test_client_matches_status_filter(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", cpv_primary="45200000", text_blob="construction bridge one")
    _insert_notice(conn, "REF-B", cpv_primary="45300000", text_blob="construction bridge two")
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)
    notice_a = conn.execute("SELECT id FROM notices WHERE ref = 'REF-A'").fetchone()["id"]

    client.post(f"/admin/clients/{client_id}/notices/{notice_a}/set-status", data={"status": "SHORTLISTED"})

    resp = client.get(f"/admin/clients/{client_id}/matches?status=SHORTLISTED")
    body = resp.data.decode()
    assert "REF-A" in body
    assert "REF-B" not in body

    resp = client.get(f"/admin/clients/{client_id}/matches?status=NEW")
    body = resp.data.decode()
    assert "REF-A" not in body
    assert "REF-B" in body


def test_set_client_notice_status_denied_to_non_admin(app):
    client = _logged_in_client(app, "victoria")
    resp = client.post("/admin/clients/1/notices/1/set-status", data={"status": "SHORTLISTED"}, follow_redirects=True)
    assert b"Only the admin account" in resp.data


def test_add_client_rejects_empty_filter(app):
    """2026-09-19 safeguard: evaluate_client_filter() treats an unconfigured
    field as no constraint, so an entirely empty filter would PASS every
    notice in the backlog -- a brand-new tenant's first login would land on
    the full undifferentiated notice stream instead of a real match set."""
    conn = _db(app)
    client = _admin_client(app)
    resp = client.post("/admin/clients/add", data={"name": "Acme Construction"}, follow_redirects=True)
    assert b"filter field" in resp.data
    assert conn.execute("SELECT * FROM clients WHERE name = 'Acme Construction'").fetchone() is None


def test_add_client_preserves_typed_fields_on_failure(app):
    """2026-09-20: any failure here used to redirect to a blank form,
    discarding all 7 fields on a single mistake -- e.g. forgetting the
    name after carefully filling in the filter."""
    client = _admin_client(app)
    resp = client.post(
        "/admin/clients/add",
        data={"name": "", "keywords": "construction, building works"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert b"Client name is required" in resp.data
    assert 'value="construction, building works"' in body


def test_update_client_filter_rejects_empty_filter(app):
    conn = _db(app)
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)

    resp = client.post(f"/admin/clients/{client_id}/update-filter", data={}, follow_redirects=True)
    assert b"filter field" in resp.data
    filter_row = conn.execute("SELECT * FROM client_filters WHERE client_id = ?", (client_id,)).fetchone()
    assert json.loads(filter_row["cpv_prefixes"]) == ["45"]  # unchanged from _add_client_and_get_id's default


def test_update_client_filter_empty_submission_still_shows_real_unchanged_filter(app):
    """2026-09-20: unlike add_row/update_row/add_client, this route's only
    failure is "every field was left blank" -- there's no partially-typed
    submission to preserve (any single non-blank field would have
    succeeded instead). The redirect back to edit_client() must keep
    showing the filter that's actually still saved (cpv_prefixes "45"),
    not a blank one matching what was just (mistakenly) submitted."""
    conn = _db(app)
    client = _admin_client(app)
    client_id = _add_client_and_get_id(client, conn)  # seeds cpv_prefixes=["45"]

    resp = client.post(f"/admin/clients/{client_id}/update-filter", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert 'value="45"' in resp.data.decode()


def test_add_user_defaults_to_trifork_when_no_client_id_given(app):
    """Backward compatibility: an old cached form (or any client posting
    without the new client_id field) must never silently create a login
    for the wrong client -- it should default to Trifork, never leave a
    stray unscoped account."""
    conn = _db(app)
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    client = _admin_client(app)
    resp = client.post(
        "/admin/users/add", data={"display_name": "New Hire", "email": "newhire@bidsavvy.io"}, follow_redirects=True
    )
    assert resp.status_code == 200
    row = conn.execute("SELECT * FROM users WHERE email = 'newhire@bidsavvy.io'").fetchone()
    assert row["client_id"] == trifork_id


def test_add_user_creates_account_for_chosen_client(app):
    conn = _db(app)
    acme_id = _add_client_and_get_id(_admin_client(app), conn)
    client = _admin_client(app)
    client.post(
        "/admin/users/add",
        data={"display_name": "Acme Contact", "email": "contact@acme.example", "client_id": str(acme_id)},
        follow_redirects=True,
    )
    row = conn.execute("SELECT * FROM users WHERE email = 'contact@acme.example'").fetchone()
    assert row["client_id"] == acme_id


def test_add_user_forces_account_user_role_for_admin_on_non_trifork_client(app):
    """admin is the platform owner (2026-09-20, generalized from the old
    is_victoria-only guard) -- it must never be attachable to a non-Trifork
    tenant account, even if the form posts role=admin."""
    conn = _db(app)
    acme_id = _add_client_and_get_id(_admin_client(app), conn)
    client = _admin_client(app)
    client.post(
        "/admin/users/add",
        data={
            "display_name": "Acme Contact",
            "email": "contact@acme.example",
            "client_id": str(acme_id),
            "role": "admin",
        },
        follow_redirects=True,
    )
    row = conn.execute("SELECT * FROM users WHERE email = 'contact@acme.example'").fetchone()
    assert row["role"] == "account_user"


def test_add_user_allows_account_approver_role_for_non_trifork_client(app):
    """Unlike admin, a tenant client CAN have its own Account Approver
    (2026-09-20) -- this is the whole point of generalizing the role model
    off Trifork's single hardcoded Victoria."""
    conn = _db(app)
    acme_id = _add_client_and_get_id(_admin_client(app), conn)
    client = _admin_client(app)
    client.post(
        "/admin/users/add",
        data={
            "display_name": "Acme Approver",
            "email": "approver@acme.example",
            "client_id": str(acme_id),
            "role": "account_approver",
        },
        follow_redirects=True,
    )
    row = conn.execute("SELECT * FROM users WHERE email = 'approver@acme.example'").fetchone()
    assert row["role"] == "account_approver"
