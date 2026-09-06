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


def _insert_notice(conn, ref, status, title=None, owner="Mark", sector="Fintech"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, owner, status, source, uk_stage, "
        "cpv_primary, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, ?, 'A Buyer', ?, ?, ?, 'Find a Tender', 'UK3', '72200000', '{}', ?, ?, ?, ?)",
        (ref, title or f"Title {ref}", sector, owner, status, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def test_no_selection_shows_empty_detail_pane(app):
    conn = _db(app)
    _insert_notice(conn, "REF-A", "NEW", title="Some Notice")
    client = _logged_in_client(app)
    resp = client.get("/opportunities")
    assert b"Select a notice" in resp.data


def test_selecting_a_notice_shows_detail_pane(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A", "NEW", title="Preview Me Notice")
    client = _logged_in_client(app)
    resp = client.get(f"/opportunities?selected={notice_id}")
    body = resp.data.decode()
    assert body.count("Preview Me Notice") >= 2  # once in the list, once in the detail pane
    assert "View full notice" in body
    assert "row-selected" in body


def test_selecting_unknown_id_shows_empty_state_not_error(app):
    client = _logged_in_client(app)
    resp = client.get("/opportunities?selected=99999")
    assert resp.status_code == 200
    assert b"Select a notice" in resp.data


def test_bulk_shortlist_add(app):
    conn = _db(app)
    id_a = _insert_notice(conn, "REF-A", "NEW")
    id_b = _insert_notice(conn, "REF-B", "NEW")
    client = _logged_in_client(app)

    resp = client.post(
        "/opportunities/bulk-shortlist",
        data={"notice_ids": [str(id_a), str(id_b)], "bulk_action": "add"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    rows = conn.execute("SELECT notice_id FROM shortlisted_notices ORDER BY notice_id").fetchall()
    assert [r["notice_id"] for r in rows] == sorted([id_a, id_b])


def test_bulk_shortlist_remove(app):
    conn = _db(app)
    id_a = _insert_notice(conn, "REF-A", "NEW")
    conn.execute(
        "INSERT INTO shortlisted_notices (notice_id, added_by, added_at) VALUES (?, 'Mark', ?)",
        (id_a, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    client = _logged_in_client(app)

    client.post("/opportunities/bulk-shortlist", data={"notice_ids": [str(id_a)], "bulk_action": "remove"})
    row = conn.execute("SELECT * FROM shortlisted_notices WHERE notice_id = ?", (id_a,)).fetchone()
    assert row is None


def test_bulk_shortlist_no_selection_flashes_error(app):
    client = _logged_in_client(app)
    resp = client.post("/opportunities/bulk-shortlist", data={"bulk_action": "add"}, follow_redirects=True)
    assert b"No notices selected" in resp.data


def test_bulk_mark_docs_downloaded_succeeds_for_eligible_notices(app):
    conn = _db(app)
    approved_id = _insert_notice(conn, "REF-APPROVED", "APPROVED")
    client = _logged_in_client(app)

    resp = client.post(
        "/opportunities/bulk-mark-docs-downloaded",
        data={"notice_ids": [str(approved_id)]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Marked 1 notice" in resp.data
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (approved_id,)).fetchone()
    assert row["status"] == "DOCS_DOWNLOADED"


def test_bulk_mark_docs_downloaded_skips_ineligible_notices(app):
    """A notice still in NEW isn't Approved/Capture-Brief-Drafted -- this must
    be skipped with a clear count, not silently ignored or a 500."""
    conn = _db(app)
    new_id = _insert_notice(conn, "REF-NEW", "NEW")
    client = _logged_in_client(app)

    resp = client.post(
        "/opportunities/bulk-mark-docs-downloaded",
        data={"notice_ids": [str(new_id)]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Skipped 1 notice" in resp.data
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (new_id,)).fetchone()
    assert row["status"] == "NEW"


def test_bulk_mark_docs_downloaded_mixed_eligibility_reports_both_counts(app):
    conn = _db(app)
    approved_id = _insert_notice(conn, "REF-APPROVED", "APPROVED")
    new_id = _insert_notice(conn, "REF-NEW", "NEW")
    client = _logged_in_client(app)

    resp = client.post(
        "/opportunities/bulk-mark-docs-downloaded",
        data={"notice_ids": [str(approved_id), str(new_id)]},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert "Marked 1 notice" in body
    assert "Skipped 1 notice" in body
