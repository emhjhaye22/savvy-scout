from datetime import datetime, timedelta, timezone

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
        "INSERT INTO users (username, password_hash, display_name, is_admin, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", 1, datetime.now(timezone.utc).isoformat(), trifork_id),
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


def _logged_in_client(app):
    client = app.test_client()
    client.post("/login", data={"username": "mark", "password": "testpass"})
    return client


def _table_body(html):
    """Scopes an assertion to the opportunities table itself -- the topbar's
    'Needs attention' notification panel also lists notice titles and
    renders earlier in the page, which would otherwise throw off a raw
    substring-position comparison."""
    return html.split('id="opp-tbody"', 1)[1]


def _insert_notice(conn, ref, status, title=None, owner="Mark", sector="Fintech",
                    uk_stage="UK3", cpv_primary="72200000", first_seen_at=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, owner, status, source, uk_stage, "
        "cpv_primary, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, ?, 'A Buyer', ?, ?, ?, 'Find a Tender', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, title or f"Title {ref}", sector, owner, status, uk_stage, cpv_primary,
         first_seen_at or now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def _insert_phase2(conn, notice_id, overall_rating):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO phase2_assessments (notice_id, capability_fit_rating, capability_fit_reasoning, "
        "competitor_position_rating, competitor_position_reasoning, right_to_win_rating, "
        "right_to_win_reasoning, overall_rating, overall_reasoning, open_questions, model_used, created_at) "
        "VALUES (?, 'MED', 'x', 'UNKNOWN', 'x', 'MED', 'x', ?, 'x', '[]', 'test-model', ?)",
        (notice_id, overall_rating, now),
    )
    conn.commit()


def test_opportunities_defaults_to_priority_sort(app):
    conn = _db(app)
    _insert_notice(conn, "REF-CLOSED", "REJECTED", title="Closed one")
    pursue_id = _insert_notice(conn, "REF-PURSUE", "AWAITING_PHASE2_APPROVAL", title="Pursue one")
    _insert_phase2(conn, pursue_id, "PURSUE")
    _insert_notice(conn, "REF-QUEUED", "NEW", title="Queued one")
    review_id = _insert_notice(conn, "REF-REVIEW", "TO_REVIEW", title="Review one")

    client = _logged_in_client(app)
    resp = client.get("/opportunities")
    body = _table_body(resp.data.decode())

    pursue_pos = body.index("Pursue one")
    review_pos = body.index("Review one")
    queued_pos = body.index("Queued one")
    closed_pos = body.index("Closed one")
    assert pursue_pos < review_pos < queued_pos < closed_pos


def test_opportunities_priority_badges_reflect_tier(app):
    conn = _db(app)
    pursue_id = _insert_notice(conn, "REF-PURSUE", "AWAITING_PHASE2_APPROVAL")
    _insert_phase2(conn, pursue_id, "PURSUE")
    decline_id = _insert_notice(conn, "REF-DECLINE", "AWAITING_PHASE2_APPROVAL")
    _insert_phase2(conn, decline_id, "DECLINE")
    _insert_notice(conn, "REF-NOAI", "AWAITING_PHASE2_APPROVAL")  # no phase2 row at all

    client = _logged_in_client(app)
    resp = client.get("/opportunities")
    body = resp.data.decode()

    assert "Pursue now" in body
    assert "AI: Decline" in body
    assert "Needs review" in body  # the no-AI-read AWAITING_PHASE2_APPROVAL falls to tier 2


def test_opportunities_sort_newest_preserves_first_seen_at_order(app):
    conn = _db(app)
    older = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    newer = datetime.now(timezone.utc).isoformat()
    # Older notice would win on priority (PURSUE, tier 1) but newest sort
    # must ignore that and go strictly by first_seen_at.
    older_id = _insert_notice(conn, "REF-OLDER", "AWAITING_PHASE2_APPROVAL", title="Older pursue", first_seen_at=older)
    _insert_phase2(conn, older_id, "PURSUE")
    _insert_notice(conn, "REF-NEWER", "NEW", title="Newer queued", first_seen_at=newer)

    client = _logged_in_client(app)
    resp = client.get("/opportunities?sort=newest")
    body = _table_body(resp.data.decode())

    assert body.index("Newer queued") < body.index("Older pursue")


def test_opportunities_sort_toggle_defaults_to_priority_link_active(app):
    client = _logged_in_client(app)
    resp = client.get("/opportunities")
    body = resp.data.decode()
    assert 'sort=\'priority\'' not in body  # sanity: not a raw unescaped literal
    assert "Priority" in body and "Newest" in body


def test_admin_sees_every_sector_and_owner_by_default(app):
    """2026-09-20 explicit request: Admin's All Opportunities view is the
    one "every trade and sector ever swept" page in the app -- unlike a
    sector owner (scoped to their own notices) or even Account Approver
    (scoped to Trifork's configured sectors), Admin must see a notice with
    no sector at all, owned by someone else entirely."""
    conn = _db(app)
    _insert_notice(conn, "REF-UNCLASSIFIED", "TO_REVIEW", title="Unclassified trade", sector=None, owner=None)
    _insert_notice(conn, "REF-OTHER-OWNER", "TO_REVIEW", title="Other owner notice", owner="Someone Else")

    client = _logged_in_client(app)
    resp = client.get("/opportunities")
    body = resp.data.decode()

    assert "Unclassified trade" in body
    assert "Other owner notice" in body
    assert "every trade and sector ever swept" in body


def test_non_admin_owner_stays_scoped_to_configured_sectors_and_own_notices(app):
    """The widening above is Admin-only -- a regular sector owner must keep
    seeing only their own notices, and Account Approver must keep seeing
    only Trifork's configured scope, exactly as before."""
    conn = _db(app)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, created_at, client_id) "
        "SELECT 'priya', ?, 'Priya', 'account_user', ?, client_id FROM users WHERE username = 'mark'",
        (generate_password_hash("testpass"), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    _insert_notice(conn, "REF-UNCLASSIFIED", "TO_REVIEW", title="Unclassified trade", sector=None, owner=None)
    _insert_notice(conn, "REF-MARKS", "TO_REVIEW", title="Marks own notice", owner="Mark")

    client = app.test_client()
    client.post("/login", data={"username": "priya", "password": "testpass"})
    resp = client.get("/opportunities")
    body = resp.data.decode()

    assert "Unclassified trade" not in body
    assert "Marks own notice" not in body
