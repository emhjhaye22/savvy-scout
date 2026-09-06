import json
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all
from savvy_scout.models.notice import Notice
from savvy_scout.sources.ocds_parser import ParsedNotice
from savvy_scout.sweep.dedupe import upsert_notice
from savvy_scout.triage.gates import triage_notice
from savvy_scout.workflow import approvals

VALID_ASSESSMENT = {
    "capability_fit": {"rating": "MED", "reasoning": "Transferable engineering fit."},
    "competitor_position": {"rating": "UNKNOWN", "reasoning": "No incumbent named."},
    "right_to_win": {"rating": "MED", "reasoning": "Plausible given the profile."},
    "overall": {"rating": "PURSUE", "reasoning": "Worth pursuing."},
    "open_questions": ["Confirm the deadline with the buyer."],
}


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, content_text):
        self.content = [FakeTextBlock(content_text)]
        self.stop_reason = "end_turn"


class FakeMessages:
    def create(self, **kwargs):
        return FakeResponse(json.dumps(VALID_ASSESSMENT))


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


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


def _make_phase2_notice(conn, ref, title, buyer, deadline=None):
    """Runs a notice through the real Gate 1/Gate 2 pipeline (clean PASS,
    real-time-payments-platform text -> Fintech, owner Mark) and the real
    Phase 2 scope read, landing it in AWAITING_PHASE2_APPROVAL with a real
    phase2_assessments row -- same construction as test_approvals.py's
    _make_pass_notice/_advance_to_phase2_queue, since the Approval Queue's
    in_scope_filter_sql requires a real sector/CPV/uk_stage combination.
    title/buyer must differ meaningfully across notices in the same test --
    upsert_notice falls back to a fuzzy title+buyer match (dedupe.py's
    FUZZY_MATCH_THRESHOLD) when no exact ref match exists, and two near-
    identical titles/buyers collide onto the same row."""
    notice = Notice(
        ref=ref,
        title=title,
        buyer=buyer,
        source="Find a Tender",
        notice_type="UK3",
        uk_stage="UK3",
        raw_json="{}",
        cpv_primary="72200000",
        deadline=deadline,
    )
    parsed = ParsedNotice(
        notice=notice,
        text_blob="bespoke build of a real-time payments platform, a direct award open tender",
        tender_status="active",
    )
    notice_id = upsert_notice(conn, parsed)
    triage_notice(conn, notice_id)
    approvals.process_pending_phase2_scope_reads(conn, FakeClient())
    conn.commit()
    return notice_id


def _set_rating(conn, notice_id, rating):
    conn.execute(
        "UPDATE phase2_assessments SET overall_rating = ? WHERE notice_id = ? "
        "AND id = (SELECT MAX(id) FROM phase2_assessments WHERE notice_id = ?)",
        (rating, notice_id, notice_id),
    )
    conn.commit()


def test_phase2_queue_orders_by_rating_tier_before_deadline(app):
    """A DECLINE-rated notice due tomorrow shouldn't outrank a PURSUE-rated
    one due next month -- the exact gap found in the 1x1 feature audit
    (queue.py used to sort by deadline only, ignoring the AI rating already
    sitting in the same query)."""
    conn = _db(app)
    now = datetime.now(timezone.utc)
    urgent_decline_id = _make_phase2_notice(
        conn, "REF-URGENT-DECLINE", "Card Fraud Detection Overhaul", "First City Bank",
        deadline=(now + timedelta(days=1)).isoformat(),
    )
    _set_rating(conn, urgent_decline_id, "DECLINE")
    distant_pursue_id = _make_phase2_notice(
        conn, "REF-DISTANT-PURSUE", "Open Banking API Gateway Refresh", "Northgate Building Society",
        deadline=(now + timedelta(days=30)).isoformat(),
    )
    _set_rating(conn, distant_pursue_id, "PURSUE")

    client = _logged_in_client(app)
    html = client.get("/queue").get_data(as_text=True)
    # Scoped past the topbar's own "Recent activity" notification dropdown,
    # which also lists notices by ref earlier in the raw HTML than the
    # actual Approval Queue section this test cares about.
    queue_body = html.split("AI Scope Read Review", 1)[1]

    assert queue_body.index("REF-DISTANT-PURSUE") < queue_body.index("REF-URGENT-DECLINE")


def test_phase2_queue_deadline_chip_reflects_real_urgency(app):
    """queue.html defined .deadline-chip.urgent/.warning from the start, but
    every row hardcoded the 'ok' (green) class regardless of how close the
    deadline actually was -- found during the 1x1 audit."""
    conn = _db(app)
    now = datetime.now(timezone.utc)
    urgent_id = _make_phase2_notice(
        conn, "REF-URGENT", "Card Fraud Detection Overhaul", "First City Bank",
        deadline=(now + timedelta(days=1)).isoformat(),
    )
    _set_rating(conn, urgent_id, "PURSUE")
    far_out_id = _make_phase2_notice(
        conn, "REF-FAROUT", "Open Banking API Gateway Refresh", "Northgate Building Society",
        deadline=(now + timedelta(days=90)).isoformat(),
    )
    _set_rating(conn, far_out_id, "PURSUE")

    client = _logged_in_client(app)
    html = client.get("/queue").get_data(as_text=True)
    # Scoped past the topbar's own "Recent activity" notification dropdown,
    # which also lists notices by ref earlier in the raw HTML.
    queue_body = html.split("AI Scope Read Review", 1)[1]

    urgent_row = queue_body[queue_body.index("REF-URGENT"):queue_body.index("REF-URGENT") + 700]
    far_out_row = queue_body[queue_body.index("REF-FAROUT"):queue_body.index("REF-FAROUT") + 700]
    assert "deadline-chip urgent" in urgent_row
    assert "deadline-chip ok" in far_out_row
