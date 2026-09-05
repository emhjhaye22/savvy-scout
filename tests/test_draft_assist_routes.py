from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def _make_settings(db_path, **overrides):
    kwargs = dict(
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
    kwargs.update(overrides)
    return Settings(**kwargs)


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

    flask_app = create_app(_make_settings(db_path))
    flask_app.config["TESTING"] = True
    return flask_app


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


def _logged_in_client(app):
    client = app.test_client()
    client.post("/login", data={"username": "mark", "password": "testpass"})
    return client


def _insert_notice(conn, ref, title="An opportunity", sector="Fintech", is_award=0, indicative_value=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, status, source, uk_stage, is_award, "
        "indicative_value, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, ?, 'A Buyer', ?, 'NEW', 'Find a Tender', 'UK3', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, title, sector, is_award, indicative_value, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def _shortlist(conn, notice_id):
    conn.execute(
        "INSERT INTO shortlisted_notices (notice_id, added_by, added_at) VALUES (?, 'Mark', ?)",
        (notice_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def test_draft_assist_requires_login(app):
    client = app.test_client()
    resp = client.get("/draft-assist")
    assert resp.status_code in (302, 401)


def test_draft_assist_empty_state_with_nothing_shortlisted(app):
    client = _logged_in_client(app)
    resp = client.get("/draft-assist")
    assert resp.status_code == 200
    assert b"Nothing shortlisted yet" in resp.data


def test_draft_assist_picker_shows_shortlisted_opportunities(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A", title="Widget Contract")
    _shortlist(conn, notice_id)

    client = _logged_in_client(app)
    resp = client.get("/draft-assist")
    assert b"Widget Contract" in resp.data


def test_draft_assist_shows_price_guidance_from_real_award_data(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A", sector="Fintech")
    _insert_notice(conn, "REF-AWARD-1", sector="Fintech", is_award=1, indicative_value="100000 GBP")
    _insert_notice(conn, "REF-AWARD-2", sector="Fintech", is_award=1, indicative_value="300000 GBP")
    _insert_notice(conn, "REF-AWARD-3", sector="Fintech", is_award=1, indicative_value=None)

    client = _logged_in_client(app)
    resp = client.get(f"/draft-assist?notice_id={notice_id}")
    body = resp.data.decode()
    assert "100,000" in body
    assert "300,000" in body
    assert "200,000" in body  # median of 100k/300k
    assert "across 2 of 3 award" not in body  # phrasing belongs to Competitor Intel, not this screen
    assert "2 of 3 award" in body


def test_draft_assist_no_priced_awards_says_so(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A", sector="Aviation")
    _insert_notice(conn, "REF-AWARD-1", sector="Aviation", is_award=1, indicative_value=None)

    client = _logged_in_client(app)
    resp = client.get(f"/draft-assist?notice_id={notice_id}")
    assert b"not enough data for a real range" in resp.data


def test_draft_without_configured_provider_fails_gracefully(app):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A")

    client = _logged_in_client(app)
    resp = client.post(
        "/draft-assist/draft",
        data={"notice_id": notice_id, "question_text": "Describe your relevant experience."},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Could not draft an answer" in resp.data
    row = conn.execute("SELECT * FROM draft_assist_items WHERE notice_id = ?", (notice_id,)).fetchone()
    assert row is None


def test_draft_with_mocked_ai_client_creates_reviewable_item(app, monkeypatch):
    conn = _db(app)
    notice_id = _insert_notice(conn, "REF-A")

    def fake_get_client(settings):
        return object(), (lambda client, conn, notice_row, question_text: "A drafted answer."), "fake-model"

    monkeypatch.setattr(
        "savvy_scout.dashboard.routes.draft_assist.get_draft_assist_client", fake_get_client
    )

    client = _logged_in_client(app)
    resp = client.post(
        "/draft-assist/draft",
        data={"notice_id": notice_id, "question_text": "Describe your relevant experience."},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Draft answer generated" in resp.data
    assert b"A drafted answer." in resp.data
    assert b"Provisional" in resp.data

    row = conn.execute("SELECT * FROM draft_assist_items WHERE notice_id = ?", (notice_id,)).fetchone()
    assert row is not None
    assert row["status"] == "DRAFTED"
    assert row["model_used"] == "fake-model"

    review_resp = client.post(
        "/draft-assist/review",
        data={"item_id": row["id"], "notice_id": notice_id, "decision": "REVIEWED_USED"},
        follow_redirects=True,
    )
    assert review_resp.status_code == 200
    updated = conn.execute("SELECT * FROM draft_assist_items WHERE id = ?", (row["id"],)).fetchone()
    assert updated["status"] == "REVIEWED_USED"
    assert updated["reviewed_at"] is not None
